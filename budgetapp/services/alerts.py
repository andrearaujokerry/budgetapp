"""Alerts with de-duplication (ALR-1..3, SYNC-6) — ARCHITECTURE §11.

:func:`record_and_notify` is the only way an alert is raised: it inserts an ``alert_events`` row
keyed by ``dedupe_key`` (``INSERT … ON CONFLICT DO NOTHING``) and queues a notification ONLY when
the row was inserted, so each alert fires once. The notification is sent after the caller's
transaction COMMITS (never while it holds locks), and only if the row is still there then: a
rolled-back transaction or savepoint sends nothing, so a later run can raise the alert again
without a duplicate. :func:`clear_alert` deletes an event so it can fire again.

**What is evaluated, and when**

* :func:`evaluate_after_sync` — after every sync run that reached Plaid (``success``,
  ``not_ready`` and failures; never ``skipped``), in its own transaction (``services.sync``):

  - ALR-2, when ``app_settings.sync_alerts_enabled``: a failed run alerts once per Item, error code
    and day (``sync-error:<item>:<code>:<day>``), unless the Item needs reconnecting: that alerts
    once per occurrence (``reconnect:<item>:open``, renamed to ``reconnect:<item>:<id>`` when a
    sync sees the Item healthy again, so the next occurrence alerts again). Stale data (no success
    for 36 hours, ``services.status``) alerts once a day (``stale:<item>:<day>``), silently when a
    failure or reconnect alert goes out in the same run (its message says how old the data is).
  - SYNC-6: consent renewal due within ``consent_reminder_days`` alerts once per due date
    (``consent:<item>:<due date>``), whatever ``sync_alerts_enabled`` says, except while the Item
    needs reconnecting (reconnecting renews the consent too).
  - ALR-1: :func:`evaluate_budgets` after every successful run (so budget and setting edits are
    picked up by the next sync too).
  - ALR-3, when ``large_charge_threshold`` is set: Plaid rows in
    ``outcome.inserted_transaction_ids`` (new charges only) for more than the threshold in a
    spending (``expense``) category after categorization, on any account (a card payment from
    checking, a transfer or a paycheck never alerts; W3-PLAN C5), dated at
    most 7 days before the previous successful sync, never on an initial history load; once per
    charge (``large-charge:<pending id the row names, else its own Plaid id>``), so a posting never
    alerts again. A pending charge at or under the threshold that this sync stored above it
    (``outcome.pending_raises``: a tip or hotel incidentals added at posting) alerts once under
    the pending row's key. More than :data:`MAX_CHARGE_NOTIFICATIONS` new ones in a run are
    recorded silently and announced by one summary (``large-charges:<item>:<sync run>``).

* :func:`evaluate_after_import` — after a CSV import that inserted or paired rows: budgets only
  (CSV imports never raise large-charge alerts).
* :func:`evaluate_budgets` — ALR-1 for the current budget period (and the calendar year for annual
  budgets), with the Budgets page's own numbers (``services.budgets``, which reads
  ``spending.period_spending``). Threshold ``t`` is crossed when ``spent > 0`` and
  ``spent ≥ available × t / 100``. Keys: ``budget:<category id|overall>:<period start>:<t>`` and,
  for annual budgets, ``budget:<category id>:<year>:<t>`` (:func:`budget_key`). When several
  thresholds are crossed at once only the highest is notified (the others are recorded silently),
  and a threshold whose spending fell back below it (a pending hold dropped, a bigger budget) is
  cleared so it can fire again. A statement cycle's start can still move by a few days (a
  generated closing date replaced by the real one), so an event keyed within 10 days of the
  period's start counts as this period's.

Every evaluation runs in a savepoint and catches its own errors: an alert problem is logged and
never breaks the sync, import or edit that called it (the caller's data still commits).
Notifications that cannot use the database (Keychain approval needed, schema upgrade needed,
backups) are sent by their commands through ``secrets.claim_daily_marker`` instead.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, delete, event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.models import AlertEvent, AppSettings, PlaidItem, Transaction, get_app_settings
from budgetapp.money import format_money
from budgetapp.periods import Period
from budgetapp.services import budgets, status
from budgetapp.services.events import ImportOutcome, SyncOutcome
from budgetapp.services.notify import Notifier

log = logging.getLogger(__name__)

_PENDING = "budgetapp.alerts.pending"

KIND_BUDGET = "budget"
KIND_SYNC_ERROR = "sync_error"
KIND_RECONNECT = "reconnect"
KIND_STALE = "stale"
KIND_CONSENT = "consent"
KIND_LARGE_CHARGE = "large_charge"

#: ALR-3 looks at charges dated at most this many days before the previous successful sync
#: (before today on a first sync), so a catch-up after a break still announces them (§11).
LARGE_CHARGE_DAYS = 7
#: More new large charges than this in one sync are announced by a single summary notification.
MAX_CHARGE_NOTIFICATIONS = 3
#: An existing budget event keyed this close to a period's start belongs to that period.
PERIOD_START_SLACK = dt.timedelta(days=10)
#: Budget events older than this can't belong to the current period or year.
_BUDGET_LOOKBACK = dt.timedelta(days=400)
#: An Item saved without an institution name (rare): connection alerts stay neutral (C9).
DEFAULT_INSTITUTION = "Your bank"

_BUDGET_KEY = re.compile(
    r"^budget:(?P<scope>overall|\d+):(?P<token>\d{4}(?:-\d{2}-\d{2})?):(?P<threshold>\d+)$"
)


@dataclass(frozen=True, slots=True)
class AlertResult:
    """``inserted`` is False when an event with the same ``dedupe_key`` already existed.

    ``queued``: the notification will be sent when the caller's transaction commits; the outcome
    is then recorded on the row (``alert_events.delivered`` / ``delivered_at`` / ``error``).
    """

    kind: str
    dedupe_key: str
    inserted: bool
    alert_id: int | None = None
    delivered: bool = False
    error: str | None = None
    queued: bool = False


# --------------------------------------------------------------------------- recording


@dataclass(frozen=True, slots=True)
class _Pending:
    notifier: Notifier
    alert_id: int
    title: str
    message: str


def _queue(session: Session, pending: _Pending) -> None:
    queue = session.info.get(_PENDING)
    if queue is None:
        queue = session.info[_PENDING] = []
        event.listen(session, "after_commit", _deliver_after_commit)
        event.listen(session, "after_soft_rollback", _forget_after_rollback)
    queue.append(pending)


def _forget_after_rollback(session: Session, previous_transaction: Any) -> None:
    # A savepoint rollback keeps the queue: delivery re-checks that each row still exists.
    if previous_transaction.parent is None:
        session.info.get(_PENDING, []).clear()


def _deliver_after_commit(session: Session) -> None:
    # SQLAlchemy fires after_commit when a savepoint is released too; only the caller's own
    # COMMIT may send (a released savepoint can still be rolled back with its transaction).
    if session.get_nested_transaction() is not None:
        return
    queue = session.info.get(_PENDING)
    if not queue:
        return
    pending, queue[:] = list(queue), []
    try:
        bind = session.get_bind()
    except Exception:
        log.warning("alerts: no database bind to record deliveries; %d not sent", len(pending))
        return
    for item in pending:
        try:
            _deliver(bind, item)
        except Exception:  # never break the caller's commit
            log.warning("alerts: could not deliver alert %s", item.alert_id, exc_info=True)


def _deliver(bind: Engine | Connection, item: _Pending) -> None:
    """Send one committed alert and record the outcome in a short transaction of its own."""
    with Session(bind=bind) as check:
        row = check.get(AlertEvent, item.alert_id)
        if row is None or row.delivered or row.delivered_at is not None or row.error is not None:
            return  # rolled back with a savepoint, or already handled
        try:
            result = item.notifier.notify(item.title, item.message)
            ok, error = result.ok, result.error
        except Exception as exc:  # a broken notifier must not break a sync
            ok, error = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            log.warning("alerts: notification %s was not sent: %s", item.alert_id, error)
        row.delivered = ok
        row.delivered_at = clock.now() if ok else None
        row.error = error if not ok else None
        check.commit()


def record_and_notify(
    session: Session,
    notifier: Notifier,
    *,
    kind: str,
    dedupe_key: str,
    title: str,
    message: str,
    category_id: int | None = None,
    transaction_id: int | None = None,
    item_id: int | None = None,
    notify: bool = True,
) -> AlertResult:
    """Record an alert once and notify only if it is new.

    The notification is queued and sent right after the caller COMMITS (nothing is sent if the
    transaction rolls back); ``alert_events.delivered``/``delivered_at``/``error`` then record
    whether osascript accepted it. Never raises because of the notifier. The caller commits.
    ``notify=False`` records the event without a notification (it still counts as fired).
    """
    stmt = (
        pg_insert(AlertEvent)
        .values(
            kind=kind,
            dedupe_key=dedupe_key,
            title=title,
            message=message,
            category_id=category_id,
            transaction_id=transaction_id,
            item_id=item_id,
        )
        .on_conflict_do_nothing(index_elements=[AlertEvent.dedupe_key])
        .returning(AlertEvent.id)
    )
    alert_id = session.execute(stmt).scalar_one_or_none()
    if alert_id is None:
        return AlertResult(kind, dedupe_key, inserted=False)
    log.info("alerts: recorded %s (%s)%s", dedupe_key, kind, "" if notify else ", not notified")
    if not notify:
        return AlertResult(kind, dedupe_key, inserted=True, alert_id=alert_id)
    _queue(session, _Pending(notifier, alert_id, title, message))
    return AlertResult(kind, dedupe_key, inserted=True, alert_id=alert_id, queued=True)


def clear_alert(session: Session, dedupe_key: str) -> bool:
    """Delete the event with ``dedupe_key`` so the alert can fire again. True if one existed."""
    deleted = session.execute(
        delete(AlertEvent).where(AlertEvent.dedupe_key == dedupe_key).returning(AlertEvent.id)
    ).first()
    return deleted is not None


@contextmanager
def _guarded(session: Session, what: str) -> Iterator[None]:
    """Run one evaluation in a savepoint; an error is logged and undone, never raised."""
    try:
        with session.begin_nested():
            yield
    except Exception:
        log.warning("alerts: %s failed; skipped this time", what, exc_info=True)


def _run(session: Session, what: str, step: Callable[[], list[AlertResult]]) -> list[AlertResult]:
    results: list[AlertResult] = []
    with _guarded(session, what):
        results = step()
    return results


# --------------------------------------------------------------------------- wording helpers


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _date_text(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _when_text(moment: dt.datetime) -> str:
    local = moment.astimezone()
    hour = local.strftime("%I:%M %p").lstrip("0")
    return f"{local:%b} {local.day} at {hour}"


def _age_text(delta: dt.timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f"{hours} hours"
    return _days(hours // 24)


def _institution(state: status.ItemStatus) -> str:
    """The connection's name to start an alert ("Tartan Bank sync failed", BANK-7); with another
    login at the same bank, the label that tells them apart ("Houndstooth Bank (Cash Rewards
    ··5678) sync failed")."""
    return state.display_name or DEFAULT_INSTITUTION


