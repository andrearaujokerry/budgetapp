"""``sync`` — sync every linked Plaid Item now (ARCHITECTURE §8, §10; SYNC-2, SYNC-5, REL-1).

``./run sync [--if-due] [--min-age-hours H] [--unattended] [--item ID]``

* ``--if-due`` skips Items whose last successful sync is newer than ``--min-age-hours`` (5): the
  launchd agent runs at 00:15, 06:15, 12:15 and 18:15 and at login.
* ``--unattended`` (launchd): Keychain dialogs are turned off for this process. When Keychain
  needs the user's approval the command notifies once a day (a marker file: the database password
  may be the item that failed) and exits 75. A database that isn't up yet ends the run quietly
  (exit 75, no alert): the next scheduled run tries again. A production database that needs an
  upgrade is announced once a day, also with exit 75.
* Exit status: 0 when every Item synced (or was skipped or not due), 1 when a sync failed (the
  failure is recorded on the Item and shown on the Connection page), 2 for a usage error, 75 for
  "try again later".

:class:`Runtime` builds the database session factory, the Plaid gateway and the notifier from the
settings, lazily (no Plaid keys are needed while nothing is linked); tests hand in fakes. The
Sandbox helpers in ``commands/sandbox.py`` use it too.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import db, secrets
from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.logsetup import setup_logging
from budgetapp.models import PlaidItem
from budgetapp.plaid_gateway import PlaidGateway, RealPlaidGateway
from budgetapp.services import sync as sync_service
from budgetapp.services.events import SyncOutcome
from budgetapp.services.notify import Notifier, get_notifier

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
#: EX_TEMPFAIL, "try again later" (Keychain approval, database not ready); = cli.EXIT_KEYCHAIN
EXIT_LATER = 75

KEYCHAIN_MARKER = "sync-keychain-approval"
SCHEMA_MARKER = "sync-schema-upgrade"


def register(subparsers: Any) -> None:
    parser = add_command(subparsers, "sync", handler=run, help="sync every Plaid Item now")
    parser.add_argument(
        "--if-due", action="store_true", help="skip Items synced successfully in the last 5 hours"
    )
    parser.add_argument(
        "--min-age-hours", type=float, default=5.0, help="with --if-due: hours (default 5)"
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="for launchd: never show Keychain prompts (exit 75 when approval is needed)",
    )
    parser.add_argument("--item", type=int, metavar="ID", help="sync only this Item (database id)")


# --------------------------------------------------------------------------- runtime


class Runtime:
    """What a sync command needs, built from ``settings`` on first use (or handed in by tests)."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        gateway: PlaidGateway | None = None,
        store: secrets.SecretStore | None = None,
        notifier: Notifier | None = None,
        sleep: Callable[[float], None] = time.sleep,
        out: Callable[[str], None] = print,
    ) -> None:
        self.settings = settings
        self.store = store if store is not None else secrets.get_store()
        self.notifier = notifier if notifier is not None else get_notifier(settings)
        self.sleep = sleep
        self.out = out
        self._session_factory = session_factory
        self._gateway = gateway
        self._engine: Engine | None = None

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """The environment's database (reading its password from Keychain the first time)."""
        if self._session_factory is None:
            self._engine = db.make_engine(self.settings)
            self._session_factory = db.make_session_factory(self._engine)
        return self._session_factory

    @property
    def gateway(self) -> PlaidGateway:
        """The Plaid gateway (reads the Plaid keys; ``MissingSecretError`` until they're saved)."""
        if self._gateway is None:
            self._gateway = RealPlaidGateway.from_settings(self.settings, store=self.store)
        return self._gateway

    def ensure_database(self) -> None:
        """Wait for the database (up to ~2 minutes), then the schema guard."""
        sync_service.ensure_database(self.settings, self.session_factory.kw["bind"])

    def item_names(self) -> dict[int, str]:
        """Every Item's database id → a short name, in id order."""
        with self.session_factory() as s:
            rows = s.execute(
                select(PlaidItem.id, PlaidItem.institution_name).order_by(PlaidItem.id)
            ).all()
        return {row.id: row.institution_name or f"Item {row.id}" for row in rows}

    def plaid_item_ids(self) -> dict[int, str]:
        """Every Item's database id → its Plaid ``item_id``."""
        with self.session_factory() as s:
            return {
                row.id: row.plaid_item_id
                for row in s.execute(select(PlaidItem.id, PlaidItem.plaid_item_id))
            }

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


# --------------------------------------------------------------------------- reporting


def describe_outcome(name: str, outcome: SyncOutcome) -> str:
    """One line about one sync run, for the terminal and the sync log."""
    if outcome.status == "skipped":
        return f"{name}: skipped (another sync of this connection was running)."
    if outcome.status == "not_ready":
        return f"{name}: Plaid is still preparing the first transactions; nothing to add yet."
    if outcome.status == "error":
        code = outcome.error_code or "error"
        detail = outcome.display_message or sync_service.describe_error(
            outcome.error_code, outcome.error_type
        )
        line = f"{name}: failed ({code})."
        if detail:
            line += f" {detail}"
        if outcome.needs_reconnect:
            line += " Open the Connection page and click Reconnect."
        return line
    counts = (
        f"{outcome.added:,} new, {outcome.modified:,} changed, {outcome.removed:,} removed"
        if outcome.changed
        else "no changes"
    )
    line = f"{name}: {counts}."
    if outcome.history_completed_now:
        line += " Plaid has now sent the full history."
    elif outcome.update_status and outcome.update_status != sync_service.HISTORICAL_UPDATE_COMPLETE:
        line += " Plaid is still loading older history."
    return line


