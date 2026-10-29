"""Connection page (SYNC-1 … SYNC-7): linking, the connection's status, update mode, Sync now.

* ``GET /connection``: the page; with ``HX-Request`` only its live region (``#connection-live``,
  polled while a sync or the backfill after linking runs). ``GET /connection/preflight``: the
  production pre-flight institution check, loaded after the page.
* ``POST /connection/sync`` (the top-bar Sync now on every page) and ``GET /connection/sync-status``
  answer with ``components/sync_status.html`` (components README §2.4); a plain form post gets a
  303 to this page.
* JSON endpoints for ``static/js/link.js`` (Plaid Link): ``POST /connection/link-token``,
  ``/connection/exchange`` (a new link), ``/connection/update-complete`` (update mode: its public
  token is never exchanged) and ``/connection/link-exit`` (Link closed without linking).
* Forms: ``POST /connection/sandbox-item`` (Sandbox: a test Item without Link),
  ``/connection/recover`` (re-attach a connection whose token is in Keychain but whose database
  row is missing), ``/connection/attach`` (move imported CSV history onto a linked card) and
  ``/connection/lost-edits/{id}/dismiss``.

Background work — "Sync now", the backfill after linking and the syncs after update mode — runs on
the app's :class:`~budgetapp.services.background.BackgroundJobs` (``app.state.jobs``, made on first
use; under pytest it never starts a thread: tests run the queue with ``run_all()``). A route marks
the sync run ``running`` before answering, so the page says "Syncing…" at once.

A demo database (``serve --demo``) never calls Plaid: those actions answer "Not available with
demo data", and the page loads no Plaid script.
"""

from __future__ import annotations

import logging
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import clock, db, secrets
from budgetapp.config import Settings, in_test_process
from budgetapp.models import SyncRun
from budgetapp.plaid_gateway import PlaidGateway, PlaidGatewayError, PlaidTransportError
from budgetapp.services import linking
from budgetapp.services import sync as sync_service
from budgetapp.services.background import BackgroundJobs
from budgetapp.services.notify import Notifier
from budgetapp.services.status import connection_status
from budgetapp.web.deps import (
    FormDep,
    NotifierDep,
    SecretStoreDep,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
    get_gateway,
)
from budgetapp.web.flash import flash, flash_oob, redirect
from budgetapp.web.security import allow_plaid_link
from budgetapp.web.templating import is_htmx, layout_env, render

log = logging.getLogger(__name__)

router = APIRouter()

PAGE = "/connection"
NAV = "connection"
LINK_MODES = ("new", "update", "liabilities")

DEMO_MESSAGE = (
    "Not available with demo data: this server shows a demo database and never calls Plaid."
)
KEYS_MISSING = (
    "The Plaid keys aren't saved yet, so the app can't reach Plaid. Save them in Terminal with "
    "./run setup --keys-only, then reload this page."
)
KEYCHAIN_MESSAGE = (
    "macOS Keychain needs your approval before the app can use your Plaid keys. Run ./run doctor "
    "in Terminal, click Always Allow, then reload this page."
)
KEYS_UNREADABLE = "The Plaid keys couldn't be read from Keychain. Check them with ./run doctor."
NOT_STARTED = "The app is stopping, so the sync didn't start. Try again in a moment."

# Polling of the live region: fast while a sync runs, slower while a follow-up waits.
POLL_SYNCING = "every 4s"
POLL_WAITING = "every 10s"


# --------------------------------------------------------------------------- dependencies

_jobs_lock = threading.Lock()


def get_jobs(request: Request) -> BackgroundJobs:
    """The app's background job runner (``app.state.jobs``), made on first use.

    Under pytest it never starts a worker thread (the test's connection is not thread-safe):
    queued jobs wait until the test runs them with ``app.state.jobs.run_all()``.
    """
    state = request.app.state
    jobs = getattr(state, "jobs", None)
    if jobs is None:
        with _jobs_lock:
            jobs = getattr(state, "jobs", None)
            if jobs is None:
                jobs = BackgroundJobs(start=not in_test_process())
                state.jobs = jobs
    return jobs


async def get_json_body(request: Request) -> dict[str, Any]:
    """The posted JSON object (``{}`` for anything else): link.js sends JSON."""
    try:
        data = await request.json()
    except ValueError:  # malformed JSON or not UTF-8
        return {}
    return data if isinstance(data, dict) else {}


