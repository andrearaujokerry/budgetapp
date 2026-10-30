"""Outcomes of a sync or a CSV import, passed to ``services.alerts`` (ARCHITECTURE §8, §11)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What one ``sync_item`` run did, for alerts and the UI.

    * ``status``: ``success`` | ``error`` | ``skipped`` (another sync held the lock) |
      ``not_ready`` (Plaid's ``NOT_READY``: recorded as a success with no changes).
    * ``inserted_transaction_ids``: rows this sync inserted for charges the app had not seen
      before (ALR-3 large-charge alerts look only at these, dated within the last 7 days). A row
      that replaces one the app already had is left out, so each charge appears once: a posted
      charge naming its pending row, one matched to a removed charge (with or without the user's
      edits), or a re-delivery after Plaid re-issued an account. ``added`` still counts every
      inserted row.
    * ``initial_history_load``: True while the Item has never completed its historical pull,
      including the run that first sees ``HISTORICAL_UPDATE_COMPLETE``; large-charge alerts skip
      such runs so two years of backfill do not notify.
    * ``history_completed_now``: this run saw ``HISTORICAL_UPDATE_COMPLETE`` for the first time.
    * On failure: ``error_type``/``error_code``/``error_message``/``display_message`` from Plaid (or
      ``error_code='INTERNAL'`` with the exception class for any other failure,
      ``internal_error=True``), and ``item_status`` after the run (``needs_reconnect`` | ``error``).
    """

    item_id: int
    trigger: str
    status: str
    sync_run_id: int | None = None
    added: int = 0
    modified: int = 0
    removed: int = 0
    inserted_transaction_ids: tuple[int, ...] = ()
    initial_history_load: bool = False
    history_completed_now: bool = False
    update_status: str | None = None
    item_status: str | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    display_message: str | None = None
    internal_error: bool = False
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "not_ready")

    @property
    def needs_reconnect(self) -> bool:
        return self.item_status == "needs_reconnect"

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.removed)


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What one Amex CSV import did (SYNC-8).

    CSV imports never trigger large-charge alerts (ALR-3); budget thresholds (ALR-1) are
    re-evaluated after an import that inserted rows.
    """

    account_id: int
    filename: str
    csv_import_id: int | None = None
    row_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0
    matched_plaid_count: int = 0
    error_count: int = 0
    inserted_transaction_ids: tuple[int, ...] = ()
    date_min: dt.date | None = None
    date_max: dt.date | None = None
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return self.inserted_count > 0 or self.matched_plaid_count > 0
