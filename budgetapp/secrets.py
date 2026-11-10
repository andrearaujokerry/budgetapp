"""Secrets in the macOS Keychain (ARCHITECTURE §3, SEC-2).

Everything secret is read and written through this module only, under the Keychain service
``budgetapp``:

========================================  ==========================================================
Keychain account                          Value
========================================  ==========================================================
``plaid.client_id``                       Plaid client ID (the same for every Plaid environment)
``plaid.secret.sandbox``                  Plaid Sandbox secret
``plaid.secret.production``               Plaid Production secret
``plaid.access_token.production.<item>``  production access tokens, one per Item (wave 3, C6)
``plaid.item_ids.production``             JSON list of production item ids (keyring cannot list)
``plaid.link_in_progress.production``     marker written when a production new-link token is issued
``plaid.access_token.sandbox.<item>``     Sandbox access tokens, one per Item
``plaid.item_ids.sandbox``                JSON list of Sandbox item ids
``db.password.<role>``                    generated passwords of budgetapp_dev / budgetapp_live
========================================  ==========================================================

``plaid.access_token.production`` (:data:`LEGACY_PRODUCTION_TOKEN`) is the v1 name of the one
production token. :func:`move_legacy_production_token` moves it once to its Item's own key (write,
read back and compare, then delete the old key); on any doubt the old key stays and keeps working.
:func:`legacy_token_state` says where it stands; a copy left because the old key couldn't be
deleted is deleted later by :func:`delete_legacy_copy` (every production use of a token tries).

Rules: values are never written to the database, files (except the 0600 recovery file below), logs,
exceptions, process arguments or templates; every value read is registered with the log redactor;
tests use :class:`MemoryStore` (``use_store``), never the real Keychain (the live Sandbox tests may
read only the Sandbox items).

Production link guard (ARCHITECTURE §7, Principle 4 "One Plaid link per bank login, ever"):
:func:`production_link_blockers` lists every reason outside the database not to issue a new
production link — an unexpired link-in-progress marker (one link at a time) or a production token
waiting in a recovery file. ``services.linking`` adds the reasons that need the database (tokens
without their Item row, the v1 token, the 10 Items).
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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from budgetapp import clock
from budgetapp.logsetup import register_secret

if TYPE_CHECKING:
    from budgetapp.config import Settings

SERVICE = "budgetapp"

PLAID_CLIENT_ID = "plaid.client_id"
#: The v1 name of THE production access token (one production Item). Never written any more:
#: :func:`move_legacy_production_token` moves it to ``plaid.access_token.production.<item>``.
LEGACY_PRODUCTION_TOKEN = "plaid.access_token.production"
SANDBOX_ITEM_INDEX = "plaid.item_ids.sandbox"
PRODUCTION_ITEM_INDEX = "plaid.item_ids.production"
LINK_TOKEN_LIFETIME = timedelta(hours=4)
_ITEM_ID = re.compile(r"[A-Za-z0-9_-]+")

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


def access_token_key(plaid_env: str, item_id: str) -> str:
    """Keychain account of one Item's access token, in either environment (C6)."""
    _check_plaid_env(plaid_env)
    if not item_id or not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
        raise ValueError("invalid Plaid item id")
    return f"plaid.access_token.{plaid_env}.{item_id}"


def sandbox_access_token_key(item_id: str) -> str:
    """Keychain account of one Sandbox Item's access token."""
    return access_token_key("sandbox", item_id)


def production_access_token_key(item_id: str) -> str:
    """Keychain account of one production Item's access token."""
    return access_token_key("production", item_id)


def item_index_key(plaid_env: str) -> str:
    """Keychain account of the JSON list of an environment's item ids."""
    _check_plaid_env(plaid_env)
    return PRODUCTION_ITEM_INDEX if plaid_env == "production" else SANDBOX_ITEM_INDEX


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


