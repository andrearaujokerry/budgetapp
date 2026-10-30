"""CSV ↔ Plaid reconciliation (SYNC-8) — ARCHITECTURE §6 "CSV ↔ Plaid reconciliation".

An Amex CSV download and Plaid can both deliver the same charge. This module pairs them, one to
one, so every charge is stored once, and attaches a manual (CSV-only) account to a linked one.

* :func:`reconcile_item` is a FULL pass, run after every sync that inserted rows, after every CSV
  import and after attaching a manual account. It takes ``db.ledger_lock`` (re-entrant, so a
  caller that already holds it, like the sync apply transaction, is fine).
  - Candidates: unpaired CSV rows (``source = 'csv'``; a paired CSV row no longer exists) and
    unpaired, posted Plaid rows (no ``csv_reference``) across all accounts of the Item, with equal
    amounts, the CSV date within ``[COALESCE(authorized_date, date) − 3 days, date]`` — widened to
    ``[date − 30 days, date]`` when ``authorized_date`` is null. Pending Plaid rows are not
    candidates: a CSV file holds posted charges, and a pending row is replaced when it posts
    (:func:`pending_twins` only tells the user which CSV rows will pair once it has).
  - A CSV row whose Reference holds a plausible posting date (below) pairs only with a Plaid row
    posted on that date or up to 3 days before it (:data:`POSTED_DATE_TOLERANCE`): equal when
    Plaid's ``date`` is the posting date, as Plaid documents; later if Plaid gave the charge
    date. Never a Plaid row posted after it: that is another charge of the same amount, and
    merging the CSV row into it would delete a real charge — a row of a file of the months
    before Plaid's history (nothing Plaid has was posted that early), last month's
    subscription, a row the user kept as a charge Plaid missed.
  - One global one-to-one assignment (:func:`match`), best first by date distance (CSV date vs
    ``COALESCE(authorized_date, date)``), then description similarity (CSV ``Appears On Your
    Statement As``/``Description`` vs Plaid ``original_description``/``name``/``merchant_name``),
    then a pair on the same account before one across accounts. Two identical charges therefore
    pair with two different rows.
  - A pair moves the CSV row's user state onto the Plaid row (the Plaid row's own category and
    splits win; notes and tags merge), copies ``csv_reference`` and ``csv_import_id`` onto it, and
    deletes the CSV row — in THIS order, because ``transactions.csv_reference`` is unique and not
    deferrable (CSV import relies on ``ON CONFLICT (csv_reference) DO NOTHING``): capture every
    state first (deleting a row also deletes its splits), delete the CSV rows and flush, then copy
    the references and call ``categorize.apply_carried_state``.
  - Cutover (:func:`apply_cutover`): once the Item has ``history_complete_at``, each account's
    ``plaid_history_start`` is its earliest Plaid posted date (set here when the sync did not set
    it yet); unpaired CSV rows dated on or after it become ``duplicate_status = 'suspect'``
    (excluded from every total by ``v_allocations``), never rows the user marked ``kept``.
* :func:`attach_manual_account` moves a manual account's history (transactions, imports, rules,
  statements) onto a linked account, then runs the full pass.

For the sync (ARCHITECTURE §8), inside the one apply transaction: call
``reconcile_item(session, item_id)`` after the inserts and before the cursor compare-and-set, and
in the sync that first sets ``history_complete_at`` (and ``plaid_history_start``) call
``apply_cutover(session, item)`` AFTER setting them: ``reconcile_item`` ran before the history was
complete, so its own cutover flagged nothing. Neither function commits.

Amex References embed a posting date (``ref[1:8]`` = YYYYDDD, RESEARCH T4.3b, inferred from one
real dataset): :func:`reference_posting_date` reads it, CSV import uses it for ``posted_date``, and
every pass reports how often it equals the posting date of the Plaid row a CSV row paired with.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from budgetapp.db import ledger_lock
from budgetapp.models import (
    Account,
    AppSettings,
    CsvImport,
    PlaidItem,
    Rule,
    Statement,
    Transaction,
    TransactionTombstone,
)
from budgetapp.services import userstate
from budgetapp.services.categorize import Categorizer, apply_carried_state

#: The CSV date may be this many days before Plaid's authorized date (or posting date).
WINDOW_BEFORE_AUTHORIZED = dt.timedelta(days=3)
#: Without an authorized date, the CSV date may be this far before Plaid's posting date.
WINDOW_BEFORE_POSTED = dt.timedelta(days=30)
#: A Reference's embedded posting date counts only within [Date, Date + 30 days].
REFERENCE_DATE_MAX_LAG = dt.timedelta(days=30)
#: A CSV row with a Reference posting date pairs only with a Plaid row posted on that date or at
#: most this long before it.
POSTED_DATE_TOLERANCE = dt.timedelta(days=3)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What a reconciliation pass did.

    ``pairs`` lists ``(deleted_csv_transaction_id, plaid_transaction_id)`` (internal row ids);
    ``suspects_flagged`` counts CSV rows newly marked ``suspect`` by the cutover.
    ``posted_dates_compared`` counts pairs whose CSV Reference held a plausible posting date, and
    ``posted_dates_matched`` how many of those equal the Plaid row's posting date.
    """

    paired: int = 0
    pairs: tuple[tuple[int, int], ...] = ()
    suspects_flagged: int = 0
    posted_dates_compared: int = 0
    posted_dates_matched: int = 0