def _bank(state: status.ItemStatus) -> str:
    """The connection's name inside a sentence ("Reconnect Tartan Bank", "… your bank")."""
    return state.display_name or "your bank"


# --------------------------------------------------------------------------- after a sync


def evaluate_after_sync(
    session: Session, notifier: Notifier, outcome: SyncOutcome
) -> list[AlertResult]:
    """Alerts after one sync run (see the module docstring for the rules and keys).

    ``services.sync`` calls it in a transaction of its own after ``success``, ``not_ready`` and
    every failure it records; notifications go out when that transaction commits. Never raises.
    """
    if outcome.status == "skipped":
        return []
    loaded: tuple[PlaidItem, AppSettings, status.ItemStatus] | None = None
    today = clock.today()
    with _guarded(session, f"reading Item {outcome.item_id} for its alerts"):
        loaded = _load_item(session, outcome.item_id, today)
    if loaded is None:
        return []
    item, settings, state = loaded
    results: list[AlertResult] = []
    results += _run(
        session,
        "sync problem alerts",
        lambda: _sync_problems(session, notifier, settings, item, outcome, state, today),
    )
    results += _run(
        session, "the consent reminder", lambda: _consent(session, notifier, item, state)
    )
    if outcome.ok:
        results += evaluate_budgets(session, notifier, today=today)
    if outcome.status == "success":
        results += _run(
            session,
            "large-charge alerts",
            lambda: _large_charges(session, notifier, settings, item, outcome, today),
        )
    return results


