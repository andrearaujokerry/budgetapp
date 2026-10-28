"""``setup`` — one-time (and idempotent) setup (ARCHITECTURE §2, §3; MNT-1).

1. Private folders (0700) under ``~/Library/Application Support/budgetapp`` and ``~/Library/Logs``.
2. The dedicated PostgreSQL 18 cluster (created if missing) started on 127.0.0.1:5434.
3. Roles and databases, with generated passwords saved in the login Keychain.
4. Time Machine exclusion of the cluster's data directory.
5. The Sandbox database migrated to the latest schema; the production database only the first
   time (while it is still empty). After that production is upgraded ONLY by the guarded upgrade
   of the installed copy (``./run live`` / ``./run install-agents``: backup, sync lock, migrations
   from the new install — ARCHITECTURE §2), never from whatever checkout runs setup.
6. Unless ``--skip-plaid``: the Plaid client ID, Sandbox secret and optional Production secret,
   typed with hidden input (never echoed, never on a command line) and saved in Keychain.

``--keys-only`` does only step 6. ``--env production --access-token`` re-enters a saved production
access token (for example from your password manager), verified with ``/item/get`` first.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
from collections.abc import Callable
from typing import Any

from budgetapp import pgcluster, secrets
from budgetapp.commands import add_command
from budgetapp.config import Settings, load_settings
from budgetapp.db import code_head, current_revision, make_engine, migrate_env

KEYS_INTRO = """\
Plaid keys: copy them from https://dashboard.plaid.com/developers/keys
Typing is hidden and nothing is shown or logged.
Press Enter to keep a value that is already saved."""

NEXT_STEPS = """\
Next steps:
  1. Plaid keys (if you skipped them):     ./run setup --keys-only
  2. Allow notifications (one time):       ./run notify-test
  3. Start the Sandbox app:                ./run      → http://127.0.0.1:8766