@dataclass(frozen=True, slots=True)
class AttachResult:
    """What attaching a manual CSV account to a Plaid account did."""

    moved_transactions: int = 0
    dropped_duplicates: int = 0
    reconcile: ReconcileResult = ReconcileResult()
    moved_imports: int = 0
    moved_rules: int = 0
    moved_statements: int = 0
    merged_statements: int = 0


class AttachError(ValueError):
    """The accounts can't be attached; the message is written for the user."""


# --------------------------------------------------------------------------- Reference dates


def reference_posting_date(reference: str | None, csv_date: dt.date) -> dt.date | None:
    """The posting date embedded in an Amex Reference, when it is plausible; else ``None``.

    ``ref[1:8]`` is YYYYDDD (year and day of the year), e.g. ``320150170634561830`` → 2015-01-17
    (RESEARCH T4.1b, T4.3b). It counts only when it falls within ``[csv_date, csv_date + 30 d]``
    (Amex posts a charge on or after its transaction date).
    """
    if not reference or len(reference) < 8 or not reference[1:8].isdigit():
        return None
    year, day = int(reference[1:5]), int(reference[5:8])
    if not (dt.MINYEAR < year < dt.MAXYEAR) or not 1 <= day <= (
        366 if calendar.isleap(year) else 365
    ):
        return None
    posted = dt.date(year, 1, 1) + dt.timedelta(days=day - 1)
    if csv_date <= posted <= csv_date + REFERENCE_DATE_MAX_LAG:
        return posted
    return None


# --------------------------------------------------------------------------- matching


@dataclass(frozen=True, slots=True)
class CsvSide:
    """A CSV row as the matcher sees it. ``key`` orders and identifies it: ``(0, transaction id)``
    for a stored row, ``(1, n)`` for a row of a file being previewed. ``posted`` is the posting
    date read from its Reference (:func:`reference_posting_date`), when plausible."""

    key: tuple[int, int]
    account_id: int
    date: dt.date
    amount: Decimal
    texts: tuple[str, ...]
    posted: dt.date | None = None


@dataclass(frozen=True, slots=True)
class PlaidSide:
    """An unpaired Plaid row as the matcher sees it: posted, or ``pending`` (only ever matched by
    :func:`pending_twins`)."""

    id: int
    account_id: int
    date: dt.date
    authorized_date: dt.date | None
    amount: Decimal
    texts: tuple[str, ...]
    label: str = ""
    pending: bool = False

    @property
    def spend_date(self) -> dt.date:
        return self.authorized_date or self.date

    @property
    def window(self) -> tuple[dt.date, dt.date]:
        """The CSV dates this row can pair with (inclusive)."""
        if self.pending:  # a pending row's date is the day of the charge, like the CSV Date
            latest = max(self.date, self.spend_date)
            return (
                self.spend_date - WINDOW_BEFORE_AUTHORIZED,
                latest + WINDOW_BEFORE_AUTHORIZED,
            )
        if self.authorized_date is None:
            return self.date - WINDOW_BEFORE_POSTED, self.date
        return self.authorized_date - WINDOW_BEFORE_AUTHORIZED, self.date

    def accepts(self, csv_date: dt.date, csv_posted: dt.date | None = None) -> bool:
        """Can a CSV row dated ``csv_date`` (posted ``csv_posted`` by its Reference) be this
        charge? The date must be inside :attr:`window`; for a posted Plaid row the Reference
        posting date must also be its ``date`` or at most :data:`POSTED_DATE_TOLERANCE` after
        it (module docstring)."""
        low, high = self.window
        if not low <= csv_date <= high:
            return False
        if csv_posted is None or self.pending:
            return True
        return self.date <= csv_posted <= self.date + POSTED_DATE_TOLERANCE