def _load_item(
    session: Session, item_id: int, today: dt.date
) -> tuple[PlaidItem, AppSettings, status.ItemStatus] | None:
    item = session.get(PlaidItem, item_id)
    if item is None:
        return None
    settings = get_app_settings(session)
    state = status.item_status(
        item,
        now=clock.now(),
        today=today,
        reminder_days=settings.consent_reminder_days,
        label=status.connection_label(session, item),
    )
    return item, settings, state


def _sync_problems(
    session: Session,
    notifier: Notifier,
    settings: AppSettings,
    item: PlaidItem,
    outcome: SyncOutcome,
    state: status.ItemStatus,
    today: dt.date,
) -> list[AlertResult]:
    """ALR-2: reconnect needed (once per occurrence), a failed run (per code and day), stale."""
    if item.status == "healthy":
        _close_reconnect(session, item.id)
    if not settings.sync_alerts_enabled:
        return []
    results: list[AlertResult] = []
    name = _institution(state)
    stale_note = _stale_sentence(state) if state.stale else ""
    if item.status == "needs_reconnect" or outcome.needs_reconnect:
        results.append(_reconnect(session, notifier, item, outcome, state, stale_note))
    elif outcome.status == "error":
        code = outcome.error_code or "UNKNOWN"
        detail = _error_detail(
            outcome.error_code,
            outcome.error_type,
            outcome.display_message,
            state.display_name,
            amex=state.is_amex,
        )
        message = f"{detail}. Code: {code}."
        if stale_note:
            message += f" {stale_note}"
        results.append(
            record_and_notify(
                session,
                notifier,
                kind=KIND_SYNC_ERROR,
                dedupe_key=f"sync-error:{item.id}:{code}:{today.isoformat()}",
                title=f"{name} sync failed",
                message=message,
                item_id=item.id,
            )
        )
    if state.stale:
        announced = any(r.queued for r in results)
        results.append(
            record_and_notify(
                session,
                notifier,
                kind=KIND_STALE,
                dedupe_key=f"stale:{item.id}:{today.isoformat()}",
                title=f"{name} data is out of date",
                message=f"{stale_note} {_stale_advice(item)}",
                item_id=item.id,
                notify=not announced,
            )
        )
    return results