class MissingSecretFix(NamedTuple):
    """What to tell the user about a :class:`MissingSecretError` (:func:`missing_secret_fix`)."""

    problem: str  # "The Plaid keys aren't saved yet."
    action: str  # "Save them", followed by "with: <command>" or "in Terminal …"
    command: str  # "./run setup --keys-only"


def missing_secret_fix(account: str) -> MissingSecretFix:
    """The fix for a missing Keychain item, for ``./run sync``, the Sandbox helpers and the web
    app's "Setup isn't finished" page: the Plaid keys are saved by ``./run setup --keys-only``; a
    database password only by ``./run setup`` (it creates the roles and their passwords)."""
    if account == PLAID_CLIENT_ID or account.startswith("plaid.secret."):
        return MissingSecretFix(
            "The Plaid keys aren't saved yet.", "Save them", "./run setup --keys-only"
        )
    if account.startswith("db.password."):
        return MissingSecretFix(
            "The database password isn't in your Keychain.", "Create it", "./run setup"
        )
    return MissingSecretFix(
        "A key the app needs is missing from your Keychain.", "Check", "./run doctor"
    )


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


def item_ids(plaid_env: str, *, store: SecretStore | None = None) -> list[str]:
    """Item ids of every access token stored for an environment, in the order they were added
    (the Keychain cannot list accounts, so an index is kept beside the tokens)."""
    raw = get_secret(item_index_key(plaid_env), store=store)
    if not raw:
        return []
    try:
        ids = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and _ITEM_ID.fullmatch(i)]


def sandbox_item_ids(*, store: SecretStore | None = None) -> list[str]:
    """Item ids of every Sandbox access token stored."""
    return item_ids("sandbox", store=store)


def production_item_ids(*, store: SecretStore | None = None) -> list[str]:
    """Item ids of every production access token stored under its own key (C6). Never shrinks:
    each id is a production Item the app created or re-attached, so it counts toward the 10."""
    return item_ids("production", store=store)


def _add_item_id(plaid_env: str, item_id: str, store: SecretStore) -> None:
    ids = item_ids(plaid_env, store=store)
    if item_id not in ids:
        ids.append(item_id)
        store.set(item_index_key(plaid_env), json.dumps(ids))


def remove_sandbox_item(item_id: str, *, store: SecretStore | None = None) -> None:
    """Delete a Sandbox Item's token and its index entry (Sandbox only; never production)."""
    store = store or get_store()
    store.delete(sandbox_access_token_key(item_id))
    ids = [i for i in sandbox_item_ids(store=store) if i != item_id]
    store.set(SANDBOX_ITEM_INDEX, json.dumps(ids))


def get_access_token(
    plaid_env: str, item_id: str, *, store: SecretStore | None = None
) -> str | None:
    """The access token stored for one Item, or ``None``. Production never falls back to the v1
    key here: ``services.sync.access_token_for`` decides when that one may be used."""
    return get_secret(access_token_key(plaid_env, item_id), store=store)


def legacy_production_token(*, store: SecretStore | None = None) -> str | None:
    """The v1 production token (``plaid.access_token.production``) while it hasn't been moved."""
    return get_secret(LEGACY_PRODUCTION_TOKEN, store=store)


@dataclass(frozen=True, slots=True)
class LegacyTokenMove:
    """What :func:`move_legacy_production_token` did.

    ``status``: ``none`` (there is no v1 token), ``moved`` (it is under its Item's key now; the
    old key is gone, or ``reason`` says it couldn't be deleted yet) or ``kept`` (the old key stays
    and keeps working; ``reason`` says why). Never holds a token.
    """

    status: str
    item_id: str | None = None
    reason: str | None = None