def report(rt: Runtime, names: dict[int, str], outcomes: tuple[SyncOutcome, ...]) -> None:
    for outcome in outcomes:
        line = describe_outcome(names.get(outcome.item_id, f"Item {outcome.item_id}"), outcome)
        log.info("%s", line)
        rt.out(line)


# --------------------------------------------------------------------------- "try later" paths


def keychain_approval(rt: Runtime, exc: secrets.KeychainApprovalNeeded, unattended: bool) -> int:
    """Keychain needs the user: notify once a day when unattended; always exit 75."""
    title, message = secrets.keychain_approval_message()
    log.warning("Sync stopped: Keychain approval needed for %r", exc.account)
    if unattended:
        try:
            first_today = secrets.claim_daily_marker(rt.settings.state_dir, KEYCHAIN_MARKER)
        except OSError:
            first_today = True
        if first_today:
            rt.notifier.notify(title, message)
    else:
        rt.out(f"{title}. {message}")
    return EXIT_LATER


def database_not_ready(rt: Runtime, exc: Exception, unattended: bool) -> int:
    """ "Not ready" means "try later", never an alert (ARCHITECTURE §8)."""
    log.info("Sync skipped: the database is not ready (%s)", exc)
    if unattended:
        return EXIT_LATER
    rt.out("The database isn't responding. Start it with: ./run db start")
    return EXIT_FAILED


def schema_mismatch(rt: Runtime, exc: db.SchemaMismatch, unattended: bool) -> int:
    """The database is at another schema: sync never migrates (ARCHITECTURE §2)."""
    settings = rt.settings
    fix = "./run live" if settings.is_production else "./run migrate"
    log.warning("Sync skipped: %s", exc)
    if unattended:
        try:
            first_today = secrets.claim_daily_marker(settings.state_dir, SCHEMA_MARKER)
        except OSError:
            first_today = True
        if first_today and settings.is_production:
            rt.notifier.notify(
                "Budget Tracker needs an upgrade",
                f"Syncing is paused until you run {fix} in Terminal (it takes a backup first).",
            )
        return EXIT_LATER
    rt.out(f"The database needs an upgrade before syncing. Run: {fix}")
    return EXIT_FAILED


def missing_plaid_keys(rt: Runtime, exc: secrets.MissingSecretError) -> int:
    log.warning("Sync stopped: %s", exc)
    rt.out("The Plaid keys aren't saved yet. Save them with: ./run setup --keys-only")
    return EXIT_FAILED


# --------------------------------------------------------------------------- the command


def sync_command(args: argparse.Namespace, rt: Runtime) -> int:
    """``./run sync`` with an explicit :class:`Runtime` (the testable part of :func:`run`)."""
    unattended = bool(getattr(args, "unattended", False))
    if unattended:
        secrets.enable_unattended_mode()
    min_age = getattr(args, "min_age_hours", 5.0)
    if min_age is None or min_age < 0:
        rt.out("error: --min-age-hours must be 0 or more.")
        return EXIT_USAGE
    item_id = getattr(args, "item", None)
    try:
        rt.ensure_database()
        names = rt.item_names()
        if not names:
            rt.out("Nothing is linked yet. Link your Amex card on the Connection page.")
            return EXIT_OK
        if item_id is not None and item_id not in names:
            known = ", ".join(f"{i} ({n})" for i, n in names.items())
            rt.out(f"error: there is no Item {item_id}. Linked Items: {known}.")
            return EXIT_USAGE
        result = sync_service.sync_all(
            rt.session_factory,
            rt.gateway,
            rt.store,
            rt.settings,
            trigger="schedule" if unattended else "cli",
            notifier=rt.notifier,
            if_due=bool(getattr(args, "if_due", False)),
            min_age=dt.timedelta(hours=min_age),
            item_ids=None if item_id is None else [item_id],
            sleep=rt.sleep,
        )
    except secrets.KeychainApprovalNeeded as exc:
        return keychain_approval(rt, exc, unattended)
    except db.DatabaseUnavailable as exc:
        return database_not_ready(rt, exc, unattended)
    except db.SchemaMismatch as exc:
        return schema_mismatch(rt, exc, unattended)
    except secrets.MissingSecretError as exc:
        return missing_plaid_keys(rt, exc)
    except Exception:
        # sync_item already recorded it (INTERNAL on the Item, the run, the alert hook).
        log.exception("The sync failed")
        rt.out("error: the sync failed unexpectedly; the details are in the sync log.")
        return EXIT_FAILED
    report(rt, names, result.outcomes)
    for skipped in result.not_due:
        line = f"{names.get(skipped, f'Item {skipped}')}: synced recently; not due yet."
        log.info("%s", line)
        rt.out(line)
    return EXIT_OK if result.ok else EXIT_FAILED


def run(args: argparse.Namespace, settings: Settings) -> int:
    verbose = bool(getattr(args, "verbose", False))
    setup_logging(
        settings,
        log_name=None if settings.is_test else f"sync-{settings.env}",
        console_level=logging.INFO if verbose else logging.WARNING,
    )
    rt = Runtime(settings)
    try:
        return sync_command(args, rt)
    finally:
        rt.close()
