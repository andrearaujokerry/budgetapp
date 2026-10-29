"""Sandbox-only test helpers (ARCHITECTURE §10; RESEARCH T3). They refuse to run in production.

* ``./run sandbox-link [--user user_good|user_transactions_dynamic|custom] [--institution ID]``
  creates a test Item without Plaid Link (``/sandbox/public_token/create``), saves it through the
  same exchange path as a real link, then syncs until Plaid has sent the full history.
  ``custom`` is an Amex-like credit card with known totals (RESEARCH T3.7,
  ``linking.custom_user_config``).
* ``./run sandbox-refresh [--item ID]``: what "Sync now" does — ``/transactions/refresh``, then a
  sync now and after about 15 s, 45 s and 2 minutes (``user_transactions_dynamic`` then moves its
  pending charges to posted).
* ``./run sandbox-reset-login [--item ID]``: forces ``ITEM_LOGIN_REQUIRED`` and syncs once, so the
  Connection page offers Reconnect (update mode) to try the repair flow.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from budgetapp import clock, db, secrets
from budgetapp.commands import add_command
from budgetapp.commands.sync import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    Runtime,
    describe_outcome,
    keychain_approval,
    missing_plaid_keys,
)
from budgetapp.config import Settings
from budgetapp.logsetup import setup_logging
from budgetapp.plaid_gateway import PlaidGatewayError
from budgetapp.services import linking
from budgetapp.services import sync as sync_service
from budgetapp.services.linking import SANDBOX_USERS as USERS
from budgetapp.services.linking import sandbox_credentials as credentials

log = logging.getLogger(__name__)

#: Sandbox backfill after a test link: Plaid's Sandbox usually has the full history in seconds.
LINK_BACKFILL_DELAYS: tuple[float, ...] = (5.0, 5.0, 10.0, 10.0, 15.0, 15.0, 30.0, 30.0)
LINK_BACKFILL_CAP = 150.0


def register(subparsers: Any) -> None:
    link = add_command(
        subparsers,
        "sandbox-link",
        handler=run_link,
        help="Sandbox only: create a test Item without Plaid Link",
    )
    link.add_argument(
        "--user",
        choices=USERS,
        default="user_transactions_dynamic",
        help="Sandbox test user (custom = an Amex-like credit card with known totals)",
    )
    link.add_argument(
        "--institution",
        default=linking.DEFAULT_SANDBOX_INSTITUTION,
        help=f"institution id (default {linking.DEFAULT_SANDBOX_INSTITUTION})",
    )
    refresh = add_command(
        subparsers,
        "sandbox-refresh",
        handler=run_refresh,
        help="Sandbox only: /transactions/refresh, then sync",
    )
    refresh.add_argument("--item", type=int, metavar="ID", help="only this Item (database id)")
    reset = add_command(
        subparsers,
        "sandbox-reset-login",
        handler=run_reset_login,
        help="Sandbox only: force ITEM_LOGIN_REQUIRED to test reconnecting",
    )
    reset.add_argument("--item", type=int, metavar="ID", help="only this Item (database id)")


# --------------------------------------------------------------------------- shared steps


def refuse_outside_sandbox(rt: Runtime, command: str) -> int | None:
    settings = rt.settings
    if settings.is_production or settings.plaid_env != "sandbox":
        rt.out(
            f"error: {command} works only in the Sandbox (test data); it never touches the "
            "production connection. Drop --env production."
        )
        return EXIT_USAGE
    return None


def chosen_items(rt: Runtime, item_id: int | None) -> dict[int, str] | int:
    """The Items to act on (all, or ``--item``), or an exit code after explaining why not."""
    names = rt.item_names()
    if not names:
        rt.out("Nothing is linked in the Sandbox yet. Create a test Item: ./run sandbox-link")
        return EXIT_FAILED
    if item_id is None:
        return names
    if item_id not in names:
        known = ", ".join(f"{i} ({n})" for i, n in names.items())
        rt.out(f"error: there is no Item {item_id}. Linked Items: {known}.")
        return EXIT_USAGE
    return {item_id: names[item_id]}


def _guarded(rt: Runtime, command: str, body: Any) -> int:
    """Run ``body()`` with the Sandbox check, the database check and friendly errors."""
    refused = refuse_outside_sandbox(rt, command)
    if refused is not None:
        return refused
    try:
        rt.ensure_database()
        return body()
    except secrets.KeychainApprovalNeeded as exc:
        return keychain_approval(rt, exc, unattended=False)
    except db.DatabaseUnavailable:
        rt.out("The database isn't responding. Start it with: ./run db start")
        return EXIT_FAILED
    except db.SchemaMismatch:
        rt.out("The Sandbox database is behind this code. Run: ./run migrate")
        return EXIT_FAILED
    except secrets.MissingSecretError as exc:
        return missing_plaid_keys(rt, exc)
    except (linking.LinkError, sync_service.MissingAccessToken) as exc:
        rt.out(f"error: {exc}")
        return EXIT_FAILED
    except PlaidGatewayError as exc:
        detail = exc.display_message or exc.error_message or "no detail"
        rt.out(f"error: Plaid refused ({exc.error_code}): {detail}")
        return EXIT_FAILED


# --------------------------------------------------------------------------- sandbox-link


def link_command(args: argparse.Namespace, rt: Runtime) -> int:
    def body() -> int:
        user = getattr(args, "user", "user_transactions_dynamic")
        institution = getattr(args, "institution", linking.DEFAULT_SANDBOX_INSTITUTION)
        username, password = credentials(user, clock.today())
        # Liabilities is documented for the non-OAuth First Platypus Bank (RESEARCH T3.1).
        products = (
            ("transactions", "liabilities")
            if institution == linking.DEFAULT_SANDBOX_INSTITUTION
            else ("transactions",)
        )
        result = linking.sandbox_create_item(
            rt.session_factory,
            rt.gateway,
            rt.store,
            rt.settings,
            institution_id=institution,
            username=username,
            password=password,
            initial_products=products,
            sleep=rt.sleep,
        )
        name = result.institution_name or institution
        rt.out(
            f"Created a Sandbox Item for {user} at {name}: Item {result.item_id} with "
            f"{result.account_count} account{'' if result.account_count == 1 else 's'}."
        )
        if result.attach_needed:
            rt.out("Imported CSV history is waiting: attach it on the Connection page.")
        rt.out("Syncing until Plaid has sent the full history (up to about 2 minutes)…")
        outcomes = sync_service.backfill_until_complete(
            rt.session_factory,
            rt.gateway,
            rt.store,
            rt.settings,
            result.item_id,
            notifier=rt.notifier,
            trigger="link",
            delays=LINK_BACKFILL_DELAYS,
            cap=LINK_BACKFILL_CAP,
            sleep=rt.sleep,
        )
        for outcome in outcomes:
            rt.out(describe_outcome(name, outcome))
        if outcomes and outcomes[-1].status == "error":
            return EXIT_FAILED
        if not any(o.update_status == sync_service.HISTORICAL_UPDATE_COMPLETE for o in outcomes):
            rt.out("Plaid is still loading older history; ./run sandbox-refresh syncs again.")
        return EXIT_OK

    return _guarded(rt, "sandbox-link", body)


# --------------------------------------------------------------------------- sandbox-refresh


def refresh_command(args: argparse.Namespace, rt: Runtime) -> int:
    def body() -> int:
        chosen = chosen_items(rt, getattr(args, "item", None))
        if isinstance(chosen, int):
            return chosen
        failed = False
        for item_id, name in chosen.items():
            rt.out(f"{name}: asking Plaid to refresh, then syncing now and 3 more times (~2 min)…")
            task = sync_service.RefreshThenSync(
                rt.session_factory,
                rt.gateway,
                rt.store,
                rt.settings,
                item_id,
                notifier=rt.notifier,
                sleep=rt.sleep,
            )
            while True:
                delay = task()
                if len(task.outcomes) == 1 and task.refresh_error is not None:
                    code = task.refresh_error.error_code
                    rt.out(f"{name}: Plaid refused the refresh ({code}); synced anyway.")
                rt.out(describe_outcome(name, task.outcomes[-1]))
                if delay is None:
                    break
                rt.sleep(delay)
            failed |= task.outcomes[-1].status == "error"
        return EXIT_FAILED if failed else EXIT_OK

    return _guarded(rt, "sandbox-refresh", body)


# --------------------------------------------------------------------------- sandbox-reset-login


def reset_login_command(args: argparse.Namespace, rt: Runtime) -> int:
    def body() -> int:
        chosen = chosen_items(rt, getattr(args, "item", None))
        if isinstance(chosen, int):
            return chosen
        plaid_ids = rt.plaid_item_ids()
        for item_id, name in chosen.items():
            token = sync_service.access_token_for(rt.settings, plaid_ids[item_id], rt.store)
            rt.gateway.sandbox_reset_login(token)
            outcome = sync_service.sync_item(
                rt.session_factory,
                rt.gateway,
                rt.store,
                rt.settings,
                item_id,
                trigger="cli",
                notifier=rt.notifier,
                sleep=rt.sleep,
            )
            if outcome.needs_reconnect:
                rt.out(
                    f"{name}: Plaid now asks to sign in again ({outcome.error_code}). Open the "
                    "Connection page and click Reconnect to try update mode."
                )
            else:
                rt.out(describe_outcome(name, outcome))
        return EXIT_OK

    return _guarded(rt, "sandbox-reset-login", body)


# --------------------------------------------------------------------------- handlers


def _run(args: argparse.Namespace, settings: Settings, command: Any) -> int:
    verbose = bool(getattr(args, "verbose", False))
    setup_logging(
        settings,
        log_name=None if settings.is_test else f"sync-{settings.env}",
        console_level=logging.INFO if verbose else logging.WARNING,
    )
    rt = Runtime(settings)
    try:
        return command(args, rt)
    finally:
        rt.close()


def run_link(args: argparse.Namespace, settings: Settings) -> int:
    return _run(args, settings, link_command)


def run_refresh(args: argparse.Namespace, settings: Settings) -> int:
    return _run(args, settings, refresh_command)


def run_reset_login(args: argparse.Namespace, settings: Settings) -> int:
    return _run(args, settings, reset_login_command)