JobsDep = Annotated[BackgroundJobs, Depends(get_jobs)]
JsonDep = Annotated[dict[str, Any], Depends(get_json_body)]


def is_demo(request: Request) -> bool:
    return layout_env(request) == "demo"


def plaid_gateway(request: Request) -> tuple[PlaidGateway | None, str | None]:
    """``(gateway, None)``, or ``(None, why not)`` while the Plaid keys can't be used."""
    try:
        return get_gateway(request), None
    except secrets.KeychainApprovalNeeded:
        return None, KEYCHAIN_MESSAGE
    except secrets.MissingSecretError:
        return None, KEYS_MISSING
    except secrets.SecretStoreError as exc:
        log.warning("The Plaid keys couldn't be read: %s", exc)
        return None, KEYS_UNREADABLE


def refresh_key(item_id: int) -> str:
    return f"refresh:{item_id}"


def backfill_key(item_id: int) -> str:
    return f"backfill:{item_id}"


# --------------------------------------------------------------------------- helpers


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _short(value: Any, limit: int = 80) -> str | None:
    """A browser-sent label, safe for one log line."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:limit] or None


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def plaid_message(exc: PlaidGatewayError) -> str:
    """What to tell the user about a Plaid error (never a token)."""
    if isinstance(exc, PlaidTransportError):
        return "Plaid couldn't be reached. Check your internet connection and try again."
    detail = exc.display_message or exc.error_message or exc.error_code or "no detail"
    return f"Plaid couldn't do that ({exc.error_code}): {detail}"


def _abandon_run(session_factory: sessionmaker[Session], run_id: int) -> None:
    with db.transaction(session_factory) as s:
        run = s.get(SyncRun, run_id)
        if run is not None and run.status == "running":
            run.status = "interrupted"
            run.finished_at = clock.now()
            run.error_message = "The app stopped before this sync could start."


def start_backfill(
    jobs: BackgroundJobs,
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    notifier: Notifier,
    item_id: int,
    *,
    trigger: str,
) -> bool:
    """Queue the syncs after linking (``trigger="link"``: until Plaid has sent the full history)
    or after update mode (``"update"``: now and once more shortly after). The first run is marked
    ``running`` at once."""
    with db.transaction(session_factory) as s:
        run_id = sync_service.prepare_run(s, item_id, trigger).id
    options: dict[str, Any] = {"trigger": trigger, "run_id": run_id}
    if trigger == "update":
        options.update(delays=sync_service.UPDATE_DELAYS, min_runs=2)
    task = sync_service.Backfill(
        session_factory, gateway, store, settings, item_id, notifier=notifier, **options
    )
    if jobs.submit(backfill_key(item_id), task, replace=True):
        return True
    _abandon_run(session_factory, run_id)
    return False


def _busy(jobs: BackgroundJobs, item_id: int) -> str | None:
    """What is queued for an Item: ``"backfill"``, ``"refresh"`` or ``None``."""
    if jobs.is_busy(backfill_key(item_id)):
        return "backfill"
    if jobs.is_busy(refresh_key(item_id)):
        return "refresh"
    return None


def page_context(
    request: Request,
    session: Session,
    settings: Settings,
    store: secrets.SecretStore,
    jobs: BackgroundJobs,
) -> dict[str, Any]:
    """Everything the page and its live region show."""
    demo = is_demo(request)
    overview = linking.connection_overview(session, settings, store=store)
    plaid_problem = None if demo else plaid_gateway(request)[1]
    link_enabled = not demo and plaid_problem is None
    plaid_script = link_enabled and (overview.can_link or overview.link_actions)
    if plaid_script:
        allow_plaid_link(request)
    busy = {item.id: _busy(jobs, item.id) for item in overview.items}
    if overview.syncing:
        poll = POLL_SYNCING
    elif any(busy.values()):
        poll = POLL_WAITING
    else:
        poll = None
    return {
        "overview": overview,
        "conn": overview.status,
        "demo": demo,
        "plaid_problem": plaid_problem,
        "link_enabled": link_enabled,
        "plaid_script": plaid_script,
        "busy": busy,
        "poll": poll,
        "checklist": linking.checklist(),
        "sandbox_users": SANDBOX_USER_OPTIONS,
        "now": clock.now(),
        "today": clock.today(),
    }


SANDBOX_USER_OPTIONS = [
    ("custom", "Amex-like card, known totals"),
    ("user_transactions_dynamic", "Dynamic: pending charges post"),
    ("user_good", "Plaid's standard test user"),
]


# --------------------------------------------------------------------------- the page


@router.get("/connection")
def connection_page(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    jobs: JobsDep,
) -> Response:
    context = page_context(request, session, settings, store, jobs)
    if is_htmx(request):  # the live region polling for news
        return render(request, "connection/_live.html", context, active_nav=NAV)
    return render(request, "connection/index.html", context, active_nav=NAV)


@router.get("/connection/preflight")
def preflight(request: Request, settings: SettingsDep) -> Response:
    """The live ``/institutions/search`` check before the first production link (best effort)."""
    check: linking.InstitutionCheck | None = None
    if settings.is_production and not is_demo(request):
        gateway, problem = plaid_gateway(request)
        if gateway is None:
            check = linking.InstitutionCheck("error", problem or KEYS_MISSING)
        else:
            check = linking.preflight(gateway, settings).institution
    return render(request, "connection/_preflight.html", {"check": check}, active_nav=NAV)


# --------------------------------------------------------------------------- Sync now


def _sync_answer(request: Request, session: Session, level: str, text: str) -> Response:
    """The status partial plus a toast (HTMX), or a 303 back to this page (a plain form)."""
    if is_htmx(request):
        return render(
            request,
            "components/sync_status.html",
            {"conn": connection_status(session)},
            active_nav=NAV,
            oob=flash_oob(level, text),
        )
    return redirect(request, PAGE, level, text)


@router.post("/connection/sync")
def sync_now(
    request: Request,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    notifier: NotifierDep,
    jobs: JobsDep,
) -> Response:
    """Sync now (SYNC-2): ``/transactions/refresh``, a sync now and three follow-ups, in the
    background. Answers at once with the status pill saying "Syncing…"."""
    if is_demo(request):
        return _sync_answer(request, session, "info", DEMO_MESSAGE)
    conn = connection_status(session)
    if not conn.linked:
        return _sync_answer(
            request, session, "info", "Nothing is linked yet. Link your card first."
        )
    if conn.syncing:
        return _sync_answer(request, session, "info", "A sync is already running.")
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return _sync_answer(request, session, "error", problem or KEYS_MISSING)
    for item in conn.items:
        run_id = sync_service.prepare_run(session, item.item_id, "manual").id
        session.commit()
        task = sync_service.RefreshThenSync(
            session_factory,
            gateway,
            store,
            settings,
            item.item_id,
            notifier=notifier,
            run_id=run_id,
        )
        if not jobs.submit(refresh_key(item.item_id), task, replace=True):
            _abandon_run(session_factory, run_id)
            return _sync_answer(request, session, "error", NOT_STARTED)
    return _sync_answer(request, session, "info", "Sync started")


@router.get("/connection/sync-status")
def sync_status(request: Request, session: SessionDep) -> Response:
    """Polled by the top-bar pill while a sync runs."""
    return render(
        request, "components/sync_status.html", {"conn": connection_status(session)}, active_nav=NAV
    )


# --------------------------------------------------------------------------- Plaid Link (JSON)


@router.post("/connection/link-token")
def link_token(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    body: JsonDep,
) -> Response:
    """A Link token: ``mode`` "new" (guarded in production), "update" (Reconnect / Renew
    consent) or "liabilities" (update mode asking for statement details)."""
    if is_demo(request):
        return _json_error(409, DEMO_MESSAGE)
    mode = body.get("mode")
    if mode not in LINK_MODES:
        return _json_error(400, "That isn't a kind of Plaid Link session this page knows.")
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return _json_error(503, problem or KEYS_MISSING)
    try:
        if mode == "new":
            token = linking.new_link_token(session, gateway, settings, store=store)
            session.commit()  # releases the link lock
        else:
            item_id = _int(body.get("item_id"))
            if item_id is None:
                return _json_error(400, "Which connection? Reload the page and try again.")
            token = linking.update_link_token(
                session,
                gateway,
                item_id,
                add_liabilities=mode == "liabilities",
                store=store,
                settings=settings,
            )
    except (linking.LinkError, sync_service.MissingAccessToken) as exc:
        return _json_error(409, str(exc))
    except secrets.KeychainApprovalNeeded:
        return _json_error(503, KEYCHAIN_MESSAGE)
    except secrets.SecretStoreError as exc:
        log.warning("Keychain failed while starting Plaid Link: %s", exc)
        return _json_error(503, KEYS_UNREADABLE)
    except PlaidGatewayError as exc:
        log.warning("Link token refused: %s %s", exc.error_code, exc.error_message)
        return _json_error(502, plaid_message(exc))
    return JSONResponse({"link_token": token})


@router.post("/connection/exchange")
def exchange(
    request: Request,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    notifier: NotifierDep,
    jobs: JobsDep,
    body: JsonDep,
) -> Response:
    """Link's ``onSuccess`` for a NEW link: save the connection, then sync its history."""
    if is_demo(request):
        return _json_error(409, DEMO_MESSAGE)
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return _json_error(503, problem or KEYS_MISSING)
    try:
        result = linking.exchange(
            session_factory,
            gateway,
            store,
            settings,
            body.get("public_token"),  # type: ignore[arg-type]  (checked by exchange)
            body.get("metadata"),
        )
    except linking.LinkRefused as exc:
        return _json_error(409, str(exc))
    except linking.LinkFailed as exc:
        return _json_error(502, str(exc))
    except secrets.KeychainApprovalNeeded:
        return _json_error(503, KEYCHAIN_MESSAGE)
    start_backfill(
        jobs, session_factory, gateway, store, settings, notifier, result.item_id, trigger="link"
    )
    name = result.institution_name or "Your card"
    text = (
        f"{name} is linked. Plaid is sending your transactions; two years of history can take a "
        "few minutes."
    )
    if result.attach_needed:
        text += " Choose below which card your imported CSV history belongs to."
    elif result.attached is not None:
        moved = result.attached.moved_transactions
        text += f" Your imported history ({moved:,} transactions) now belongs to the card."
    response = JSONResponse({"redirect": PAGE, "item_id": result.item_id})
    flash(response, "success", text)
    return response


