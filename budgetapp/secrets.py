"""Secrets in the macOS Keychain (ARCHITECTURE §3, SEC-2).

Everything secret is read and written through this module only, under the Keychain service
``budgetapp``:

=====================================  =============================================================
Keychain account                       Value
=====================================  =============================================================
``plaid.client_id``                    Plaid client ID (the same for every Plaid environment)
``plaid.secret.sandbox``               Plaid Sandbox secret
``plaid.secret.production``            Plaid Production secret
``plaid.access_token.production``      THE production access token (v1 allows one production Item)
``plaid.link_in_progress.production``  marker written when a production new-link token is issued
``plaid.access_token.sandbox.<item>``  Sandbox access tokens, one per Item
``plaid.item_ids.sandbox``             JSON list of Sandbox item ids (keyring cannot list items)
``db.password.<role>``                 generated passwords of budgetapp_dev / budgetapp_live
=====================================  =============================================================

Rules: values are never written to the database, files (except the 0600 recovery file below), logs,
exceptions, process arguments or templates; every value read is registered with the log redactor;
tests use :class:`MemoryStore` (``use_store``), never the real Keychain (the live Sandbox tests may
read only the Sandbox items).

Production link guard (ARCHITECTURE §7, Principle 4): :func:`production_link_blockers` lists every
reason outside the database not to issue a new production link — a saved production token, an
unexpired link-in-progress marker, or a production token waiting in a recovery file.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import re
import secrets as _stdlib_secrets
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from budgetapp import clock
from budgetapp.logsetup import register_secret

if TYPE_CHECKING:
    from budgetapp.config import Settings

SERVICE = "budgetapp"

PLAID_CLIENT_ID = "plaid.client_id"
PRODUCTION_ACCESS_TOKEN = "plaid.access_token.production"
SANDBOX_ITEM_INDEX = "plaid.item_ids.sandbox"
LINK_TOKEN_LIFETIME = timedelta(hours=4)

#: Keychain status codes that mean "the Keychain wants the user to approve access":
#: -25293 errSecAuthFailed (python binary changed, e.g. after `brew upgrade`), -25308
#: errSecInteractionNotAllowed (unattended mode, a prompt was needed), -128 errSecUserCanceled.
APPROVAL_STATUSES = frozenset({-25293, -25308, -128})
KEYCHAIN_FIX_COMMAND = "./run doctor --env production"


# --------------------------------------------------------------------------- account names


def plaid_secret_key(plaid_env: str) -> str:
    """Keychain account of the Plaid secret for ``sandbox`` or ``production``."""
    _check_plaid_env(plaid_env)
    return f"plaid.secret.{plaid_env}"


def sandbox_access_token_key(item_id: str) -> str:
    """Keychain account of one Sandbox Item's access token."""
    if not item_id or not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        raise ValueError("invalid Plaid item id")
    return f"plaid.access_token.sandbox.{item_id}"


def access_token_key(plaid_env: str, item_id: str | None = None) -> str:
    """Keychain account of an access token: fixed in production, per Item in Sandbox."""
    _check_plaid_env(plaid_env)
    if plaid_env == "production":
        return PRODUCTION_ACCESS_TOKEN
    if item_id is None:
        raise ValueError("Sandbox access tokens are stored per Item; pass item_id")
    return sandbox_access_token_key(item_id)


def link_in_progress_key(plaid_env: str = "production") -> str:
    _check_plaid_env(plaid_env)
    return f"plaid.link_in_progress.{plaid_env}"


def db_password_key(role: str) -> str:
    """Keychain account of a database role's generated password."""
    if role not in ("budgetapp_dev", "budgetapp_live"):
        raise ValueError(f"no stored password for role {role!r}")
    return f"db.password.{role}"


def _check_plaid_env(plaid_env: str) -> None:
    if plaid_env not in ("sandbox", "production"):
        raise ValueError(f"unknown Plaid environment {plaid_env!r}")


# --------------------------------------------------------------------------- errors


class SecretStoreError(Exception):
    """The secret store failed (the message never contains a secret value)."""


class MissingSecretError(SecretStoreError):
    """A required secret is not in the Keychain."""

    def __init__(self, account: str, hint: str | None = None) -> None:
        self.account = account
        message = f"No value for {account!r} in Keychain."
        if hint:
            message = f"{message} {hint}"
        super().__init__(message)


