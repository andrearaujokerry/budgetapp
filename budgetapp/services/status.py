"""Connection status for the layout, dashboard, Connection page and alerts (SYNC-5, SYNC-6).

``stale`` means no successful sync for 36 hours (counted from linking when there was never a
success), whatever the Item status says (ARCHITECTURE §8). Consent is due at
``consent_expires_at`` from ``/item/get``, or ``last_consented_at + 365 days`` when Amex leaves it
null; a warning starts ``app_settings.consent_reminder_days`` before.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import PlaidItem, SyncRun, get_app_settings

STALE_AFTER = dt.timedelta(hours=36)
CONSENT_FALLBACK = dt.timedelta(days=365)
#: A ``running`` sync row older than this is shown as not running (it was probably interrupted).
RUNNING_WINDOW = dt.timedelta(minutes=30)

_SEVERITY = {"needs_reconnect": 4, "error": 3, "stale": 2, "healthy": 1}
_LABELS = {
    "not_linked": "Not linked",
    "healthy": "Connected",
    "stale": "Data is stale",
    "error": "Sync problem",
    "needs_reconnect": "Reconnect needed",
}


@dataclass(frozen=True, slots=True)
class ItemStatus:
    """Status of one Plaid Item."""

    item_id: int
    plaid_item_id: str
    institution_name: str | None
    #: ``plaid_items.status``: healthy | needs_reconnect | error
    item_status: str
    #: overall state for display: healthy | stale | error | needs_reconnect
    state: str
    error_code: str | None
    error_message: str | None
    linked_at: dt.datetime
    last_sync_success_at: dt.datetime | None
    last_sync_attempt_at: dt.datetime | None
    plaid_last_update_at: dt.datetime | None
    stale: bool
    syncing: bool
    history_complete: bool
    consent_expires_at: dt.datetime | None
    consent_due_at: dt.datetime
    consent_days_left: int
    consent_warning: bool
    consented_products: tuple[str, ...]
    liabilities_status: str | None


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """Overall status: the worst state across Items (``not_linked`` when there is none)."""

    state: str
    label: str
    items: tuple[ItemStatus, ...]

    @property
    def linked(self) -> bool:
        return bool(self.items)

    @property
    def primary(self) -> ItemStatus | None:
        """The Item to show first (the worst one)."""
        return self.items[0] if self.items else None

    @property
    def stale(self) -> bool:
        return any(i.stale for i in self.items)

    @property
    def needs_reconnect(self) -> bool:
        return any(i.item_status == "needs_reconnect" for i in self.items)

    @property
    def consent_warning(self) -> bool:
        return any(i.consent_warning for i in self.items)

    @property
    def syncing(self) -> bool:
        return any(i.syncing for i in self.items)

    @property
    def last_sync_success_at(self) -> dt.datetime | None:
        stamps = [i.last_sync_success_at for i in self.items if i.last_sync_success_at]
        return min(stamps) if stamps else None


def consent_due_at(item: PlaidItem) -> dt.datetime:
    """When consent must be renewed: Plaid's expiry, else ``last_consented_at`` + 365 days."""
    return item.consent_expires_at or (item.last_consented_at + CONSENT_FALLBACK)


def item_status(
    item: PlaidItem,
    *,
    now: dt.datetime,
    today: dt.date,
    reminder_days: int,
    syncing: bool = False,
) -> ItemStatus:
    """Status of one Item at ``now`` (pure; no database access)."""
    reference = item.last_sync_success_at or item.linked_at
    stale = now - reference > STALE_AFTER
    if item.status in ("needs_reconnect", "error"):
        state = item.status
    elif stale:
        state = "stale"
    else:
        state = "healthy"
    due = consent_due_at(item)
    days_left = (due.astimezone().date() - today).days
    return ItemStatus(
        item_id=item.id,
        plaid_item_id=item.plaid_item_id,
        institution_name=item.institution_name,
        item_status=item.status,
        state=state,
        error_code=item.error_code,
        error_message=item.error_message,
        linked_at=item.linked_at,
        last_sync_success_at=item.last_sync_success_at,
        last_sync_attempt_at=item.last_sync_attempt_at,
        plaid_last_update_at=item.plaid_last_update_at,
        stale=stale,
        syncing=syncing,
        history_complete=item.history_complete_at is not None,
        consent_expires_at=item.consent_expires_at,
        consent_due_at=due,
        consent_days_left=days_left,
        consent_warning=days_left <= reminder_days,
        consented_products=tuple(item.consented_products or ()),
        liabilities_status=item.liabilities_status,
    )


def connection_status(session: Session, *, now: dt.datetime | None = None) -> ConnectionStatus:
    """Status of every Item, worst first, plus the overall state and a short label."""
    today = now.astimezone().date() if now is not None else clock.today()
    now = now or clock.now()
    items = list(session.scalars(select(PlaidItem).order_by(PlaidItem.id)))
    if not items:
        return ConnectionStatus("not_linked", _LABELS["not_linked"], ())
    reminder_days = get_app_settings(session).consent_reminder_days
    running_ids = set(
        session.scalars(
            select(SyncRun.item_id).where(
                SyncRun.status == "running", SyncRun.started_at > now - RUNNING_WINDOW
            )
        )
    )
    statuses = [
        item_status(
            item, now=now, today=today, reminder_days=reminder_days, syncing=item.id in running_ids
        )
        for item in items
    ]
    statuses.sort(key=lambda s: (-_SEVERITY[s.state], s.item_id))
    state = statuses[0].state
    return ConnectionStatus(state, _LABELS[state], tuple(statuses))