def _error_detail(
    code: str | None,
    error_type: str | None,
    display: str | None,
    institution: str | None = None,
    *,
    amex: bool = False,
) -> str:
    """Plain words for a failed sync (the Connection page's, naming the institution), without a
    final full stop."""
    from budgetapp.services import sync as sync_service  # sync imports this module

    text = (
        sync_service.describe_error(code, error_type, institution, amex=amex)
        or display
        or "Plaid reported an error"
    )
    return text.strip().rstrip(".")


def _reconnect(
    session: Session,
    notifier: Notifier,
    item: PlaidItem,
    outcome: SyncOutcome,
    state: status.ItemStatus,
    stale_note: str,
) -> AlertResult:
    code = outcome.error_code or item.error_code
    detail = _error_detail(
        code,
        outcome.error_type,
        item.error_display_message,
        state.display_name,
        amex=state.is_amex,
    )
    message = f"{detail}. Open the Connection page and click Reconnect."
    if stale_note:
        message += f" {stale_note}"
    return record_and_notify(
        session,
        notifier,
        kind=KIND_RECONNECT,
        dedupe_key=f"reconnect:{item.id}:open",
        title=f"Reconnect {_bank(state)}",
        message=message,
        item_id=item.id,
    )


def _close_reconnect(session: Session, item_id: int) -> None:
    """The Item is healthy again: the open reconnect occurrence ends (the next one alerts)."""
    row = session.scalars(
        select(AlertEvent).where(AlertEvent.dedupe_key == f"reconnect:{item_id}:open")
    ).one_or_none()
    if row is not None:
        row.dedupe_key = f"reconnect:{item_id}:{row.id}"
        session.flush()