class KeychainApprovalNeeded(SecretStoreError):
    """The Keychain needs the user to approve access (status -25293, -25308 or -128).

    Unattended processes notify (once a day, see :func:`claim_daily_marker`) and exit 75; the web
    app shows a page with :data:`KEYCHAIN_FIX_COMMAND`.
    """

    def __init__(self, account: str, status: int | None = None) -> None:
        self.account = account
        self.status = status
        super().__init__(
            f"Keychain approval is needed to read {account!r} (status {status}). In Terminal run "
            f"`{KEYCHAIN_FIX_COMMAND}` and click Always Allow."
        )


class AccessTokenConflict(SecretStoreError):
    """A different access token is already stored where a new one was about to be written."""


class TokenNotSavedError(SecretStoreError):
    """An access token could not be saved; it was written to a 0600 recovery file instead.

    ``recovery_path`` is ``None`` when even the recovery file could not be written: the token then
    exists only in the caller's memory (the ``ExchangeResult``), so the caller must show it to the
    user once to keep. The message never contains the token.
    """

    def __init__(self, message: str, recovery_path: Path | None) -> None:
        self.recovery_path = recovery_path
        if recovery_path is not None:
            detail = f"The token was saved to {recovery_path} (readable only by you)."
        else:
            detail = (
                "Writing a recovery file failed too, so the token exists only in this process: "
                "show it to the user to copy into a password manager, then store it with "
                "./run setup --env production --access-token."
            )
        super().__init__(f"{message} {detail}")


# --------------------------------------------------------------------------- stores


@runtime_checkable
class SecretStore(Protocol):
    """Minimal key/value secret store keyed by Keychain account name."""

    def get(self, account: str) -> str | None: ...

    def set(self, account: str, value: str) -> None: ...

    def delete(self, account: str) -> None: ...


@dataclass
class MemoryStore:
    """In-memory store for tests, with failure injection.

    ``fail_on[("get" | "set" | "delete", account)] = exception`` makes that call raise; use
    ``account=None`` for any account. Failures stay until removed.
    """

    values: dict[str, str] = field(default_factory=dict)
    fail_on: dict[tuple[str, str | None], BaseException] = field(default_factory=dict)
    corrupt_reads: set[str] = field(default_factory=set)

    def _maybe_fail(self, op: str, account: str) -> None:
        exc = self.fail_on.get((op, account)) or self.fail_on.get((op, None))
        if exc is not None:
            raise exc

    def get(self, account: str) -> str | None:
        self._maybe_fail("get", account)
        value = self.values.get(account)
        if value is not None and account in self.corrupt_reads:
            return value + "-corrupted"
        register_secret(value)
        return value

    def set(self, account: str, value: str) -> None:
        self._maybe_fail("set", account)
        register_secret(value)
        self.values[account] = value

    def delete(self, account: str) -> None:
        self._maybe_fail("delete", account)
        self.values.pop(account, None)


def _keychain_status(exc: BaseException) -> int | None:
    """Extract the Security framework status code from a keyring error chain."""
    seen: BaseException | None = exc
    while seen is not None:
        args = getattr(seen, "args", ())
        if args and isinstance(args[0], int) and not isinstance(args[0], bool):
            return args[0]
        seen = seen.__cause__ or seen.__context__
    match = re.search(r"\((-?\d+),", str(exc))
    return int(match.group(1)) if match else None


def is_sandbox_account(account: str) -> bool:
    """Keychain accounts the live Sandbox tests may touch (never production or DB passwords)."""
    return account in (
        PLAID_CLIENT_ID,
        "plaid.secret.sandbox",
        SANDBOX_ITEM_INDEX,
    ) or account.startswith("plaid.access_token.sandbox.")