_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_description(text: str | None) -> str:
    """Lower-case letters and digits only, accents removed, single spaces."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NON_ALNUM.sub(" ", plain.casefold()).split())


def _trigrams(text: str) -> frozenset[str]:
    grams: set[str] = set()
    for word in text.split():
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


def _gram_sets(texts: Iterable[str | None]) -> tuple[frozenset[str], ...]:
    return tuple(g for g in (_trigrams(normalize_description(t)) for t in texts if t) if g)


def description_similarity(a: Iterable[str | None], b: Iterable[str | None]) -> float:
    """Best trigram Dice similarity (0–1) between any text of ``a`` and any text of ``b``."""
    return _best_dice(_gram_sets(a), _gram_sets(b))


def _best_dice(a: Sequence[frozenset[str]], b: Sequence[frozenset[str]]) -> float:
    best = 0.0
    for x in a:
        for y in b:
            score = 2 * len(x & y) / (len(x) + len(y))
            if score > best:
                best = score
    return best


def match(
    csv_rows: Iterable[CsvSide], plaid_rows: Iterable[PlaidSide]
) -> list[tuple[CsvSide, PlaidSide]]:
    """One-to-one pairs, assigned globally best first (see the module docstring).

    A candidate pair has equal amounts and a CSV row the Plaid row accepts
    (:meth:`PlaidSide.accepts`). Pairs are taken in order of date distance, then description
    similarity (higher first), then same account first; ties fall back to the dates and keys, so
    the result is deterministic.
    """
    by_amount: dict[Decimal, list[PlaidSide]] = defaultdict(list)
    for plaid in plaid_rows:
        by_amount[plaid.amount].append(plaid)
    plaid_grams: dict[int, tuple[frozenset[str], ...]] = {}
    candidates: list[tuple[tuple[Any, ...], CsvSide, PlaidSide]] = []
    for row in csv_rows:
        options = by_amount.get(row.amount)
        if not options:
            continue
        row_grams: tuple[frozenset[str], ...] | None = None
        for plaid in options:
            if not plaid.accepts(row.date, row.posted):
                continue
            if row_grams is None:
                row_grams = _gram_sets(row.texts)
            if plaid.id not in plaid_grams:
                plaid_grams[plaid.id] = _gram_sets(plaid.texts)
            similarity = _best_dice(row_grams, plaid_grams[plaid.id])
            distance = abs((row.date - plaid.spend_date).days)
            order = (
                distance,
                -similarity,
                row.account_id != plaid.account_id,
                row.date,
                plaid.date,
                row.key,
                plaid.id,
            )
            candidates.append((order, row, plaid))
    candidates.sort(key=lambda candidate: candidate[0])
    used_csv: set[tuple[int, int]] = set()
    used_plaid: set[int] = set()
    pairs: list[tuple[CsvSide, PlaidSide]] = []
    for _, row, plaid in candidates:
        if row.key in used_csv or plaid.id in used_plaid:
            continue
        used_csv.add(row.key)
        used_plaid.add(plaid.id)
        pairs.append((row, plaid))
    return pairs


# --------------------------------------------------------------------------- loading candidates


def item_account_ids(session: Session, item_id: int) -> list[int]:
    return list(
        session.scalars(select(Account.id).where(Account.item_id == item_id).order_by(Account.id))
    )


def _plaid_sides(session: Session, account_ids: Sequence[int], *, pending: bool) -> list[PlaidSide]:
    if not account_ids:
        return []
    rows = session.execute(
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.date,
            Transaction.authorized_date,
            Transaction.amount,
            Transaction.original_description,
            Transaction.name,
            Transaction.merchant_name,
        ).where(
            Transaction.account_id.in_(account_ids),
            Transaction.source == "plaid",
            Transaction.csv_reference.is_(None),
            Transaction.pending.is_(pending),
        )
    )
    return [
        PlaidSide(
            id=r.id,
            account_id=r.account_id,
            date=r.date,
            authorized_date=r.authorized_date,
            amount=r.amount,
            texts=(r.original_description or "", r.name or "", r.merchant_name or ""),
            label=r.merchant_name or r.name or "",
            pending=pending,
        )
        for r in rows
    ]


def plaid_candidates(session: Session, account_ids: Sequence[int]) -> list[PlaidSide]:
    """Unpaired, posted Plaid rows of these accounts."""
    return _plaid_sides(session, account_ids, pending=False)


def pending_candidates(session: Session, account_ids: Sequence[int]) -> list[PlaidSide]:
    """Pending Plaid rows of these accounts (never paired; see :func:`pending_twins`)."""
    return _plaid_sides(session, account_ids, pending=True)


def earliest_window_start(plaid_rows: Iterable[PlaidSide]) -> dt.date | None:
    starts = [p.window[0] for p in plaid_rows]
    return min(starts) if starts else None


def csv_candidates(
    session: Session, account_ids: Sequence[int], *, since: dt.date | None = None
) -> list[CsvSide]:
    """Every CSV row of these accounts (a paired CSV row no longer exists), optionally dated on
    or after ``since``."""
    if not account_ids:
        return []
    query = select(
        Transaction.id,
        Transaction.account_id,
        Transaction.date,
        Transaction.amount,
        Transaction.original_description,
        Transaction.name,
        Transaction.csv_reference,
    ).where(Transaction.account_id.in_(account_ids), Transaction.source == "csv")
    if since is not None:
        query = query.where(Transaction.date >= since)
    return [
        CsvSide(
            key=(0, r.id),
            account_id=r.account_id,
            date=r.date,
            amount=r.amount,
            texts=(r.original_description or "", r.name or ""),
            posted=reference_posting_date(r.csv_reference, r.date),
        )
        for r in session.execute(query)
    ]


def pending_twins(session: Session, account_ids: Sequence[int]) -> dict[int, PlaidSide]:
    """``{CSV transaction id: pending Plaid row}`` for the stored CSV rows of these accounts whose
    charge Plaid still shows as pending.

    One to one, best first, like :func:`match`; a pending row's window is its charge date ± 3 days.
    Nothing is paired: the posted row Plaid sends later pairs with the CSV row in the next pass.
    The Import page uses this to say so, instead of "no matching Plaid charge".
    """
    pending = pending_candidates(session, account_ids)
    if not pending:
        return {}
    since = earliest_window_start(pending)
    rows = csv_candidates(session, account_ids, since=since)
    return {row.key[1]: plaid for row, plaid in match(rows, pending)}


# --------------------------------------------------------------------------- the full pass


def reconcile_item(session: Session, item_id: int) -> ReconcileResult:
    """Full CSV ↔ Plaid reconciliation pass for one Item, then the cutover (module docstring).

    Takes the ledger lock (re-entrant). Never commits; the caller commits.
    """
    ledger_lock(session)
    item = session.get(PlaidItem, item_id)
    if item is None:
        return ReconcileResult()
    account_ids = item_account_ids(session, item_id)
    plaid_rows = plaid_candidates(session, account_ids)
    pairs: list[tuple[CsvSide, PlaidSide]] = []
    if plaid_rows:
        since = earliest_window_start(plaid_rows)
        pairs = match(csv_candidates(session, account_ids, since=since), plaid_rows)
    compared, matched = _apply_pairs(session, [(c.key[1], p.id) for c, p in pairs])
    flagged = apply_cutover(session, item)
    return ReconcileResult(
        paired=len(pairs),
        pairs=tuple((c.key[1], p.id) for c, p in pairs),
        suspects_flagged=flagged,
        posted_dates_compared=compared,
        posted_dates_matched=matched,
    )


def _apply_pairs(session: Session, pairs: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """Merge each CSV row into its Plaid row. Returns the Reference posting-date check counts."""
    if not pairs:
        return 0, 0
    ids = [i for pair in pairs for i in pair]
    rows = {
        txn.id: txn
        for txn in session.scalars(
            select(Transaction)
            .where(Transaction.id.in_(ids))
            .options(selectinload(Transaction.splits))
        )
    }
    # 1. capture everything the CSV rows carry (deleting a row also deletes its splits)
    carried = []
    for csv_id, plaid_id in pairs:
        csv_row = rows[csv_id]
        carried.append(
            (
                rows[plaid_id],
                userstate.capture(csv_row),
                csv_row.csv_reference,
                csv_row.csv_import_id,
                reference_posting_date(csv_row.csv_reference, csv_row.date),
            )
        )
    # 2. delete the CSV rows and flush, so their References are free
    for csv_id, _ in pairs:
        session.delete(rows[csv_id])
    session.flush()
    # 3. copy the References onto the Plaid rows, then move the user state
    categorizer: Categorizer | None = None
    compared = matched = 0
    for plaid_row, state, reference, import_id, derived in carried:
        plaid_row.csv_reference = reference
        plaid_row.csv_import_id = import_id
        if state is not None:
            if categorizer is None:
                categorizer = Categorizer.load(session)
            apply_carried_state(categorizer, plaid_row, state)
        if derived is not None:
            compared += 1
            matched += derived == (plaid_row.posted_date or plaid_row.date)
    session.flush()
    return compared, matched


def history_start(session: Session, account_id: int) -> dt.date | None:
    """The earliest posting date of the account's posted Plaid rows (``None`` without any)."""
    return session.scalar(
        select(func.min(func.coalesce(Transaction.posted_date, Transaction.date))).where(
            Transaction.account_id == account_id,
            Transaction.source == "plaid",
            Transaction.pending.is_(False),
        )
    )


