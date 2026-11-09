"""Connection status for the layout, dashboard, Connection page and alerts (SYNC-5, SYNC-6, BANK-6).

``stale`` means no successful sync for 36 hours (counted from linking when there was never a
success), whatever the Item status says (ARCHITECTURE §8). Consent is due at
``consent_expires_at`` from ``/item/get``; only American Express (``ins_10``) falls back to
``last_consented_at + 365 days`` when Plaid leaves it null (Amex asks every 12 months, RESEARCH
T2.5). Any other bank without a date from Plaid has no consent due date (``consent_due_at`` is
``None``, no warning). A warning starts ``app_settings.consent_reminder_days`` before.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import Account, PlaidItem, SyncRun, get_app_settings

STALE_AFTER = dt.timedelta(hours=36)
#: American Express only: a year after the last link or renewal when Plaid sends no date.
CONSENT_FALLBACK = dt.timedelta(days=365)
#: Plaid's institution id for American Express (RESEARCH T3.1).
AMEX_INSTITUTION_ID = "ins_10"
#: ``accounts.type`` of a credit card (``services.accounts.CARD``).
CARD_TYPE = "credit"
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
    #: Plaid's institution id (``ins_10`` is American Express)
    institution_id: str | None
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
    #: Plaid's date, or for American Express a year after the last consent; ``None`` for a bank
    #: whose consent doesn't expire (then no days, no warning)
    consent_due_at: dt.datetime | None
    consent_days_left: int | None
    consent_warning: bool
    consented_products: tuple[str, ...]
    liabilities_status: str | None
    #: whether the connection has a credit card (Liabilities apply only then; BANK-7)
    has_cards: bool = True

    @property
    def name(self) -> str:
        """The institution's name for messages ("TD Bank"), or a neutral one."""
        return self.institution_name or "A bank connection"

    @property
    def is_amex(self) -> bool:
        """American Express (``ins_10``): its consent is renewed every 12 months."""
        return is_amex(self)

    @property
    def consent_estimated(self) -> bool:
        """The due date is the app's estimate (American Express, no date from Plaid)."""
        return self.consent_expires_at is None and self.consent_due_at is not None

    @property
    def consent_expired(self) -> bool:
        return self.consent_days_left is not None and self.consent_days_left < 0


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
    def reconnect_names(self) -> tuple[str, ...]:
        """The institutions that need the user to sign in again, worst first (banners)."""
        return tuple(i.name for i in self.items if i.item_status == "needs_reconnect")

    @property
    def consent_warning(self) -> bool:
        return any(i.consent_warning for i in self.items)

    @property
    def consent_item(self) -> ItemStatus | None:
        """The connection whose consent is due first among those with a warning (the banner)."""
        due = [i for i in self.items if i.consent_warning and i.consent_days_left is not None]
        return min(due, key=lambda i: (i.consent_days_left, i.item_id)) if due else None

    @property
    def consent_expired(self) -> bool:
        return any(i.consent_expired for i in self.items)

    @property
    def syncing(self) -> bool:
        return any(i.syncing for i in self.items)

    @property
    def last_sync_success_at(self) -> dt.datetime | None:
        stamps = [i.last_sync_success_at for i in self.items if i.last_sync_success_at]
        return min(stamps) if stamps else None


def is_amex(item: PlaidItem | ItemStatus) -> bool:
    """American Express: Plaid's ``ins_10``. An Item saved without an institution id (never by
    this app, but a database is kept for years) counts when its name says American Express."""
    if item.institution_id:
        return item.institution_id == AMEX_INSTITUTION_ID
    return "american express" in (item.institution_name or "").casefold()


def consent_due_at(item: PlaidItem) -> dt.datetime | None:
    """When consent must be renewed: Plaid's expiry; for American Express without one,
    ``last_consented_at`` + 365 days; otherwise ``None`` (BANK-6)."""
    if item.consent_expires_at is not None:
        return item.consent_expires_at
    if is_amex(item):
        return item.last_consented_at + CONSENT_FALLBACK
    return None


def item_status(
    item: PlaidItem,
    *,
    now: dt.datetime,
    today: dt.date,
    reminder_days: int,
    syncing: bool = False,
    has_cards: bool = True,
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
    days_left = (due.astimezone().date() - today).days if due is not None else None
    return ItemStatus(
        item_id=item.id,
        plaid_item_id=item.plaid_item_id,
        institution_name=item.institution_name,
        institution_id=item.institution_id,
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
        consent_warning=days_left is not None and days_left <= reminder_days,
        consented_products=tuple(item.consented_products or ()),
        liabilities_status=item.liabilities_status,
        has_cards=has_cards,
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
    with_cards = set(
        session.scalars(
            select(Account.item_id).where(Account.type == CARD_TYPE, Account.item_id.is_not(None))
        )
    )
    statuses = [
        item_status(
            item,
            now=now,
            today=today,
            reminder_days=reminder_days,
            syncing=item.id in running_ids,
            has_cards=item.id in with_cards,
        )
        for item in items
    ]
    statuses.sort(key=lambda s: (-_SEVERITY[s.state], s.item_id))
    state = statuses[0].state
    return ConnectionStatus(state, _LABELS[state], tuple(statuses))
