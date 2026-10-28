"""Alerts with de-duplication (ALR-1..3, SYNC-6) — ARCHITECTURE §11.

:func:`record_and_notify` is the only way an alert is raised: it inserts an ``alert_events`` row
keyed by ``dedupe_key`` (``INSERT … ON CONFLICT DO NOTHING``) and queues a notification ONLY when
the row was inserted, so each alert fires once. The notification is sent after the caller's
transaction COMMITS (never while it holds locks), and only if the row is still there then: a
rolled-back transaction or savepoint sends nothing, so a later run can raise the alert again
without a duplicate. :func:`clear_alert` deletes an event so it can fire again (e.g. spending fell
back below a threshold after a pending hold dropped).

The evaluation functions below are stubs until the ALR package implements them; callers (sync,
CSV import, edits) already call them. Suggested dedupe keys: ``budget:<category_id|overall>:
<period start>:<threshold>``, ``sync-error:<item_id>:<error_code>:<YYYY-MM-DD>``,
``reconnect:<item_id>:<occurrence>``, ``stale:<item_id>:<YYYY-MM-DD>``, ``consent:<item_id>:<due
date>``, ``large-charge:<plaid or pending transaction id>``. Notifications that cannot use the
database (Keychain approval needed, database down) use ``secrets.claim_daily_marker`` instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, delete, event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import AlertEvent
from budgetapp.services.events import ImportOutcome, SyncOutcome
from budgetapp.services.notify import Notifier

log = logging.getLogger(__name__)

_PENDING = "budgetapp.alerts.pending"


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


def evaluate_after_sync(
    session: Session, notifier: Notifier, outcome: SyncOutcome
) -> list[AlertResult]:
    """Alerts after a sync run (stub: returns ``[]`` until the ALR package implements it).

    Contract (ARCHITECTURE §11):

    * ALR-2 sync problems, when ``app_settings.sync_alerts_enabled``: a failed run (Plaid or
      ``INTERNAL``) alerts once per Item, error code and day; ``needs_reconnect`` once per
      occurrence; stale data (no success for 36 h, ``services.status``) once a day.
    * SYNC-6 consent: renewal due within ``consent_reminder_days`` alerts once per due date.
    * ALR-1 budgets: after a successful run that changed data, call :func:`evaluate_budgets`.
    * ALR-3 large charges, when ``large_charge_threshold`` is set: only rows in
      ``outcome.inserted_transaction_ids``, dated within the last 7 days, and never when
      ``outcome.initial_history_load``; once per charge and not again when it posts (key on the
      pending transaction id the posted row names).
    """
    return []


def evaluate_after_import(
    session: Session, notifier: Notifier, outcome: ImportOutcome
) -> list[AlertResult]:
    """Alerts after a CSV import (stub: returns ``[]``).

    Contract: re-evaluate budget thresholds (:func:`evaluate_budgets`) when the import inserted or
    paired rows. CSV imports never raise large-charge alerts (ALR-3).
    """
    return []


def evaluate_budgets(
    session: Session, notifier: Notifier, *, today: dt.date | None = None
) -> list[AlertResult]:
    """Budget threshold alerts for the current period (stub: returns ``[]``).

    Contract (ALR-1, ARCHITECTURE §11): when ``app_settings.budget_alerts_enabled``, for each
    category budget and the overall target, with spending from ``spending.period_spending`` (same
    basis and pending setting as the Budgets page), threshold ``t`` in
    ``app_settings.alert_thresholds`` is crossed when ``spent > 0`` and
    ``spent >= available × t / 100`` (a zero or negative available fires on the first spending).
    Each fires once per category, period and threshold; when spending falls back below a threshold
    its event is cleared (:func:`clear_alert`) so it can fire again. Called after each sync, import
    and edit that changes categories.
    """
    return []