def apply_cutover(session: Session, item: PlaidItem) -> int:
    """Flag unpaired CSV rows inside Plaid's history as ``suspect``; returns how many.

    Only once the Item has ``history_complete_at``. An account's ``plaid_history_start`` is set
    here (its earliest Plaid posting date) when the sync has not set it yet; it is never moved
    afterwards. Rows the user marked ``kept`` are never flagged again. :func:`reconcile_item`
    ends with it; the sync that first sets ``history_complete_at`` calls it again AFTER setting
    it, in the same transaction (module docstring). Idempotent; takes the ledger lock.
    """
    ledger_lock(session)
    if item.history_complete_at is None:
        return 0
    flagged = 0
    accounts = session.scalars(select(Account).where(Account.item_id == item.id)).all()
    for account in accounts:
        start = account.plaid_history_start
        if start is None:
            start = history_start(session, account.id)
            if start is None:
                continue
            account.plaid_history_start = start
        result = session.execute(
            update(Transaction)
            .where(
                Transaction.account_id == account.id,
                Transaction.source == "csv",
                Transaction.duplicate_status == "none",
                Transaction.date >= start,
            )
            .values(duplicate_status="suspect")
            .execution_options(synchronize_session="fetch")
        )
        flagged += result.rowcount or 0
    if flagged:
        session.flush()
    return flagged