class KeychainStore:
    """The login Keychain via ``keyring`` (service ``budgetapp``).

    ``backend`` defaults to keyring's macOS backend; tests pass a fake with the keyring backend
    API (``get_password``/``set_password``/``delete_password``) to exercise the error mapping.
    Under ``BUDGETAPP_ENV=test`` the real Keychain is refused, except for the live Sandbox tests
    (``BUDGETAPP_LIVE_SANDBOX=1``), which may touch only :func:`is_sandbox_account` items.
    """

    def __init__(self, service: str = SERVICE, backend: Any = None) -> None:
        self.service = service
        self._backend = backend
        self._injected = backend is not None

    def _check_account(self, account: str) -> None:
        if (
            not self._injected
            and os.environ.get("BUDGETAPP_ENV") == "test"
            and not is_sandbox_account(account)
        ):
            raise SecretStoreError(
                f"Live Sandbox tests may use only the Sandbox Keychain items, not {account!r}."
            )

    @property
    def backend(self) -> Any:
        if self._backend is None:
            if os.environ.get("BUDGETAPP_ENV") == "test" and not os.environ.get(
                "BUDGETAPP_LIVE_SANDBOX"
            ):
                raise SecretStoreError(
                    "Tests must not use the real Keychain (use MemoryStore via use_store)."
                )
            if sys.platform == "darwin":
                from keyring.backends.macOS import Keyring

                self._backend = Keyring()
            else:  # pragma: no cover - the app targets macOS
                import keyring

                self._backend = keyring.get_keyring()
        return self._backend

    def _translate(self, exc: Exception, account: str, action: str) -> SecretStoreError:
        from keyring.errors import KeyringLocked

        status = _keychain_status(exc)
        if isinstance(exc, KeyringLocked) or status in APPROVAL_STATUSES:
            return KeychainApprovalNeeded(account, status)
        return SecretStoreError(f"Keychain could not {action} {account!r} (status {status}).")

    def get(self, account: str) -> str | None:
        from keyring.errors import KeyringError

        self._check_account(account)
        try:
            value = self.backend.get_password(self.service, account)
        except KeyringError as exc:
            raise self._translate(exc, account, "read") from None
        register_secret(value)
        return value

    def set(self, account: str, value: str) -> None:
        from keyring.errors import KeyringError

        self._check_account(account)
        register_secret(value)
        try:
            self.backend.set_password(self.service, account, value)
        except KeyringError as exc:
            raise self._translate(exc, account, "save") from None

    def delete(self, account: str) -> None:
        from keyring.errors import KeyringError

        self._check_account(account)
        try:
            self.backend.delete_password(self.service, account)
        except KeyringError as exc:
            if _keychain_status(exc) == -25300:  # errSecItemNotFound: already gone
                return
            raise self._translate(exc, account, "delete") from None


_store_lock = threading.Lock()
_store: SecretStore | None = None


def get_store() -> SecretStore:
    """The process-wide secret store (the Keychain unless :func:`use_store` replaced it)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = KeychainStore()
        return _store


class _StoreSwap(contextlib.AbstractContextManager["SecretStore"]):
    def __init__(self, store: SecretStore, previous: SecretStore | None) -> None:
        self._store = store
        self._previous = previous

    def __enter__(self) -> SecretStore:
        return self._store

    def __exit__(self, *exc: object) -> None:
        global _store
        with _store_lock:
            _store = self._previous


def use_store(store: SecretStore) -> _StoreSwap:
    """Make ``store`` the process-wide store; used as a context manager, restore the old one."""
    global _store
    with _store_lock:
        previous = _store
        _store = store
    return _StoreSwap(store, previous)


# --------------------------------------------------------------------------- generic access


def get_secret(account: str, *, store: SecretStore | None = None) -> str | None:
    """Read a secret (``None`` when absent). The value is registered with the log redactor."""
    value = (store or get_store()).get(account)
    register_secret(value)
    return value


def require_secret(
    account: str, *, store: SecretStore | None = None, hint: str | None = None
) -> str:
    """Read a secret or raise :class:`MissingSecretError` (with ``hint`` on how to add it)."""
    value = get_secret(account, store=store)
    if not value:
        raise MissingSecretError(account, hint)
    return value


def set_secret(account: str, value: str, *, store: SecretStore | None = None) -> None:
    """Write a secret and read it back to verify (raises :class:`SecretStoreError` on mismatch)."""
    if not value:
        raise ValueError("refusing to store an empty secret")
    store = store or get_store()
    register_secret(value)
    store.set(account, value)
    if store.get(account) != value:
        raise SecretStoreError(f"Keychain read-back of {account!r} did not match what was written.")


def delete_secret(account: str, *, store: SecretStore | None = None) -> None:
    (store or get_store()).delete(account)


def generate_password(nbytes: int = 32) -> str:
    """A random URL-safe password for a database role."""
    return _stdlib_secrets.token_urlsafe(nbytes)


def plaid_credentials(plaid_env: str, *, store: SecretStore | None = None) -> tuple[str, str]:
    """``(client_id, secret)`` for a Plaid environment, or :class:`MissingSecretError`."""
    hint = "Run ./run setup to enter your Plaid keys."
    client_id = require_secret(PLAID_CLIENT_ID, store=store, hint=hint)
    secret = require_secret(plaid_secret_key(plaid_env), store=store, hint=hint)
    return client_id, secret


# --------------------------------------------------------------------------- access tokens


def sandbox_item_ids(*, store: SecretStore | None = None) -> list[str]:
    """Item ids of every Sandbox access token stored (the Keychain cannot list accounts)."""
    raw = get_secret(SANDBOX_ITEM_INDEX, store=store)
    if not raw:
        return []
    try:
        ids = json.loads(raw)
    except ValueError:
        return []
    return [str(i) for i in ids if isinstance(i, str)]


def _add_sandbox_item_id(item_id: str, store: SecretStore) -> None:
    ids = sandbox_item_ids(store=store)
    if item_id not in ids:
        ids.append(item_id)
        store.set(SANDBOX_ITEM_INDEX, json.dumps(ids))


def remove_sandbox_item(item_id: str, *, store: SecretStore | None = None) -> None:
    """Delete a Sandbox Item's token and its index entry (Sandbox only; never production)."""
    store = store or get_store()
    store.delete(sandbox_access_token_key(item_id))
    ids = [i for i in sandbox_item_ids(store=store) if i != item_id]
    store.set(SANDBOX_ITEM_INDEX, json.dumps(ids))