def _stale_sentence(state: status.ItemStatus) -> str:
    now = clock.now()
    if state.last_sync_success_at is None:
        age = _age_text(now - state.linked_at)
        what = state.display_name or "this connection"
        return f"No transactions have arrived since {what} was linked {age} ago."
    age = _age_text(now - state.last_sync_success_at)
    return f"The last successful sync was {age} ago ({_when_text(state.last_sync_success_at)})."


def _stale_advice(item: PlaidItem) -> str:
    if item.status == "needs_reconnect":
        return "Reconnect on the Connection page to catch up."
    if item.status == "error":
        return "The Connection page shows the last error."
    return "Check the Connection page; Sync now tries again."


def _consent(
    session: Session, notifier: Notifier, item: PlaidItem, state: status.ItemStatus
) -> list[AlertResult]:
    """SYNC-6: renewal due within ``consent_reminder_days``, once per due date, naming the
    institution. A bank whose consent has no end date gets none (BANK-6).

    Not while the Item needs reconnecting (as on the Connection page): reconnecting is update mode,
    which renews the consent too, and the reconnect alert already asks for it.
    """
    if (
        not state.consent_warning
        or state.consent_due_at is None
        or state.consent_days_left is None
        or item.status == "needs_reconnect"
    ):
        return []
    due = state.consent_due_at.astimezone().date()
    name = _bank(state)
    left = state.consent_days_left
    if left > 1:
        when = f"ends on {_date_text(due)} ({_days(left)} left)"
    elif left == 1:
        when = f"ends tomorrow, {_date_text(due)}"
    elif left == 0:
        when = "ends today"
    else:
        when = f"ended on {_date_text(due)}"
    return [
        record_and_notify(
            session,
            notifier,
            kind=KIND_CONSENT,
            dedupe_key=f"consent:{item.id}:{due.isoformat()}",
            title=f"Renew your {state.display_name or 'bank'} connection",
            message=(
                f"Plaid's access to {name} {when}. Renew it on the Connection page so syncing "
                "keeps working; it takes a minute and keeps this connection."
            ),
            item_id=item.id,
        )
    ]


