"""CSV ↔ Plaid reconciliation (SYNC-8) — STUB until the CSV package replaces this module.

Contract (ARCHITECTURE §6 "CSV ↔ Plaid reconciliation"); keep these signatures:

* :func:`reconcile_item` is a FULL pass, run after every sync that inserted rows, after every CSV
  import and after attaching a manual account. The caller holds ``db.ledger_lock``.
  - Candidates: unpaired CSV rows (``source = 'csv'``) and unpaired Plaid rows (no
    ``csv_reference``) across all accounts of the Item, with equal amounts, the CSV date within
    ``[COALESCE(authorized_date, date) − 3 days, date]`` — widened to ``[date − 30 days, date]``
    when ``authorized_date`` is null.
  - One global one-to-one assignment, best first by date distance, then description similarity
    (CSV ``Appears On Your Statement As``/``Description`` vs Plaid
    ``original_description``/``name``).
  - A pair moves the CSV row's user state onto the Plaid row (the Plaid row's own category and
    splits win; notes and tags merge), copies ``csv_reference`` and ``csv_import_id`` onto it,
    and deletes the CSV row — in THIS order, because ``transactions.csv_reference`` is unique and
    not deferrable (CSV import relies on ``ON CONFLICT (csv_reference) DO NOTHING``):
    1. ``state = userstate.capture(csv_row)`` and keep its reference and import id (capture
       first: deleting the row also deletes its splits);
    2. ``session.delete(csv_row)`` and ``session.flush()``;
    3. set ``csv_reference`` / ``csv_import_id`` on the Plaid row, then
       ``categorize.apply_carried_state(categorizer, plaid_row, state)``.
    (Setting the reference and deleting in one flush makes SQLAlchemy UPDATE before DELETE and
    violates the unique key.)
  - Cutover: once the Item has ``history_complete_at``, each account's ``plaid_history_start`` is
    its earliest Plaid posted date; unpaired CSV rows dated on or after it become
    ``duplicate_status = 'suspect'`` (never rows the user marked ``kept``).
* :func:`attach_manual_account` (caller holds the ledger lock), before deleting the manual
  account:
  - drops manual rows whose Reference already exists and moves the rest (transactions, imports,
    rules, statements) to the Plaid account;
  - statements: a moved row that collides with one of the Plaid account's rows on
    ``(account_id, closing_date)`` or ``(account_id, plaid_issue_date)`` is MERGED into it
    (user-entered closing dates and reported values win over Liabilities snapshots; both notes
    kept), never inserted twice;
  - re-points ``app_settings.budget_cycle_account_id`` to the Plaid account when it names the
    manual one (otherwise ``ON DELETE SET NULL`` would silently switch statement-mode budgets to
    calendar months), and copies ``statement_closing_day`` (and ``notice``) when the Plaid account
    has none;
  - then deletes the manual account and runs the full pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What a reconciliation pass did.

    ``pairs`` lists ``(deleted_csv_transaction_id, plaid_transaction_id)`` (internal row ids);
    ``suspects_flagged`` counts CSV rows newly marked ``suspect`` by the cutover.
    """

    paired: int = 0
    pairs: tuple[tuple[int, int], ...] = ()
    suspects_flagged: int = 0


@dataclass(frozen=True, slots=True)
class AttachResult:
    """What attaching a manual CSV account to a Plaid account did."""

    moved_transactions: int = 0
    dropped_duplicates: int = 0
    reconcile: ReconcileResult = ReconcileResult()


def reconcile_item(session: Session, item_id: int) -> ReconcileResult:
    """Full CSV ↔ Plaid reconciliation pass for one Item (stub: does nothing yet)."""
    return ReconcileResult()


def attach_manual_account(session: Session, manual_id: int, plaid_account_id: int) -> AttachResult:
    """Move a manual CSV account's history onto a linked account (not built yet)."""
    raise NotImplementedError("attach_manual_account is built by the CSV package")