def get_access_token(
    plaid_env: str, item_id: str | None = None, *, store: SecretStore | None = None
) -> str | None:
    """The stored access token for an Item (production: THE token), or ``None``."""
    return get_secret(access_token_key(plaid_env, item_id), store=store)


def store_access_token(
    settings: Settings,
    item_id: str,
    access_token: str,
    *,
    store: SecretStore | None = None,
) -> None:
    """Save an access token exactly once and read it back (ARCHITECTURE §3, §7).

    * The token is never rewritten: if a different token is already stored under the same
      account, nothing is overwritten and :class:`AccessTokenConflict` is raised (after saving the
      new token to a recovery file, because every production token used a Trial connection).
    * If the write or the read-back fails — for ANY reason, including Ctrl-C (re-raised after
      the file is written) — the token goes to a 0600 recovery file under ``settings.state_dir``
      (or, if that fails too, under :func:`fallback_recovery_dir`) and
      :class:`TokenNotSavedError` is raised; callers stop and show the path and
      ``./run setup --env production --access-token``. An unsaved production recovery file keeps
      blocking new links (:func:`production_link_blockers`).
    * Sandbox tokens are added to the ``plaid.item_ids.sandbox`` index BEFORE the token is written
      (an index entry without a token is harmless; a token nobody can find is an orphan).
    * Callers clear the link-in-progress marker only after this returns normally.
    """
    if not access_token or not item_id:
        raise ValueError("item_id and access_token are required")
    plaid_env = settings.plaid_env
    store = store or get_store()
    register_secret(access_token)
    account = access_token_key(plaid_env, item_id)

    def _recover(reason: str, cls: type[SecretStoreError] = TokenNotSavedError) -> SecretStoreError:
        path = _save_recovery_file(settings, item_id, access_token)
        if cls is AccessTokenConflict:
            where = (
                f"The new token was saved to {path} (readable only by you)."
                if path is not None
                else "The new token could not be written to a recovery file either."
            )
            return AccessTokenConflict(f"{reason} {where}")
        return TokenNotSavedError(reason, path)

    # Any failure below must not lose the token (each production token used a Trial connection),
    # so every exception — even KeyboardInterrupt/SystemExit — ends in the recovery file.
    try:
        existing = store.get(account)
    except BaseException as exc:
        error = _recover("Keychain could not be read before saving the access token.")
        if not isinstance(exc, Exception):
            raise
        raise error from exc
    if existing is not None:
        if existing == access_token:
            if plaid_env == "sandbox":
                _add_sandbox_item_id(item_id, store)
            return
        raise _recover(
            f"A different access token is already stored as {account!r}; it was not replaced.",
            AccessTokenConflict,
        )
    try:
        if plaid_env == "sandbox":
            _add_sandbox_item_id(item_id, store)
        store.set(account, access_token)
        readback = store.get(account)
    except BaseException as exc:
        error = _recover("Keychain refused to save the access token.")
        if not isinstance(exc, Exception):
            raise
        raise error from exc
    if readback != access_token:
        raise _recover("Keychain read-back of the access token did not match.")