# --------------------------------------------------------------------------- attaching


def attach_manual_account(session: Session, manual_id: int, plaid_account_id: int) -> AttachResult:
    """Move a manual CSV account's history onto a linked account, then run the full pass.

    Takes the ledger lock (re-entrant). In order:

    * drops manual rows whose Reference already exists elsewhere (the Reference is globally
      unique, so this only guards against a broken database);
    * moves the rest: transactions, CSV imports, rules and statements. A statement that collides
      with one of the linked account's rows on ``(account_id, closing_date)`` or
      ``(account_id, plaid_issue_date)`` is merged into it — user-entered closing dates and
      reported values win over Liabilities snapshots, both notes are kept — never inserted twice;
    * re-points ``app_settings.budget_cycle_account_id`` when it names the manual account (its
      ``ON DELETE SET NULL`` would otherwise turn statement-cycle budgets into calendar months);
    * copies ``statement_closing_day`` and ``notice`` when the linked account has none;
    * deletes the manual account and runs :func:`reconcile_item` for the linked account's Item.

    Raises :class:`AttachError` when the ids don't name a manual and a linked account.
    """
    ledger_lock(session)
    session.flush()
    manual = session.get(Account, manual_id)
    target = session.get(Account, plaid_account_id)
    if manual is None or not manual.is_manual:
        raise AttachError("That CSV account no longer exists. Reload the page.")
    if target is None or target.is_manual or target.item_id is None:
        raise AttachError("Choose a card linked through Plaid to attach the CSV history to.")

    others = select(Transaction.csv_reference).where(
        Transaction.account_id != manual.id, Transaction.csv_reference.is_not(None)
    )
    duplicates = list(
        session.scalars(
            select(Transaction).where(
                Transaction.account_id == manual.id, Transaction.csv_reference.in_(others)
            )
        )
    )
    for txn in duplicates:
        session.delete(txn)
    session.flush()

    moved = _move(session, Transaction, manual.id, target.id)
    moved_imports = _move(session, CsvImport, manual.id, target.id)
    moved_rules = _move(session, Rule, manual.id, target.id)
    _move(session, TransactionTombstone, manual.id, target.id)
    moved_statements, merged_statements = _move_statements(session, manual, target)

    app = session.get(AppSettings, 1)
    if app is not None and app.budget_cycle_account_id == manual.id:
        app.budget_cycle_account_id = target.id
    if target.statement_closing_day is None and manual.statement_closing_day is not None:
        target.statement_closing_day = manual.statement_closing_day
    if not target.notice and manual.notice:
        target.notice = manual.notice
    session.flush()
    session.delete(manual)
    session.flush()

    result = reconcile_item(session, target.item_id)
    return AttachResult(
        moved_transactions=moved,
        dropped_duplicates=len(duplicates),
        reconcile=result,
        moved_imports=moved_imports,
        moved_rules=moved_rules,
        moved_statements=moved_statements,
        merged_statements=merged_statements,
    )