@router.post("/connection/update-complete")
def update_complete(
    request: Request,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    notifier: NotifierDep,
    jobs: JobsDep,
    body: JsonDep,
) -> Response:
    """Link's ``onSuccess`` in update mode: same connection, healthy again; sync now and once
    more shortly after. The public token is ignored."""
    if is_demo(request):
        return _json_error(409, DEMO_MESSAGE)
    item_id = _int(body.get("item_id"))
    if item_id is None:
        return _json_error(400, "Which connection? Reload the page and try again.")
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return _json_error(503, problem or KEYS_MISSING)
    try:
        result = linking.complete_update(
            session_factory, gateway, store, settings, item_id, metadata=body.get("metadata")
        )
    except (linking.LinkError, sync_service.MissingAccessToken) as exc:
        return _json_error(409, str(exc))
    except secrets.KeychainApprovalNeeded:
        return _json_error(503, KEYCHAIN_MESSAGE)
    start_backfill(
        jobs, session_factory, gateway, store, settings, notifier, item_id, trigger="update"
    )
    if body.get("mode") == "liabilities":
        text = (
            "Statement details are now shared; they show after this sync."
            if result.liabilities_consented
            else "Plaid didn't add statement details for this card. Transactions keep syncing."
        )
    else:
        text = "Your Amex connection is working again. Syncing now."
        if result.consent_expires_at is not None:
            due = result.consent_expires_at.astimezone().date()
            text += f" Access is renewed until {due:%b} {due.day}, {due.year}."
    response = JSONResponse({"redirect": PAGE, "item_id": item_id})
    flash(response, "success", text)
    return response