def _large_charges(
    session: Session,
    notifier: Notifier,
    settings: AppSettings,
    item: PlaidItem,
    outcome: SyncOutcome,
    today: dt.date,
) -> list[AlertResult]:
    """ALR-3: charges over ``large_charge_threshold``, once per charge; spending only.

    New charges (``outcome.inserted_transaction_ids``), plus pending charges the app had at or
    under the threshold that this sync stored above it (``outcome.pending_raises``: a tip or
    hotel incidentals added at posting), dated at most :data:`LARGE_CHARGE_DAYS` days before the
    previous successful sync. Both are keyed by the pending row's Plaid id when there is one, so
    a charge that already alerted while pending stays silent when it posts. Only spending counts
    (W3-PLAN C5): a row whose category is of kind ``expense`` after categorization, on any
    account; paying the card from checking, moving money to savings and income never alert.
    """
    threshold = settings.large_charge_threshold
    if threshold is None or outcome.initial_history_load:
        return []
    raised = {r.transaction_id: r for r in outcome.pending_raises if r.previous_amount <= threshold}
    ids = [*outcome.inserted_transaction_ids, *raised]
    if not ids:
        return []
    base = today  # a catch-up sync after a break also looks at the break's charges
    if outcome.previous_success_at is not None:
        base = min(today, outcome.previous_success_at.astimezone().date())
    since = base - dt.timedelta(days=LARGE_CHARGE_DAYS)
    rows = list(
        session.scalars(
            select(Transaction)
            .where(
                Transaction.id.in_(ids),
                Transaction.source == "plaid",
                Transaction.amount > threshold,
                Transaction.date >= since,
                Transaction.category.has(kind="expense"),  # spending only (C5)
            )
            .order_by(Transaction.date, Transaction.id)
        )
    )
    by_key: dict[str, Transaction] = {}
    for row in rows:  # a pending row and the posting naming it share a key: keep the first
        charge = raised[row.id].pending_plaid_id if row.id in raised else None
        by_key.setdefault(
            f"large-charge:{charge or row.pending_transaction_id or row.plaid_transaction_id}", row
        )
    seen = set(
        session.scalars(select(AlertEvent.dedupe_key).where(AlertEvent.dedupe_key.in_(by_key)))
    )
    new = [(key, row) for key, row in by_key.items() if key not in seen]
    each = len(new) <= MAX_CHARGE_NOTIFICATIONS
    limit = format_money(threshold)
    results: list[AlertResult] = []
    for key, row in new:
        when = f"{row.date:%b} {row.date.day}"
        before = raised.get(row.id)
        if before is None:
            state = "pending charge" if row.pending else "charge"
            message = f"A {state} on {when} is over your {limit} large-charge alert."
        elif row.pending:
            message = (
                f"A pending charge on {when} went up from {format_money(before.previous_amount)}"
                f" and is over your {limit} large-charge alert."
            )
        else:
            message = (
                f"A charge on {when} posted for more than the pending "
                f"{format_money(before.previous_amount)} and is over your {limit} large-charge "
                "alert."
            )
        results.append(
            record_and_notify(
                session,
                notifier,
                kind=KIND_LARGE_CHARGE,
                dedupe_key=key,
                title=f"{format_money(row.amount)} at {_merchant(row)}",
                message=message,
                transaction_id=row.id,
                item_id=item.id,
                notify=each,
            )
        )
    if each:
        return results
    shown = ", ".join(
        f"{_merchant(row)} {format_money(row.amount)}"
        for _key, row in new[:MAX_CHARGE_NOTIFICATIONS]
    )
    more = len(new) - MAX_CHARGE_NOTIFICATIONS
    results.append(
        record_and_notify(
            session,
            notifier,
            kind=KIND_LARGE_CHARGE,
            dedupe_key=f"large-charges:{item.id}:{outcome.sync_run_id or new[0][1].id}",
            title=f"{len(new)} new charges over {limit}",
            message=f"{shown} and {more} more. They are on the Transactions page.",
            item_id=item.id,
        )
    )
    return results


def _merchant(row: Transaction) -> str:
    return row.merchant_name or row.name


# --------------------------------------------------------------------------- after an import


def evaluate_after_import(
    session: Session, notifier: Notifier, outcome: ImportOutcome
) -> list[AlertResult]:
    """Budget thresholds (ALR-1) after a CSV import that inserted or paired rows. CSV imports
    never raise large-charge alerts (ALR-3). Runs inside the import's transaction (the caller
    commits, then notifications go out); never raises."""
    if outcome.dry_run or not outcome.changed:
        return []
    return evaluate_budgets(session, notifier)


