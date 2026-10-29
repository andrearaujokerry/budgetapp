"""``backup`` and ``restore`` (REL-2, ARCHITECTURE §10; the details are in ``services.backup``).

``./run backup`` dumps this environment's database (``--env production`` for real data) into
``~/Library/Application Support/budgetapp/backups/<env>/``, verifies the dump and keeps the newest
30. The nightly launchd agent runs ``backup --env production --if-due --unattended``: it waits up
to two minutes for the database, never shows a Keychain dialog, and notifies once a day when a
backup fails or the newest one is over 26 hours old.

``./run restore FILE`` replaces this environment's database with a backup of that same database.
It shows what it will do and asks you to type the environment's name; production needs an
explicit ``--env production``. The current data is backed up first, and the restore runs in one
transaction, so it either completes or changes nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError

from budgetapp import clock, secrets
from budgetapp.commands import add_command
from budgetapp.config import ConfigError, Settings
from budgetapp.db import wait_for_db
from budgetapp.services import agents
from budgetapp.services import backup as backups
from budgetapp.services.notify import get_notifier

#: EX_TEMPFAIL: Keychain approval needed, or the database never came up (try again later).
EXIT_KEYCHAIN = EXIT_TEMPFAIL = 75
#: Replaced in tests; reads the typed confirmation.
prompt = input


def register(subparsers: Any) -> None:
    backup = add_command(
        subparsers,
        "backup",
        handler=run_backup,
        help="back up this environment's database now (verified; keeps the newest 30)",
        description=__doc__,
    )
    backup.add_argument(
        "--if-due", action="store_true", help="skip when the newest verified dump is recent"
    )
    backup.add_argument(
        "--min-age-hours", type=float, default=20.0, help="with --if-due: hours (default 20)"
    )
    backup.add_argument("--keep", type=int, default=30, help="dumps to keep (default 30)")
    backup.add_argument(
        "--unattended", action="store_true", help="for launchd: never show Keychain prompts"
    )
    restore = add_command(
        subparsers,
        "restore",
        handler=run_restore,
        help="restore a backup (asks for confirmation; production needs --env production)",
        description=__doc__,
    )
    restore.add_argument("file", metavar="FILE", help="a .dump file made by ./run backup")
    restore.add_argument(
        "--force-disconnect", action="store_true", help="disconnect running app processes first"
    )


# --------------------------------------------------------------------------- backup


def _size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"  # pragma: no cover


def _age(delta: dt.timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.0f} days"


def _say(unattended: bool, message: str, *, error: bool = False) -> None:
    """Print a line; under launchd (``--unattended``) with a timestamp, for the log file."""
    if unattended:
        message = f"{clock.now().astimezone():%Y-%m-%d %H:%M:%S} {message}"
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def _notify_keychain(settings: Settings) -> None:
    if secrets.claim_daily_marker(settings.state_dir, f"keychain-approval-{settings.env}"):
        title, message = secrets.keychain_approval_message()
        get_notifier(settings).notify(title, message)


def _alert_if_stale(settings: Settings) -> None:
    stale, newest = backups.backup_health(settings)
    if stale:
        backups.notify_backup_problem(
            settings, get_notifier(settings), backups.stale_message(settings, newest)
        )


def _failed(settings: Settings, unattended: bool, exc: Exception) -> int:
    """Report a failed backup; under launchd also notify (once a day)."""
    _say(unattended, f"error: the {settings.env} backup failed: {exc}", error=True)
    if unattended:
        backups.notify_backup_problem(
            settings,
            get_notifier(settings),
            f"The {settings.env} backup failed: {exc} Run ./run doctor --env {settings.env}.",
        )
    return 1


def run_backup(args: argparse.Namespace, settings: Settings) -> int:
    unattended = bool(args.unattended)
    if args.keep < 1:
        _say(unattended, "error: --keep must be at least 1", error=True)
        return 2
    if unattended:
        secrets.enable_unattended_mode()
    try:
        ready = wait_for_db(settings, timeout=agents.DB_WAIT if unattended else 15)
    except ConfigError as exc:  # PostgresNotFound: the PostgreSQL 18 programs are missing
        return _failed(settings, unattended, exc)
    if not ready:
        if not unattended:
            _say(
                unattended,
                "error: the database isn't running, so nothing was backed up. Start it with "
                "./run db start",
                error=True,
            )
            return 1
        # Not ready is "try later", not an alert by itself; a backup over 26 h old is.
        _say(unattended, "The database didn't come up within 2 minutes; the next run will retry.")
        _alert_if_stale(settings)
        return EXIT_TEMPFAIL
    return _backup_now(args, settings, unattended)


def _backup_now(args: argparse.Namespace, settings: Settings, unattended: bool) -> int:
    try:
        result = backups.run_backup(
            settings,
            keep=args.keep,
            if_due=args.if_due,
            min_age=dt.timedelta(hours=args.min_age_hours),
        )
    except secrets.KeychainApprovalNeeded as exc:
        _say(unattended, f"error: {exc}", error=True)
        if unattended:
            _notify_keychain(settings)
        return EXIT_KEYCHAIN
    except (backups.BackupError, secrets.SecretStoreError, ConfigError) as exc:
        return _failed(settings, unattended, exc)  # ConfigError: PostgresNotFound
    if not result.created:
        assert result.newest is not None
        _say(
            unattended,
            f"Skipped: the newest backup ({result.newest.name}) is "
            f"{_age(result.newest.age())} old, under {args.min_age_hours:g} h.",
        )
        return 0
    assert result.path is not None
    _say(
        unattended,
        f"Backed up {settings.db_name} to {result.path} ({_size(result.size)}, verified with a "
        f"full read, {result.seconds:.1f} s).",
    )
    removed = f"; removed {len(result.removed)} older" if result.removed else ""
    _say(unattended, f"{result.kept} backup(s) kept (newest {args.keep}){removed}.")
    return 0


# --------------------------------------------------------------------------- restore


def env_was_explicit(argv: list[str] | None = None) -> bool:
    """True unless production came only from ``$BUDGETAPP_ENV`` (the launchd agents set it)."""
    if os.environ.get("BUDGETAPP_ENV") != "production":
        return True
    argv = sys.argv[1:] if argv is None else argv
    return any(arg == "--env" or arg.startswith("--env=") for arg in argv)


def _plan_text(settings: Settings, plan: backups.RestorePlan) -> list[str]:
    target = f"the {settings.env.upper()} database {settings.db_name}"
    lines = [
        f"Restore this backup into {target}?",
        f"  File:    {plan.path}",
        f"  Made:    {plan.info.created or 'unknown'} by pg_dump {plan.info.dumped_by or '?'} "
        f"({_size(plan.size)}, verified with a full read)",
        f"  Schema:  {plan.revision}"
        + (
            ""
            if not plan.needs_upgrade
            else f" (this code needs {plan.code_head}: you upgrade after)"
        ),
        "",
        f"Everything in {settings.db_name} is replaced by the backup. The current data is backed "
        "up first, and the restore runs in one transaction (it completes or changes nothing).",
    ]
    if settings.is_production:
        lines.append("The web, sync and backup agents stop during the restore and start again.")
    return lines


def run_restore(args: argparse.Namespace, settings: Settings) -> int:
    path = Path(args.file).expanduser()
    if settings.is_production and not env_was_explicit():
        print(
            "error: restoring PRODUCTION needs an explicit --env production on the command line "
            "(BUDGETAPP_ENV alone is not enough).",
            file=sys.stderr,
        )
        return 2
    if not wait_for_db(settings, timeout=30):
        print("error: the database isn't running. Start it with ./run db start", file=sys.stderr)
        return 1
    try:
        plan = backups.inspect_backup(settings, path)
    except backups.RestoreRefused as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 1
    print("\n".join(_plan_text(settings, plan)))
    try:
        answer = prompt(f'\nType "{settings.env}" to restore, anything else to cancel: ')
    except EOFError:
        answer = ""
    if answer.strip() != settings.env:
        print("Cancelled; nothing was changed.")
        return 1
    control = None
    if settings.is_production:
        layout = agents.default_layout(settings)
        control = agents.AgentController(layout, agents.Launchctl(layout.uid))
    try:
        result = backups.restore(
            settings,
            plan,
            force_disconnect=args.force_disconnect,
            agents=control,
            say=lambda line: print(f"  {line}", flush=True),
        )
    except backups.RestoreRefused as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 1
    except backups.RestoreFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except backups.BackupError as exc:
        print(
            f"Not restored: the backup of the current data failed ({exc}). Nothing was changed.",
            file=sys.stderr,
        )
        return 1
    except OperationalError as exc:
        detail = str(exc.orig or exc).strip().splitlines()[0]
        print(f"error: the database connection failed: {detail}", file=sys.stderr)
        return 1
    counts = ", ".join(
        f"{count:,} {table.replace('_', ' ')}" for table, count in result.counts.items()
    )
    print(f"Done in {result.seconds:.1f} s. The database now holds {counts}.")
    print(result.schema_message)
    if result.pre_restore is not None and result.pre_restore.path is not None:
        print(
            f"To undo, restore the copy taken first: ./run restore {result.pre_restore.path}"
            + (" --env production" if settings.is_production else "")
        )
    return 0