def fallback_recovery_dir() -> Path:
    """Where a recovery file goes when ``state_dir`` is unwritable: the per-user temp folder
    (a per-process scratch folder in tests)."""
    from budgetapp.config import in_test_process, scratch_dir_for_tests

    if in_test_process():
        return scratch_dir_for_tests() / "tmp"
    return Path(tempfile.gettempdir()).resolve() / "budgetapp"


def _save_recovery_file(settings: Settings, item_id: str, access_token: str) -> Path | None:
    """Write the recovery file under ``state_dir``, else :func:`fallback_recovery_dir`; never
    raises (``None`` when both failed)."""
    for directory in (settings.state_dir, fallback_recovery_dir()):
        try:
            return write_access_token_recovery_file(
                settings, item_id, access_token, state_dir=directory
            )
        except Exception:
            continue
    return None


def _recovery_dirs(settings: Settings) -> list[Path]:
    return [Path(settings.state_dir) / "recovery", fallback_recovery_dir() / "recovery"]


def unsaved_production_tokens(settings: Settings) -> list[Path]:
    """Recovery files holding a production access token that was never stored in Keychain.

    Each one is a production Item that exists at Plaid; while any is listed, no new production
    link may be offered (:func:`production_link_blockers`). ``./run setup --env production
    --access-token`` stores the token and marks its files ``.saved`` (:func:`mark_recovery_saved`).
    """
    found: list[Path] = []
    for directory in _recovery_dirs(settings):
        try:
            found.extend(sorted(directory.glob("access-token-production-*.txt")))
        except OSError:
            continue
    return found


def mark_recovery_saved(settings: Settings, access_token: str) -> list[Path]:
    """Rename the unsaved recovery files that hold ``access_token`` to ``*.saved`` (call only
    after the token is in Keychain and read back); returns the new paths."""
    renamed: list[Path] = []
    for path in unsaved_production_tokens(settings):
        try:
            if access_token not in path.read_text(encoding="utf-8"):
                continue
            target = path.with_suffix(".saved")
            path.rename(target)
            renamed.append(target)
        except OSError:
            continue
    return renamed


def production_link_blockers(settings: Settings, *, store: SecretStore | None = None) -> list[str]:
    """Every reason, outside the database, not to issue a production new-link token (§7).

    The guard (under ``db.link_lock``) issues a token only when this is empty AND no
    ``plaid_items`` row exists. Reasons: a production access token in Keychain (offer
    "Re-attach", never a new link), an unexpired link-in-progress marker, or an unsaved
    production token in a recovery file. Keychain errors propagate (fail safe).
    """
    reasons: list[str] = []
    if get_access_token("production", store=store):
        reasons.append(
            "A production access token is saved in Keychain: re-attach the existing connection "
            "instead of linking again."
        )
    expires = get_link_in_progress(plaid_env="production", store=store)
    if expires is not None:
        reasons.append(
            "A production link was started and may still finish (the marker expires at "
            f"{expires:%Y-%m-%d %H:%M} UTC); finish or cancel it first."
        )
    for path in unsaved_production_tokens(settings):
        reasons.append(
            f"A production access token is waiting in the recovery file {path}: store it with "
            "./run setup --env production --access-token (it is an existing connection)."
        )
    return reasons


def write_access_token_recovery_file(
    settings: Settings, item_id: str, access_token: str, *, state_dir: Path | None = None
) -> Path:
    """Write a token that could not be saved to Keychain to a 0600 file with instructions
    (under ``<state_dir or settings.state_dir>/recovery``)."""
    stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
    content = (
        "Budget Tracker could not save this Plaid access token to the macOS Keychain.\n"
        "Keep this file private. To store the token, run in Terminal:\n"
        f"    ./run setup --env {settings.plaid_env} --access-token\n"
        "and paste the token below when asked. Then delete this file.\n"
        "Consider keeping a copy of the token in your password manager.\n\n"
        f"environment: {settings.plaid_env}\n"
        f"item_id: {item_id}\n"
        f"access_token: {access_token}\n"
    )
    return write_recovery_file(
        state_dir or settings.state_dir, f"access-token-{settings.plaid_env}-{stamp}", content
    )