# --------------------------------------------------------------------------- budgets (ALR-1)


def budget_key(category_id: int | None, period: Period, threshold: int) -> str:
    """The dedupe key of a budget threshold: ``budget:<category id|overall>:<token>:<t>`` where the
    token is the period's start date, or the year for an annual budget (a ``year`` period)."""
    scope = "overall" if category_id is None else str(category_id)
    token = str(period.start.year) if period.kind == periods.YEAR else period.start.isoformat()
    return f"budget:{scope}:{token}:{threshold}"


@dataclass(frozen=True, slots=True)
class _BudgetTarget:
    """One budget to compare with its thresholds: a monthly category line, the overall target or
    an annual category budget."""

    category_id: int | None
    name: str
    period: Period
    budget: Decimal
    carry_in: Decimal
    available: Decimal
    spent: Decimal
    days_left: int | None = None
    flagged_days: int | None = None

    @property
    def annual(self) -> bool:
        return self.period.kind == periods.YEAR

    @property
    def scope(self) -> str:
        return "overall" if self.category_id is None else str(self.category_id)

    def crossed(self, threshold: int) -> bool:
        return self.spent > 0 and self.spent * 100 >= self.available * threshold

    def owns(self, token: str) -> bool:
        """Whether an existing event's period token belongs to this budget's period."""
        if self.annual:
            return token == str(self.period.start.year)
        try:
            day = dt.date.fromisoformat(token) if len(token) == 10 else None
        except ValueError:
            day = None
        if day is None:
            return False
        return abs(day - self.period.start) <= PERIOD_START_SLACK and day <= self.period.end


def evaluate_budgets(
    session: Session, notifier: Notifier, *, today: dt.date | None = None
) -> list[AlertResult]:
    """Budget threshold alerts (ALR-1) for the current budget period and calendar year.

    When ``app_settings.budget_alerts_enabled``: every monthly category budget the Budgets page
    lists (hidden categories are left out), the overall target when one is set, and every annual
    category budget, against ``app_settings.alert_thresholds``. See the module docstring for the
    crossing rule, the keys, the re-arming and the one-notification-per-budget rule. Called after
    each sync, import and edit that changes categories, before the caller commits. Never raises.
    """
    results: list[AlertResult] = []
    with _guarded(session, "budget alerts"):
        results = _evaluate_budgets(session, notifier, today or clock.today())
    return results


def _evaluate_budgets(session: Session, notifier: Notifier, today: dt.date) -> list[AlertResult]:
    settings = get_app_settings(session)
    thresholds = sorted({int(t) for t in settings.alert_thresholds or () if int(t) > 0})
    if not settings.budget_alerts_enabled or not thresholds:
        return []
    period = budgets.current_period(session, today=today)
    ov = budgets.overview(session, period, today=today)
    flagged = period.days if period.kind == periods.STATEMENT and period.flagged else None
    targets = [
        _BudgetTarget(
            line.category.id,
            line.category.name,
            period,
            line.budget,
            line.carry_in,
            line.available,
            line.spent,
            ov.days_left,
            flagged,
        )
        for line in ov.lines
    ]
    if ov.overall.has_target and ov.overall.budget is not None:
        target = ov.overall.budget
        targets.append(
            _BudgetTarget(
                None,
                "Overall",
                period,
                target,
                Decimal(0),
                target,
                ov.overall.spent,
                ov.days_left,
                flagged,
            )
        )
    annual = budgets.annual_overview(session, today.year, today=today)
    targets += [
        _BudgetTarget(
            line.category.id,
            line.category.name,
            annual.period,
            line.budget,
            Decimal(0),
            line.budget,
            line.spent,
        )
        for line in annual.lines
    ]
    existing = _budget_events(session)
    results: list[AlertResult] = []
    for target in targets:
        results += _evaluate_target(session, notifier, target, thresholds, existing)
    return results