@router.post("/connection/link-exit")
def link_exit(
    request: Request, settings: SettingsDep, store: SecretStoreDep, body: JsonDep
) -> Response:
    """Link closed without linking (``onExit``, or ``CLOSE_OAUTH`` / ``FAIL_OAUTH``): logged as
    "not linked"; a production new-link attempt's marker is cleared."""
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    linking.link_exited(
        settings,
        store=store,
        mode="new" if body.get("mode") == "new" else "update",
        error_code=_short(error.get("error_code")),
        event=_short(body.get("event")),
        status=_short(body.get("status")),
        link_session_id=_short(body.get("link_session_id")),
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------- forms


@router.post("/connection/sandbox-item")
def sandbox_item(
    request: Request,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    notifier: NotifierDep,
    jobs: JobsDep,
    form: FormDep,
) -> Response:
    """Sandbox only: a test Item without Plaid Link, through the same exchange path."""
    if is_demo(request):
        return redirect(request, PAGE, "info", DEMO_MESSAGE)
    if settings.is_production or settings.plaid_env != "sandbox":
        return redirect(request, PAGE, "error", "Test Items exist only in the Sandbox.")
    user = form.get("user")
    user = user if isinstance(user, str) and user in linking.SANDBOX_USERS else "custom"
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return redirect(request, PAGE, "error", problem or KEYS_MISSING)
    username, password = linking.sandbox_credentials(user, clock.today())
    try:
        result = linking.sandbox_create_item(
            session_factory, gateway, store, settings, username=username, password=password
        )
    except linking.LinkError as exc:
        return redirect(request, PAGE, "error", str(exc))
    except PlaidGatewayError as exc:
        return redirect(request, PAGE, "error", plaid_message(exc))
    start_backfill(
        jobs, session_factory, gateway, store, settings, notifier, result.item_id, trigger="link"
    )
    name = result.institution_name or "the Sandbox bank"
    return redirect(
        request, PAGE, "success", f"Test Item created at {name}. Syncing its history now."
    )


@router.post("/connection/recover")
def recover(
    request: Request,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    notifier: NotifierDep,
    jobs: JobsDep,
) -> Response:
    """Re-attach connections whose access token is in Keychain but whose row is missing (never a
    new link), then sync each from the beginning."""
    if is_demo(request):
        return redirect(request, PAGE, "info", DEMO_MESSAGE)
    gateway, problem = plaid_gateway(request)
    if gateway is None:
        return redirect(request, PAGE, "error", problem or KEYS_MISSING)
    try:
        results = linking.recover_orphaned_items(session_factory, gateway, store, settings)
    except linking.LinkError as exc:
        return redirect(request, PAGE, "error", str(exc))
    except secrets.KeychainApprovalNeeded:
        return redirect(request, PAGE, "error", KEYCHAIN_MESSAGE)
    if not results:
        return redirect(request, PAGE, "info", "There was no saved connection to re-attach.")
    for result in results:
        start_backfill(
            jobs,
            session_factory,
            gateway,
            store,
            settings,
            notifier,
            result.item_id,
            trigger="link",
        )
    count = len(results)
    noun = "connection" if count == 1 else "connections"
    return redirect(
        request, PAGE, "success", f"Re-attached {count} {noun}. Syncing from the beginning now."
    )


@router.post("/connection/attach")
def attach(request: Request, session: SessionDep, form: FormDep) -> Response:
    """Move a manual (CSV-only) account's history onto a linked card."""
    manual_id = _int(form.get("manual_id"))
    account_id = _int(form.get("account_id"))
    if manual_id is None or account_id is None:
        return redirect(request, PAGE, "error", "Choose the imported account and the linked card.")
    try:
        result = linking.attach_manual(session, manual_id, account_id)
    except linking.LinkRefused as exc:
        return redirect(request, PAGE, "error", str(exc))
    except NotImplementedError:
        return redirect(
            request,
            PAGE,
            "info",
            "Moving imported history onto a linked card isn't available in this version yet.",
        )
    session.commit()
    moved = result.moved_transactions
    return redirect(
        request,
        PAGE,
        "success",
        f"Moved {moved:,} imported transaction{'' if moved == 1 else 's'} onto the card.",
    )


@router.post("/connection/lost-edits/{plaid_transaction_id}/dismiss")
def dismiss_lost_edit(request: Request, plaid_transaction_id: str, session: SessionDep) -> Response:
    """Forget edits that found no replacement charge ("edits we couldn't carry over")."""
    if not linking.dismiss_lost_edit(session, plaid_transaction_id):
        raise HTTPException(404)
    session.commit()
    text = "Dismissed. Those edits won't be offered again."
    if is_htmx(request):
        context = {"lost_edits": linking.lost_edits(session)}
        return render(
            request,
            "connection/_lost_edits.html",
            context,
            active_nav=NAV,
            oob=flash_oob("success", text),
        )
    return redirect(request, PAGE, "success", text)