def move_legacy_production_token(
    item_id: str, *, store: SecretStore | None = None
) -> LegacyTokenMove:
    """Move the v1 production token to ``item_id``'s own key (C6), once.

    The caller vouches that the v1 token belongs to ``item_id`` (exactly one production Item
    exists, or Plaid's ``/item/get`` named it). Steps: index entry, write the new key, read it back
    and compare, then delete the old key. On any doubt the old key stays: a failed or mismatched
    write (a bad copy is removed again so it can't shadow the good one), or a DIFFERENT token
    already saved under the new key. Keychain approval errors on the first read propagate."""
    store = store or get_store()
    legacy = store.get(LEGACY_PRODUCTION_TOKEN)
    if not legacy:
        return LegacyTokenMove("none")
    register_secret(legacy)
    key = production_access_token_key(item_id)
    try:
        current = store.get(key)
        if current is None:
            _add_item_id("production", item_id, store)
            store.set(key, legacy)
            if store.get(key) != legacy:
                with contextlib.suppress(Exception):
                    store.delete(key)
                return LegacyTokenMove(
                    "kept", item_id, "Keychain read back something else under the new name"
                )
        elif current != legacy:
            return LegacyTokenMove(
                "kept", item_id, "a different token is already saved under the new name"
            )
        else:
            _add_item_id("production", item_id, store)
    except Exception as exc:  # never lose the working token over a failed copy
        return LegacyTokenMove(
            "kept", item_id, f"Keychain couldn't save it under the new name ({type(exc).__name__})"
        )
    try:
        store.delete(LEGACY_PRODUCTION_TOKEN)
    except Exception as exc:
        return LegacyTokenMove(
            "moved", item_id, f"the old copy couldn't be deleted yet ({type(exc).__name__})"
        )
    return LegacyTokenMove("moved", item_id)


@dataclass(frozen=True, slots=True)
class LegacyTokenState:
    """Where the v1 production token stands (C6). Never holds a token.

    ``state``:

    * ``none``: there is no v1 key.
    * ``copy``: the same token is saved under ``item_id``'s own key (the move wrote and read it
      back; only deleting the old key failed). A leftover: it can't lose its owner, because every
      decision compares values, and :func:`delete_legacy_copy` removes it when Keychain allows.
    * ``movable``: exactly one Item in the database (``item_id``) and no key of its own: the next
      sync or link moves the token there (:func:`move_legacy_production_token`).
    * ``unplaced``: no Item, several, or the one Item has a different token of its own, so the v1
      token may be the only token of a connection the database lost: Re-attach asks Plaid whose
      it is (``/item/get``).
    """

    state: str
    item_id: str | None = None


def legacy_token_state(
    db_item_ids: Sequence[str], *, store: SecretStore | None = None
) -> LegacyTokenState:
    """Classify the v1 token against the production Items: ``db_item_ids`` (the database's, oldest
    first) and the Keychain index. Only reads; Keychain errors propagate (fail safe)."""
    legacy = legacy_production_token(store=store)
    if not legacy:
        return LegacyTokenState("none")
    known = list(dict.fromkeys([*db_item_ids, *production_item_ids(store=store)]))
    own = {item_id: get_access_token("production", item_id, store=store) for item_id in known}
    for item_id, token in own.items():
        if token == legacy:
            return LegacyTokenState("copy", item_id)
    if len(db_item_ids) == 1 and own.get(db_item_ids[0]) is None:
        return LegacyTokenState("movable", db_item_ids[0])
    return LegacyTokenState("unplaced")


def delete_legacy_copy(
    item_id: str, access_token: str, *, store: SecretStore | None = None
) -> LegacyTokenMove:
    """Delete the v1 key when it holds exactly ``access_token``, which the caller has just read
    from ``item_id``'s own key: a copy left by a move whose last step failed (C6). Never raises
    (it is retried by the next caller): ``none`` when there is no such copy (or Keychain didn't
    let it be read), ``moved`` when the old key is gone, ``moved`` with a reason when it still
    couldn't be deleted."""
    store = store or get_store()
    try:
        if store.get(LEGACY_PRODUCTION_TOKEN) != access_token:
            return LegacyTokenMove("none")
    except Exception:
        return LegacyTokenMove("none")
    try:
        store.delete(LEGACY_PRODUCTION_TOKEN)
    except Exception as exc:
        return LegacyTokenMove(
            "moved", item_id, f"the old copy couldn't be deleted yet ({type(exc).__name__})"
        )
    return LegacyTokenMove("moved", item_id)