def _budget_events(session: Session) -> dict[tuple[str, int], list[tuple[str, str]]]:
    """Recent budget events: ``(scope, threshold)`` → ``[(period token, dedupe key)]``."""
    found: dict[tuple[str, int], list[tuple[str, str]]] = {}
    keys = session.scalars(
        select(AlertEvent.dedupe_key).where(
            AlertEvent.kind == KIND_BUDGET, AlertEvent.created_at >= clock.now() - _BUDGET_LOOKBACK
        )
    )
    for key in keys:
        match = _BUDGET_KEY.match(key)
        if match is not None:
            scope, threshold = match["scope"], int(match["threshold"])
            found.setdefault((scope, threshold), []).append((match["token"], key))
    return found


def _evaluate_target(
    session: Session,
    notifier: Notifier,
    target: _BudgetTarget,
    thresholds: Sequence[int],
    existing: dict[tuple[str, int], list[tuple[str, str]]],
) -> list[AlertResult]:
    mine = {
        t: [key for token, key in existing.get((target.scope, t), ()) if target.owns(token)]
        for t in thresholds
    }
    crossed = [t for t in thresholds if target.crossed(t)]
    for t in thresholds:
        if t not in crossed:
            for key in mine[t]:
                clear_alert(session, key)
    fired = [t for t in crossed if mine[t]]
    new = [t for t in crossed if not mine[t]]
    if not new:
        return []
    top = max(new)
    announce = not fired or top > max(fired)
    results = []
    for t in new:
        title, message = _budget_text(target, t)
        results.append(
            record_and_notify(
                session,
                notifier,
                kind=KIND_BUDGET,
                dedupe_key=budget_key(target.category_id, target.period, t),
                title=title,
                message=message,
                category_id=target.category_id,
                notify=announce and t == top,
            )
        )
    return results


def _budget_text(target: _BudgetTarget, threshold: int) -> tuple[str, str]:
    """The notification for ``target`` crossing ``threshold`` (title, message)."""
    spent, available = target.spent, target.available
    over = spent > available and (threshold <= 100 or available <= 0)
    used_up = not over and threshold == 100 and spent == available
    year = target.period.start.year
    if target.category_id is None:
        title = (
            "Over your overall budget"
            if over
            else "Overall budget used up"
            if used_up
            else f"Overall budget: {threshold}% used"
        )
    elif target.annual:
        title = (
            f"{target.name}: over the {year} budget"
            if over
            else f"{target.name}: {year} budget used up"
            if used_up
            else f"{target.name}: {threshold}% of the {year} budget used"
        )
    else:
        title = (
            f"{target.name}: over budget"
            if over
            else f"{target.name}: budget used up"
            if used_up
            else f"{target.name}: {threshold}% of budget used"
        )

    if target.annual:
        span = f"so far in {year}"
    elif target.period.kind == periods.STATEMENT:
        end = target.period.end
        span = f"this cycle (it ends {end:%b} {end.day})"
    else:
        span = f"in {target.period.start:%B}"
    if available > 0:
        message = f"{format_money(spent)} of {format_money(available)} spent {span}"
        remaining = available - spent
        if remaining > 0:
            message += f", {format_money(remaining)} left"
        elif remaining < 0:
            message += f", {format_money(-remaining)} over"
    elif target.carry_in < 0:
        message = (
            f"{format_money(spent)} spent {span}, with nothing left after last period's "
            "overspending"
        )
    else:
        message = f"{format_money(spent)} spent {span} against a budget of " + format_money(
            target.budget
        )
    if target.days_left:
        message += f", {_days(target.days_left)} to go"
    message += "."
    if target.flagged_days:
        message += (
            f" This cycle is {target.flagged_days} days long, so check the Statements page "
            "for a missing or wrong closing date."
        )
    return title, message