Your real Amex is linked later, once, from the production app (./run live)."""


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "setup",
        handler=run,
        help="create the database cluster, roles and databases; save Plaid keys in Keychain",
        description=__doc__,
    )
    parser.add_argument("--skip-plaid", action="store_true", help="don't ask for the Plaid keys")
    parser.add_argument("--keys-only", action="store_true", help="only enter or change Plaid keys")
    parser.add_argument(
        "--access-token",
        action="store_true",
        help="production only: re-enter a saved Plaid access token (verified with Plaid first)",
    )


def _ok(message: str) -> None:
    print(f"  ✓ {message}")


def _private_dir(path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def prepare_folders(settings: Settings) -> None:
    """Create every app folder with mode 0700."""
    for path in (
        settings.data_dir,
        settings.logs_dir,
        settings.state_dir,
        settings.pg_socket_dir,
        settings.data_dir / "backups",
        settings.data_dir / "backups" / "sandbox",
        settings.data_dir / "backups" / "production",
    ):
        _private_dir(path)


def setup_database(settings: Settings) -> None:
    """Steps 1–5 (idempotent)."""
    prepare_folders(settings)
    _ok(f"Private folders ready in {settings.data_dir}")
    created = pgcluster.init_cluster(settings)
    _ok(("Created" if created else "Found") + f" the PostgreSQL cluster in {settings.pg_data_dir}")
    _ok(f"PostgreSQL on 127.0.0.1:{settings.pg_port}: {pgcluster.start(settings)}")
    actions = pgcluster.ensure_roles_and_databases(settings)
    _ok("Roles and databases: " + ("; ".join(actions) if actions else "already in place"))
    if pgcluster.exclude_from_time_machine(settings.pg_data_dir):
        _ok("Time Machine skips the database folder (a live copy is not a backup)")
    else:
        print(
            "  ! Could not exclude the database folder from Time Machine; ./run doctor checks this."
        )
    for env in ("sandbox", "production"):
        _ok(migrate_database(load_settings(env)))


def migrate_database(settings: Settings) -> str:
    """Migrate one environment's database for setup; returns a line for the user.

    Sandbox: always to head. Production: only while it is empty (the first setup); a production
    database that has a schema is reported, never upgraded here (ARCHITECTURE §2).
    """
    name = f"{settings.env.capitalize()} database {settings.db_name}"
    if settings.is_production:
        engine = make_engine(settings)
        try:
            current = current_revision(engine)
        finally:
            engine.dispose()
        if current is not None:
            head = code_head()
            state = "up to date" if current == head else f"at {current} (this code has {head})"
            return (
                f"{name}: {state}. Setup never upgrades production; ./run live does, after a backup"
            )
    before, after = migrate_env(settings)
    change = "already up to date" if before == after else f"{before or 'empty'} → {after}"
    return f"{name}: {change}"


def check_key_shape(value: str, kind: str) -> tuple[bool, str | None]:
    """``(fatal, message)`` for a typed Plaid key. Unusual shapes ask for confirmation only."""
    if any(ch.isspace() for ch in value):
        return True, "That value contains spaces; copy the key again."
    if re.match(r"^(access|public|link)-", value):
        return True, "That is a Plaid token, not an API key."
    if len(value) < 10:
        return True, "That is too short to be a Plaid key."
    if kind == "client_id" and not re.fullmatch(r"[0-9a-f]{24}", value):
        return False, "Plaid client IDs are usually 24 hexadecimal characters."
    if kind == "secret" and not re.fullmatch(r"[0-9a-f]{30}", value):
        return False, "Plaid secrets are usually 30 hexadecimal characters."
    return False, None


def prompt_plaid_keys(
    *,
    input_fn: Callable[[str], str] = getpass.getpass,
    confirm_fn: Callable[[str], str] = input,
    store: secrets.SecretStore | None = None,
) -> bool:
    """Ask for the Plaid keys with hidden input and save them in Keychain. False if one failed."""
    print(KEYS_INTRO)
    fields = (
        ("Plaid client ID", secrets.PLAID_CLIENT_ID, "client_id", True),
        ("Plaid Sandbox secret", secrets.plaid_secret_key("sandbox"), "secret", True),
        (
            "Plaid Production secret (optional, Enter to skip)",
            secrets.plaid_secret_key("production"),
            "secret",
            False,
        ),
    )
    all_ok = True
    for label, account, kind, required in fields:
        existing = secrets.get_secret(account, store=store)
        prompt = f"{label}{' [saved; Enter keeps it]' if existing else ''}: "
        for _attempt in range(3):
            value = input_fn(prompt).strip()
            if not value:
                if existing:
                    print("    kept the saved value")
                    break
                if not required:
                    print("    skipped")
                    break
                print("    this one is required")
                continue
            fatal, problem = check_key_shape(value, kind)
            if fatal:
                print(f"    {problem}")
                continue
            if problem:
                answer = confirm_fn(f"    {problem} Save it anyway? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    continue
            if value == existing:
                print("    unchanged")
            else:
                secrets.set_secret(account, value, store=store)
                print("    saved in Keychain")
            break
        else:
            print(f"    {label}: not saved")
            all_ok = False
    return all_ok


def enter_access_token(
    settings: Settings,
    *,
    input_fn: Callable[[str], str] = getpass.getpass,
    gateway: Any = None,
    store: secrets.SecretStore | None = None,
) -> int:
    """Re-enter a saved production access token after verifying it with ``/item/get``."""
    from budgetapp.plaid_gateway import (
        PlaidError,
        PlaidTransportError,
        RealPlaidGateway,
        check_token_environment,
    )

    if not settings.is_production:
        print("--access-token is for production: ./run setup --env production --access-token")
        return 2
    token = input_fn("Paste the production access token (typing is hidden): ").strip()
    if not token or any(ch.isspace() for ch in token):
        print("No token entered.")
        return 1
    secrets.register_secret(token)
    try:
        check_token_environment(token, "production")
    except Exception as exc:
        print(str(exc))
        return 1
    existing = secrets.get_access_token("production", store=store)
    if existing == token:
        print("This token is already saved in Keychain. Nothing to do.")
        _mark_recovery_files(settings, token)
        return 0
    if existing:
        print(
            "A different production access token is already saved; it is never replaced "
            "automatically. Remove it in Keychain Access only if you are sure it is dead."
        )
        return 1
    gateway = gateway or RealPlaidGateway.from_settings(settings, store=store)
    try:
        body = gateway.get_item(token)
    except PlaidError as exc:
        print(f"Plaid did not accept this token ({exc.error_code}): {exc.display_message or ''}")
        return 1
    except PlaidTransportError as exc:
        print(f"Could not reach Plaid: {exc}. Check the connection and try again.")
        return 1
    item = body.get("item") or {}
    item_id = item.get("item_id")
    if not item_id:
        print("Plaid's answer did not include an Item id; nothing was saved.")
        return 1
    secrets.store_access_token(settings, item_id, token, store=store)
    institution = (
        item.get("institution_name") or item.get("institution_id") or "unknown institution"
    )
    print(f"Saved the access token for Item {item_id} ({institution}) in Keychain.")
    _mark_recovery_files(settings, token)
    print(
        "If the app has no connection yet, open ./run live → Connection → "
        "“Re-attach existing connection”. Never link a new one."
    )
    return 0


def _mark_recovery_files(settings: Settings, token: str) -> None:
    """The token is in Keychain now: its recovery files no longer block a link (``*.saved``)."""
    for path in secrets.mark_recovery_saved(settings, token):
        print(
            f"Marked the recovery file as saved: {path}. Delete it once the token is also in "
            "your password manager."
        )


def run(args: argparse.Namespace, settings: Settings) -> int:
    if args.access_token:
        return enter_access_token(settings)
    if args.keys_only:
        return 0 if prompt_plaid_keys() else 1
    print("Setting up budgetapp")
    setup_database(settings)
    if not args.skip_plaid:
        print()
        if not prompt_plaid_keys():
            print("Some Plaid keys were not saved; run ./run setup --keys-only to try again.")
    print()
    print(NEXT_STEPS)
    return 0