def store_access_token(
    settings: Settings,
    item_id: str,
    access_token: str,
    *,
    store: SecretStore | None = None,
) -> None:
    """Save an Item's access token exactly once and read it back (ARCHITECTURE §3, §7; C6).

    * Every token has its own key, ``plaid.access_token.<env>.<item id>``, so no token can reach
      another Item. The token is never rewritten: if a different token is already stored under
      the same account, nothing is overwritten and :class:`AccessTokenConflict` is raised (after
      saving the new token to a recovery file, because every production token used a Trial
      connection).
    * If the write or the read-back fails — for ANY reason, including Ctrl-C (re-raised after
      the file is written) or an Item id that can't be part of a Keychain name
      (:func:`access_token_key`) — the token goes to a 0600 recovery file under
      ``settings.state_dir`` (or, if that fails too, under :func:`fallback_recovery_dir`) and
      :class:`TokenNotSavedError` is raised; callers stop and show the path and
      ``./run setup --env production --access-token``. An unsaved production recovery file keeps
      blocking new links (:func:`production_link_blockers`).
    * The Item is added to its environment's index (``plaid.item_ids.<env>``) BEFORE the token is
      written (an index entry without a token is harmless; a token nobody can find is an orphan).
    * Callers clear the link-in-progress marker only after this returns normally.
    """
    if not access_token:
        raise ValueError("an access token is required")
    plaid_env = settings.plaid_env
    store = store or get_store()
    register_secret(access_token)

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
    # so every exception — even KeyboardInterrupt/SystemExit — ends in the recovery file. That
    # starts with the Keychain name: Plaid has created the Item even if its id can't be in one.
    try:
        account = access_token_key(plaid_env, item_id)
    except BaseException as exc:
        error = _recover("Plaid's Item id can't be part of a Keychain name.")
        if not isinstance(exc, Exception):
            raise
        raise error from exc
    try:
        existing = store.get(account)
    except BaseException as exc:
        error = _recover("Keychain could not be read before saving the access token.")
        if not isinstance(exc, Exception):
            raise
        raise error from exc
    if existing is not None:
        if existing == access_token:
            _add_item_id(plaid_env, item_id, store)
            return
        raise _recover(
            f"A different access token is already stored as {account!r}; it was not replaced.",
            AccessTokenConflict,
        )
    try:
        _add_item_id(plaid_env, item_id, store)
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


def recovery_item_ids(settings: Settings) -> set[str]:
    """The production item ids named by unsaved recovery files (each is an Item that exists at
    Plaid, so it counts toward the 10), also an id no Keychain name can hold (only counted here).
    Unreadable files are skipped."""
    found: set[str] = set()
    for path in unsaved_production_tokens(settings):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r"^item_id: (.+)$", text, flags=re.M)
        if match:
            found.add(match.group(1))
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

    The guard (``services.linking``, under ``db.link_lock``) adds the reasons that need the
    database: tokens without their Item row, the v1 token, the 10 Items. Reasons here: an
    unexpired link-in-progress marker (one link at a time), or an unsaved production token in a
    recovery file (a connection that exists: store it, then re-attach). Keychain errors
    propagate (fail safe).
    """
    reasons: list[str] = []
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
    (under ``<state_dir or settings.state_dir>/recovery``), one file per Item and attempt:
    ``access-token-<env>-<item id>-<UTC stamp>.txt``."""
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
        state_dir or settings.state_dir,
        f"access-token-{settings.plaid_env}-{item_id}-{stamp}",
        content,
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