def write_recovery_file(state_dir: Path, name: str, content: str) -> Path:
    """Write ``content`` to ``<state_dir>/recovery/<name>.txt`` with mode 0600 (dir 0700).

    Never overwrites: a numeric suffix is added if the name exists.
    """
    directory = Path(state_dir) / "recovery"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    for attempt in range(1000):
        path = directory / (f"{safe}.txt" if attempt == 0 else f"{safe}-{attempt}.txt")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o600)
        return path
    raise SecretStoreError("could not create a recovery file")


# ----------------------------------------------------------------------- link-in-progress marker


def set_link_in_progress(
    *,
    plaid_env: str = "production",
    ttl: timedelta = LINK_TOKEN_LIFETIME,
    store: SecretStore | None = None,
) -> datetime:
    """Record that a new-link token was issued; returns when the marker expires (4 h by default).

    Callers check :func:`get_link_in_progress` and call this under ``db.link_lock`` (§7).
    """
    created = clock.now()
    expires = created + ttl
    payload = json.dumps({"created_at": created.isoformat(), "expires_at": expires.isoformat()})
    set_secret(link_in_progress_key(plaid_env), payload, store=store)
    return expires


def get_link_in_progress(
    *, plaid_env: str = "production", store: SecretStore | None = None
) -> datetime | None:
    """The expiry of an unexpired link-in-progress marker, or ``None``.

    An unreadable marker counts as present for its full lifetime from now (fail safe: it blocks a
    second link rather than allowing one).
    """
    raw = (store or get_store()).get(link_in_progress_key(plaid_env))
    if not raw:
        return None
    try:
        expires = datetime.fromisoformat(json.loads(raw)["expires_at"])
    except ValueError, KeyError, TypeError:
        return clock.now() + LINK_TOKEN_LIFETIME
    return expires if expires > clock.now() else None


def clear_link_in_progress(
    *, plaid_env: str = "production", store: SecretStore | None = None
) -> None:
    """Remove the marker (after a successful exchange, or when the user cancels Link)."""
    (store or get_store()).delete(link_in_progress_key(plaid_env))


# --------------------------------------------------------------------------- unattended mode


_unattended = False


def enable_unattended_mode() -> bool:
    """Disable Keychain UI for this process (launchd agents call this at start).

    A Keychain item that would need a dialog then fails fast with -25308/-25293, which
    :class:`KeychainStore` raises as :class:`KeychainApprovalNeeded`, instead of hanging on an
    invisible prompt. Returns ``True`` when the call succeeded (macOS only).
    """
    global _unattended
    if sys.platform != "darwin":
        return False
    try:
        library = ctypes.util.find_library("Security")
        if not library:
            return False
        security = ctypes.CDLL(library)
        func = security.SecKeychainSetUserInteractionAllowed
        func.argtypes = [ctypes.c_bool]
        func.restype = ctypes.c_int32
        status = func(False)
    except OSError, AttributeError:
        return False
    _unattended = status == 0
    return _unattended


def is_unattended() -> bool:
    return _unattended


def keychain_approval_message() -> tuple[str, str]:
    """``(title, message)`` for the Keychain-approval notification and error page."""
    return (
        "Budget Tracker needs Keychain approval",
        f"Open Terminal, go to the budgetapp folder and run {KEYCHAIN_FIX_COMMAND}, then click "
        "Always Allow for each item.",
    )


# --------------------------------------------------------------------------- daily markers


def claim_daily_marker(state_dir: Path, key: str, *, today: date | None = None) -> bool:
    """Once-a-day de-duplication without the database (e.g. Keychain or backup notifications).

    Returns ``True`` the first time it is called for ``key`` on a given day, ``False`` on later
    calls that day — also across processes racing at the same moment (the sync and backup agents
    start together at login): the claim is an exclusive create of the 0600 file
    ``<state_dir>/markers/<key>.<YYYY-MM-DD>``. Older days' files for ``key`` are removed.
    """
    day = (today or clock.today()).isoformat()
    directory = Path(state_dir) / "markers"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    path = directory / f"{safe}.{day}"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    stale = re.compile(re.escape(safe) + r"\.\d{4}-\d{2}-\d{2}")
    for other in directory.iterdir():
        if other.name != path.name and stale.fullmatch(other.name):
            with contextlib.suppress(OSError):
                other.unlink()
    return True


@contextlib.contextmanager
def memory_store(values: dict[str, str] | None = None) -> Iterator[MemoryStore]:
    """Use a fresh :class:`MemoryStore` inside a ``with`` block (handy outside pytest fixtures)."""
    store = MemoryStore(dict(values or {}))
    with use_store(store):
        yield store