def _move(session: Session, model: Any, old_account_id: int, new_account_id: int) -> int:
    result = session.execute(
        update(model)
        .where(model.account_id == old_account_id)
        .values(account_id=new_account_id)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


_REPORTED = (
    "reported_previous_balance",
    "reported_payments_credits",
    "reported_new_charges",
    "reported_fees",
    "reported_interest",
)


def _move_statements(session: Session, manual: Account, target: Account) -> tuple[int, int]:
    """Move the manual account's statements, merging collisions. Returns (moved, merged)."""
    moved = merged = 0
    incoming = session.scalars(
        select(Statement).where(Statement.account_id == manual.id).order_by(Statement.closing_date)
    ).all()
    for statement in incoming:
        existing = session.scalars(select(Statement).where(Statement.account_id == target.id)).all()
        by_closing = next((s for s in existing if s.closing_date == statement.closing_date), None)
        by_issue = None
        if statement.plaid_issue_date is not None:
            by_issue = next(
                (s for s in existing if s.plaid_issue_date == statement.plaid_issue_date), None
            )
        into = by_closing or by_issue
        if into is None:
            statement.account_id = target.id
            session.flush()
            moved += 1
            continue
        _merge_statement(statement, into, existing)
        session.delete(statement)
        session.flush()
        merged += 1
    return moved, merged


def _merge_statement(source: Statement, into: Statement, existing: Sequence[Statement]) -> None:
    """Merge ``source`` (a manual account's statement) into ``into`` (the linked account's)."""
    others = [s for s in existing if s is not into]
    if (
        source.closing_date_source == "user"
        and into.closing_date_source != "user"
        and not any(s.closing_date == source.closing_date for s in others)
    ):
        into.closing_date = source.closing_date
        into.closing_date_source = "user"
    if (
        into.plaid_issue_date is None
        and source.plaid_issue_date is not None
        and not any(s.plaid_issue_date == source.plaid_issue_date for s in others)
    ):
        into.plaid_issue_date = source.plaid_issue_date
    if (
        into.period_start_override is None
        and source.period_start_override is not None
        and source.period_start_override <= into.closing_date
    ):
        into.period_start_override = source.period_start_override
    if source.reported_new_balance is not None and (
        into.reported_new_balance is None
        or (
            source.reported_new_balance_source == "user"
            and into.reported_new_balance_source != "user"
        )
    ):
        into.reported_new_balance = source.reported_new_balance
        into.reported_new_balance_source = source.reported_new_balance_source or "user"
    for name in _REPORTED:
        if getattr(into, name) is None and getattr(source, name) is not None:
            setattr(into, name, getattr(source, name))
    if into.checked_at is None and source.checked_at is not None:
        into.checked_at = source.checked_at
    notes = [n.strip() for n in (into.notes, source.notes) if n and n.strip()]
    if len(notes) == 2 and notes[0] != notes[1]:
        into.notes = f"{notes[0]}\n\n{notes[1]}"
    elif notes:
        into.notes = notes[0]
