"""Statement cycles and reconciliation with the Amex statement (INS-4, REL-3, Gate D).

ARCHITECTURE §5 (``statements``) and §6 ("Statement cycles and reconciliation") are the design.
This module is the ONE definition of a statement's totals (principle 7): the Statements page and
anything else that shows a statement total call :func:`summary` or :func:`overview`.

* **Cycles.** A cycle is ``(previous closing date, closing date]`` by POSTING date
  (``COALESCE(posted_date, date)`` of posted rows). Pending rows and suspected duplicates never
  count. Closing dates are exactly the ``statements`` rows (the same dates budgets use, see
  :func:`cycle_inputs`); :func:`periods.statement_cycles` generates dates only before the first
  and after the last stored one (from the account's ``statement_closing_day``) and flags cycles
  outside 25–35 days. A period's start is always the previous closing date + 1, except the
  earliest statement's ``period_start_override``.
* **Editing dates.** Closing dates are kept between 2000 and 45 days from today, and a period
  start within two months before its closing date. Moving a user-entered statement onto the
  date of the Amex (Liabilities) statement next to it, within 10 days, combines the two (the
  values typed win). A statement the user added can be removed. The open period's estimated
  date is never saved: its statement doesn't exist yet.
* **Status.** ``open`` until the closing date has passed, ``settling`` for
  ``app_settings.statement_settle_days`` after it, then ``final``.
* **Buckets, by sign first.** Negative: ``payments`` (Card Payments category or
  ``transaction_code = 'payment'``) or ``credits`` (refunds, statement credits, rewards).
  Positive: ``interest`` (code ``interest``, PFC ``BANK_FEES_INTEREST_CHARGE``, or a description
  starting "INTEREST CHARGE" on a row Plaid did not classify, such as a CSV row), ``fees`` (fee
  codes, PFC ``BANK_FEES``, positive CSV ``Fees & Adjustments``) or ``new_charges``.
* **Amex's identity:** ``New Balance = Previous Balance − Payments/Credits + New Charges + Fees +
  Interest Charged``. Reported values are typed from the PDF statement (the New Balance may also
  come from the Liabilities snapshot). ``reported_payments_credits`` is stored as a POSITIVE
  amount (the statement prints it with a minus).
* **Gate D:** net activity must equal ``new balance − previous new balance`` to the cent. The
  previous new balance is the previous statement's reported New Balance, else this statement's
  reported Previous Balance.

Services never commit; the routes do.
"""

from __future__ import annotations

import datetime as dt
import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.models import Account, Category, Statement, Transaction, get_app_settings
from budgetapp.money import ZERO, parse_money_input, to_money

# --------------------------------------------------------------------------- constants

OPEN = "open"
SETTLING = "settling"
FINAL = "final"
STATUS_LABELS = {OPEN: "Open", SETTLING: "Settling", FINAL: "Final"}

PAYMENTS = "payments"
CREDITS = "credits"
NEW_CHARGES = "new_charges"
FEES = "fees"
INTEREST = "interest"
#: Display order of the buckets (the order of the Amex detail sections).
BUCKETS = (PAYMENTS, CREDITS, NEW_CHARGES, FEES, INTEREST)
BUCKET_LABELS = {
    PAYMENTS: "Payments",
    CREDITS: "Credits",
    NEW_CHARGES: "New Charges",
    FEES: "Fees",
    INTEREST: "Interest Charged",
}

#: Plaid ``transaction_code`` values that are fees on the card (RESEARCH T1.5).
FEE_CODES = frozenset({"late fee", "membership fee", "returned item fee", "bank charge"})
INTEREST_CODE = "interest"
PAYMENT_CODE = "payment"
#: PFC v2 interest (BANK_FEES_CASH_ADVANCE mixes fees and interest, so it stays a fee).
INTEREST_PFC_DETAILED = frozenset({"BANK_FEES_INTEREST_CHARGE"})
FEES_PFC_PRIMARY = "BANK_FEES"
#: PostgreSQL case-insensitive regexes.
INTEREST_TEXT_RE = r"^\s*interest charge"
AMEX_FEES_CATEGORY_RE = r"^\s*fees\s*&\s*adjustments"

#: Days either side of a closing date that :func:`near_boundary` lists.
BOUNDARY_DAYS = 3
#: Two closing dates this close are the same statement (one is a mistake).
SAME_STATEMENT_DAYS = periods.DROP_WITHIN_DAYS
MAX_NOTES_LENGTH = 2000
#: Plausible closing dates: none before this, none more than MAX_FUTURE_DAYS from today.
EARLIEST_CLOSING_DATE = dt.date(2000, 1, 1)
MAX_FUTURE_DAYS = 45
#: The earliest statement's period start lies at most this many days before its closing date.
MAX_FIRST_PERIOD_DAYS = 62
#: :func:`cycles` never looks outside these dates (stored dates are validated well inside).
_RANGE_FLOOR = dt.date(1900, 1, 1)
_RANGE_CEILING = dt.date(2200, 12, 31)

CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_UNAVAILABLE = "unavailable"
CHECK_OPEN = "open"

#: Form field → label, in the Amex summary-box order.
REPORTED_FIELDS = (
    ("previous_balance", "Previous Balance"),
    ("payments_credits", "Payments/Credits"),
    ("new_charges", "New Charges"),
    ("fees", "Fees"),
    ("interest", "Interest Charged"),
    ("new_balance", "New Balance"),
)
_CR_SUFFIX = re.compile(r"\s*cr\.?\s*$", re.IGNORECASE)


class StatementError(ValueError):
    """Invalid statement input; ``errors`` maps a form field name to a message for the user."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True, slots=True)
class AccountOption:
    """A card that has statements (the page's account selector)."""

    id: int
    name: str
    is_manual: bool
    is_hidden: bool
    closing_day: int | None


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """What ``ui.category_badge`` reads."""

    id: int
    name: str
    color: str | None
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class Reported:
    """Values typed from the statement (or the Liabilities snapshot's New Balance).

    ``payments_credits`` is positive: the amount the statement subtracts.
    """

    previous_balance: Decimal | None = None
    payments_credits: Decimal | None = None
    new_charges: Decimal | None = None
    fees: Decimal | None = None
    interest: Decimal | None = None
    new_balance: Decimal | None = None
    new_balance_source: str | None = None

    @property
    def has_values(self) -> bool:
        return any(getattr(self, name) is not None for name, _ in REPORTED_FIELDS)

    @property
    def complete(self) -> bool:
        return all(getattr(self, name) is not None for name, _ in REPORTED_FIELDS)

    @property
    def identity_difference(self) -> Decimal | None:
        """New Balance minus what the other five fields add up to (non-zero = a typo)."""
        if not self.complete:
            return None
        computed = (
            self.previous_balance  # type: ignore[operator]
            - self.payments_credits
            + self.new_charges
            + self.fees
            + self.interest
        )
        return to_money(self.new_balance - computed)  # type: ignore[operator]


@dataclass(frozen=True, slots=True)
class StatementCycle:
    """One statement period of one account (stored or generated)."""

    account_id: int
    start: dt.date
    closing_date: dt.date
    #: The closing date was generated from the closing day (no ``statements`` row has it).
    generated: bool
    #: The cycle is outside 25–35 days (usually a missing statement).
    flagged: bool
    status: str
    #: The first day the statement counts as final.
    final_on: dt.date
    statement_id: int | None = None
    closing_date_source: str | None = None
    plaid_issue_date: dt.date | None = None
    period_start_override: dt.date | None = None
    #: This cycle's statement is the earliest stored one.
    is_earliest_statement: bool = False
    #: Its period start may be set: it is (or, once saved, would be) the earliest statement.
    can_set_period_start: bool = False
    #: The user added this statement (no Amex snapshot), so it can be removed.
    removable: bool = False
    reported: Reported = field(default_factory=Reported)
    previous_statement_id: int | None = None
    #: The previous statement's reported New Balance (the app's Previous Balance).
    previous_new_balance: Decimal | None = None
    checked_at: dt.datetime | None = None
    notes: str | None = None

    @property
    def days(self) -> int:
        return (self.closing_date - self.start).days + 1

    @property
    def previous_closing(self) -> dt.date:
        return self.start - dt.timedelta(days=1)

    @property
    def label(self) -> str:
        return periods.Period(self.start, self.closing_date, periods.STATEMENT).label

    @property
    def is_stored(self) -> bool:
        return self.statement_id is not None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def key(self) -> str:
        """The cycle's id in URLs: its closing date."""
        return self.closing_date.isoformat()

    @property
    def is_estimate_open(self) -> bool:
        """The open period whose closing date is only an estimate: nothing can be saved on it."""
        return self.status == OPEN and self.generated


@dataclass(frozen=True, slots=True)
class CycleInputs:
    """What a card's statement cycles are built from, for anything else that needs the same
    cycles (statement-mode budget periods): pass these to ``periods`` as they are."""

    account_id: int
    closings: tuple[dt.date, ...]
    closing_day: int | None
    first_period_start: dt.date | None

    @property
    def known(self) -> bool:
        """False when no cycle can be computed (no stored date and no closing day)."""
        return bool(self.closings) or self.closing_day is not None

    def period_for(self, day: dt.date) -> periods.Period:
        """The statement cycle containing ``day`` (the calendar month when none is known)."""
        return periods.budget_period_for(
            day,
            mode="statement",
            closings=self.closings,
            closing_day=self.closing_day,
            first_period_start=self.first_period_start,
        )


@dataclass(frozen=True, slots=True)
class FieldCheck:
    """One line of the summary box: the app's value against the statement's.

    Values are signed the way the statement prints them: Payments/Credits negative, the other
    middle lines positive.
    """

    key: str
    label: str
    operator: str
    app: Decimal | None
    reported: Decimal | None

    @property
    def difference(self) -> Decimal | None:
        """``app − reported`` when both are known."""
        if self.app is None or self.reported is None:
            return None
        return to_money(self.app - self.reported)

    @property
    def matches(self) -> bool | None:
        diff = self.difference
        return None if diff is None else diff == ZERO


@dataclass(frozen=True, slots=True)
class GateDCheck:
    """The automatic check: net activity == New Balance − previous New Balance, to the cent."""

    state: str
    net_activity: Decimal
    previous_balance: Decimal | None = None
    #: ``previous_statement`` (its reported New Balance) or ``this_statement`` (its Previous
    #: Balance), or None when neither is known.
    previous_source: str | None = None
    new_balance: Decimal | None = None
    #: What the statement says the activity was: ``new_balance − previous_balance``.
    expected_activity: Decimal | None = None
    #: ``net_activity − expected_activity`` (negative: the app has less than the statement).
    difference: Decimal | None = None
    #: Why the check can't run: ``new_balance`` or ``previous_balance``.
    missing: str | None = None
    #: The statement is still settling: transactions can still arrive.
    provisional: bool = False

    @property
    def passed(self) -> bool:
        return self.state == CHECK_PASS

    @property
    def failed(self) -> bool:
        return self.state == CHECK_FAIL


@dataclass(frozen=True, slots=True)
class StatementSummary:
    """The app's totals for a cycle, compared with the statement (ARCHITECTURE §6)."""

    cycle: StatementCycle
    payments: Decimal
    credits: Decimal
    new_charges: Decimal
    fees: Decimal
    interest: Decimal
    net_activity: Decimal
    counts: tuple[tuple[str, int], ...]
    #: The app's Previous Balance: the previous statement's reported New Balance.
    previous_balance: Decimal | None
    #: The app's New Balance: the previous balance (or the statement's own) + net activity.
    new_balance: Decimal | None
    #: Which balance ``new_balance`` starts from (see :class:`GateDCheck.previous_source`).
    balance_basis: str | None
    fields: tuple[FieldCheck, ...]
    check: GateDCheck

    @property
    def payments_credits(self) -> Decimal:
        """Payments plus credits (negative), as the statement's Payments/Credits line."""
        return to_money(self.payments + self.credits)

    @property
    def transaction_count(self) -> int:
        return sum(n for _, n in self.counts)

    def count(self, bucket: str) -> int:
        return dict(self.counts).get(bucket, 0)

    def total(self, bucket: str) -> Decimal:
        return getattr(self, bucket)


@dataclass(frozen=True, slots=True)
class StatementLine:
    """A posted (or, near a boundary, pending) transaction as the statement view shows it."""

    transaction_id: int
    posted_date: dt.date | None
    #: The transaction date shown on statements: ``COALESCE(authorized_date, date)``.
    date: dt.date
    name: str
    merchant_name: str | None
    amount: Decimal
    bucket: str
    category: CategoryRef
    source: str
    pending: bool = False
    #: For boundary lists: ``previous``, ``this`` or ``next`` statement, or ``pending``.
    side: str | None = None

    @property
    def bucket_label(self) -> str:
        return BUCKET_LABELS[self.bucket]


@dataclass(frozen=True, slots=True)
class Boundary:
    """Transactions posted within a few days either side of one closing date."""

    closing_date: dt.date
    #: ``start`` (the previous closing date) or ``end`` (this cycle's closing date).
    edge: str
    days: int
    lines: tuple[StatementLine, ...]


@dataclass(frozen=True, slots=True)
class BucketGroup:
    bucket: str
    label: str
    total: Decimal
    lines: tuple[StatementLine, ...]


@dataclass(frozen=True, slots=True)
class PendingInfo:
    """Pending rows dated inside the cycle: not counted until they post."""

    count: int
    total: Decimal


@dataclass(frozen=True, slots=True)
class StatementDetail:
    """Everything the cycle page shows."""

    account: AccountOption
    summary: StatementSummary
    groups: tuple[BucketGroup, ...]
    boundaries: tuple[Boundary, ...]
    pending: PendingInfo
    #: Neighbouring cycles for navigation (``None`` at either end).
    earlier: StatementCycle | None
    later: StatementCycle | None

    @property
    def cycle(self) -> StatementCycle:
        return self.summary.cycle


# --------------------------------------------------------------------------- accounts


def accounts(session: Session) -> list[AccountOption]:
    """Every card, linked first, then manual (CSV) ones; hidden cards last."""
    rows = sorted(
        session.scalars(select(Account)), key=lambda a: (a.is_hidden, a.item_id is None, a.id)
    )
    return [_option(a) for a in rows]


def _option(account: Account) -> AccountOption:
    return AccountOption(
        id=account.id,
        name=account.display_name,
        is_manual=account.is_manual,
        is_hidden=account.is_hidden,
        closing_day=account.statement_closing_day,
    )


def default_account(session: Session, options: Sequence[AccountOption]) -> AccountOption | None:
    """The budget-cycle card if set, else the first visible card, else the first card."""
    if not options:
        return None
    preferred = get_app_settings(session).budget_cycle_account_id
    for option in options:
        if option.id == preferred:
            return option
    visible = [o for o in options if not o.is_hidden]
    return (visible or list(options))[0]


def _account(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"no account {account_id}")
    return account


# --------------------------------------------------------------------------- stored statements


def _reported(row: Statement) -> Reported:
    new_balance = row.reported_new_balance
    return Reported(
        previous_balance=row.reported_previous_balance,
        payments_credits=row.reported_payments_credits,
        new_charges=row.reported_new_charges,
        fees=row.reported_fees,
        interest=row.reported_interest,
        new_balance=new_balance,
        new_balance_source=row.reported_new_balance_source if new_balance is not None else None,
    )


def _stored(session: Session, account_id: int) -> list[Statement]:
    """The account's statements, oldest first. Every row is a statement (no row is hidden)."""
    return list(
        session.scalars(
            select(Statement)
            .where(Statement.account_id == account_id)
            .order_by(Statement.closing_date)
        )
    )


def _removable(row: Statement) -> bool:
    """A statement the user added (Amex's own statements, with a Plaid issue date, stay)."""
    return row.closing_date_source == "user" and row.plaid_issue_date is None


def cycle_inputs(session: Session, account_id: int) -> CycleInputs:
    """The closing dates, closing day and earliest period start the card's cycles come from.

    Statement-mode budget periods must use these (``CycleInputs.period_for(day)``, or the
    fields passed to ``periods.budget_period_for``) so they are the cycles this page shows.
    Raises ``LookupError`` for an unknown account.
    """
    account = _account(session, account_id)
    stored = _stored(session, account_id)
    return CycleInputs(
        account_id=account_id,
        closings=tuple(r.closing_date for r in stored),
        closing_day=account.statement_closing_day,
        first_period_start=stored[0].period_start_override if stored else None,
    )


# --------------------------------------------------------------------------- cycles


def status_for(closing_date: dt.date, today: dt.date, settle_days: int) -> str:
    """``open`` through the closing date, ``settling`` for ``settle_days`` after, then ``final``."""
    if today <= closing_date:
        return OPEN
    if (today - closing_date).days <= settle_days:
        return SETTLING
    return FINAL


def _posting_date():
    return func.coalesce(Transaction.posted_date, Transaction.date)


def _counted(account_id: int) -> tuple[Any, ...]:
    """Rows that count on a statement: this account's posted rows, suspects excluded."""
    return (
        Transaction.account_id == account_id,
        Transaction.pending.is_(False),
        Transaction.duplicate_status != "suspect",
    )


def _default_range(
    session: Session, account_id: int, stored: Sequence[Statement], today: dt.date
) -> tuple[dt.date, dt.date]:
    """From the earliest posted transaction or stored statement through today."""
    earliest = session.scalar(select(func.min(_posting_date())).where(*_counted(account_id)))
    candidates = [d for d in (earliest, stored[0].closing_date if stored else None) if d]
    start = min(candidates) if candidates else today
    return min(start, today), today


def cycles(
    session: Session,
    account_id: int,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    today: dt.date | None = None,
) -> list[StatementCycle]:
    """The account's statement cycles overlapping ``[start, end]``, newest first.

    By default from the earliest posted transaction (or stored statement) through the open cycle
    that contains today. Empty when the account has neither a stored closing date nor a closing
    day. Raises ``LookupError`` for an unknown account.
    """
    account = _account(session, account_id)
    today = today or clock.today()
    settle_days = get_app_settings(session).statement_settle_days
    stored = _stored(session, account_id)
    if start is None or end is None:
        default_start, default_end = _default_range(session, account_id, stored, today)
        start = start or default_start
        end = end or default_end
    if end < start:
        start, end = end, start
    if end < _RANGE_FLOOR or start > _RANGE_CEILING:
        return []
    start, end = max(start, _RANGE_FLOOR), min(end, _RANGE_CEILING)
    first_override = stored[0].period_start_override if stored else None
    found = periods.statement_cycles(
        [r.closing_date for r in stored],
        account.statement_closing_day,
        start,
        end,
        first_period_start=first_override,
    )
    by_closing = {r.closing_date: r for r in stored}
    earliest = stored[0] if stored else None
    out = []
    for period in found:
        row = by_closing.get(period.end)
        previous = by_closing.get(period.start - dt.timedelta(days=1))
        is_earliest = row is not None and row is earliest
        out.append(
            StatementCycle(
                account_id=account_id,
                start=period.start,
                closing_date=period.end,
                generated=row is None,
                flagged=period.flagged,
                status=status_for(period.end, today, settle_days),
                final_on=period.end + dt.timedelta(days=settle_days + 1),
                statement_id=row.id if row else None,
                closing_date_source=row.closing_date_source if row else None,
                plaid_issue_date=row.plaid_issue_date if row else None,
                period_start_override=row.period_start_override if is_earliest else None,
                is_earliest_statement=is_earliest,
                can_set_period_start=is_earliest
                or (row is None and (earliest is None or period.end < earliest.closing_date)),
                removable=row is not None and _removable(row),
                reported=_reported(row) if row else Reported(),
                previous_statement_id=previous.id if previous else None,
                previous_new_balance=previous.reported_new_balance if previous else None,
                checked_at=row.checked_at if row else None,
                notes=row.notes if row else None,
            )
        )
    out.reverse()
    return out


def _plausible(day: dt.date, today: dt.date) -> bool:
    """A date a statement can close on: from 2000 to the open period's closing date at most
    (generated: within a month; added by the user: within :data:`MAX_FUTURE_DAYS`)."""
    return EARLIEST_CLOSING_DATE <= day <= today + dt.timedelta(days=MAX_FUTURE_DAYS)


def find_cycle(
    session: Session, account_id: int, closing_date: dt.date, *, today: dt.date | None = None
) -> StatementCycle | None:
    """The cycle that closes on ``closing_date`` (never one after the open cycle), else None."""
    today = today or clock.today()
    if not _plausible(closing_date, today):
        return None
    found = cycles(session, account_id, start=closing_date, end=closing_date, today=today)
    cycle = next((c for c in found if c.closing_date == closing_date), None)
    if cycle is None:
        return None
    current = cycles(session, account_id, start=today, end=today, today=today)
    if current and closing_date > current[0].closing_date:
        return None
    return cycle


# --------------------------------------------------------------------------- buckets & activity


def _card_payments_id(session: Session) -> int | None:
    return session.scalar(select(Category.id).where(Category.system_key == "card_payments"))


def bucket_expression(card_payments_id: int | None):
    """The SQL ``CASE`` that puts a transaction in a statement bucket (module docstring)."""
    t = Transaction
    code = func.lower(func.btrim(func.coalesce(t.transaction_code, "")))
    is_payment = code == PAYMENT_CODE
    if card_payments_id is not None:
        is_payment = or_(is_payment, t.category_id == card_payments_id)
    unclassified = and_(
        t.transaction_code.is_(None), t.pfc_primary.is_(None), t.pfc_detailed.is_(None)
    )
    interest_text = or_(
        t.name.op("~*")(INTEREST_TEXT_RE),
        func.coalesce(t.original_description, "").op("~*")(INTEREST_TEXT_RE),
    )
    return case(
        (and_(t.amount < 0, is_payment), PAYMENTS),
        (t.amount < 0, CREDITS),
        (
            or_(
                code == INTEREST_CODE,
                t.pfc_detailed.in_(INTEREST_PFC_DETAILED),
                and_(unclassified, interest_text),
            ),
            INTEREST,
        ),
        (
            or_(
                code.in_(FEE_CODES),
                t.pfc_primary == FEES_PFC_PRIMARY,
                and_(
                    t.source == "csv",
                    func.coalesce(t.external_category, "").op("~*")(AMEX_FEES_CATEGORY_RE),
                ),
            ),
            FEES,
        ),
        else_=NEW_CHARGES,
    )


@dataclass(slots=True)
class _Activity:
    totals: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(BUCKETS, ZERO))
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))


def _activity(
    session: Session, account_id: int, ranges: Sequence[tuple[dt.date, dt.date]]
) -> list[_Activity]:
    """Bucket totals for each ``(start, end)`` range (disjoint), in one query."""
    out = [_Activity() for _ in ranges]
    if not ranges:
        return out
    order = sorted(range(len(ranges)), key=lambda i: ranges[i][0])
    starts = [ranges[i][0] for i in order]
    posting = _posting_date()
    bucket = bucket_expression(_card_payments_id(session))
    query = (
        select(
            posting.label("day"),
            bucket.label("bucket"),
            func.sum(Transaction.amount).label("total"),
            func.count().label("n"),
        )
        .where(
            *_counted(account_id),
            posting >= min(starts),
            posting <= max(end for _, end in ranges),
        )
        .group_by(posting, bucket)
    )
    for row in session.execute(query):
        index = bisect_right(starts, row.day) - 1
        if index < 0:
            continue
        target = order[index]
        if row.day > ranges[target][1]:
            continue
        acc = out[target]
        acc.totals[row.bucket] += to_money(row.total)
        acc.counts[row.bucket] += row.n
    return out


# --------------------------------------------------------------------------- summary


def _check(cycle: StatementCycle, net: Decimal) -> GateDCheck:
    if cycle.status == OPEN:
        return GateDCheck(state=CHECK_OPEN, net_activity=net)
    provisional = cycle.status == SETTLING
    new_balance = cycle.reported.new_balance
    if cycle.previous_new_balance is not None:
        previous, source = cycle.previous_new_balance, "previous_statement"
    elif cycle.reported.previous_balance is not None:
        previous, source = cycle.reported.previous_balance, "this_statement"
    else:
        previous, source = None, None
    if new_balance is None or previous is None:
        return GateDCheck(
            state=CHECK_UNAVAILABLE,
            net_activity=net,
            previous_balance=previous,
            previous_source=source,
            new_balance=new_balance,
            missing="new_balance" if new_balance is None else "previous_balance",
            provisional=provisional,
        )
    expected = to_money(new_balance - previous)
    difference = to_money(net - expected)
    return GateDCheck(
        state=CHECK_PASS if difference == ZERO else CHECK_FAIL,
        net_activity=net,
        previous_balance=previous,
        previous_source=source,
        new_balance=new_balance,
        expected_activity=expected,
        difference=difference,
        provisional=provisional,
    )


def _summarize(cycle: StatementCycle, activity: _Activity) -> StatementSummary:
    totals = {b: to_money(activity.totals[b]) for b in BUCKETS}
    net = to_money(sum(totals.values(), ZERO))
    reported = cycle.reported
    if cycle.previous_new_balance is not None:
        basis_value, basis = cycle.previous_new_balance, "previous_statement"
    elif reported.previous_balance is not None:
        basis_value, basis = reported.previous_balance, "this_statement"
    else:
        basis_value, basis = None, None
    app_new = to_money(basis_value + net) if basis_value is not None else None
    payments_credits = to_money(totals[PAYMENTS] + totals[CREDITS])
    fields = (
        FieldCheck(
            "previous_balance",
            "Previous Balance",
            "",
            cycle.previous_new_balance,
            reported.previous_balance,
        ),
        FieldCheck(
            "payments_credits",
            "Payments/Credits",
            "−",
            payments_credits,
            -reported.payments_credits if reported.payments_credits is not None else None,
        ),
        FieldCheck("new_charges", "New Charges", "+", totals[NEW_CHARGES], reported.new_charges),
        FieldCheck("fees", "Fees", "+", totals[FEES], reported.fees),
        FieldCheck("interest", "Interest Charged", "+", totals[INTEREST], reported.interest),
        FieldCheck("new_balance", "New Balance", "=", app_new, reported.new_balance),
    )
    return StatementSummary(
        cycle=cycle,
        payments=totals[PAYMENTS],
        credits=totals[CREDITS],
        new_charges=totals[NEW_CHARGES],
        fees=totals[FEES],
        interest=totals[INTEREST],
        net_activity=net,
        counts=tuple((b, activity.counts[b]) for b in BUCKETS),
        previous_balance=cycle.previous_new_balance,
        new_balance=app_new,
        balance_basis=basis,
        fields=fields,
        check=_check(cycle, net),
    )


def summary(session: Session, cycle: StatementCycle) -> StatementSummary:
    """THE totals of one statement cycle and their comparison with the statement."""
    activity = _activity(session, cycle.account_id, [(cycle.start, cycle.closing_date)])[0]
    return _summarize(cycle, activity)


def overview(
    session: Session, account_id: int, *, today: dt.date | None = None
) -> list[StatementSummary]:
    """:func:`summary` for every cycle of :func:`cycles` (newest first), in one query."""
    found = cycles(session, account_id, today=today)
    activity = _activity(session, account_id, [(c.start, c.closing_date) for c in found])
    return [_summarize(c, a) for c, a in zip(found, activity, strict=True)]


# --------------------------------------------------------------------------- transactions


def _line_query(session: Session):
    posting = _posting_date()
    bucket = bucket_expression(_card_payments_id(session))
    return (
        select(
            Transaction.id,
            Transaction.posted_date,
            Transaction.date,
            Transaction.authorized_date,
            Transaction.name,
            Transaction.merchant_name,
            Transaction.amount,
            Transaction.source,
            Transaction.pending,
            bucket.label("bucket"),
            posting.label("posting"),
            Category.id.label("category_id"),
            Category.name.label("category_name"),
            Category.color.label("category_color"),
            Category.is_hidden.label("category_hidden"),
        )
        .join(Category, Category.id == Transaction.category_id)
        .order_by(posting, func.coalesce(Transaction.authorized_date, Transaction.date))
        .order_by(Transaction.id)
    )


def _line(row: Any, side: str | None = None) -> StatementLine:
    return StatementLine(
        transaction_id=row.id,
        posted_date=None if row.pending else row.posting,
        date=row.authorized_date or row.date,
        name=row.name,
        merchant_name=row.merchant_name,
        amount=to_money(row.amount),
        bucket=row.bucket,
        category=CategoryRef(
            row.category_id, row.category_name, row.category_color, row.category_hidden
        ),
        source=row.source,
        pending=row.pending,
        side=side,
    )


def transactions_in(session: Session, cycle: StatementCycle) -> list[StatementLine]:
    """The posted transactions that make up the cycle, by posting date."""
    posting = _posting_date()
    query = _line_query(session).where(
        *_counted(cycle.account_id), posting >= cycle.start, posting <= cycle.closing_date
    )
    return [_line(row) for row in session.execute(query)]


def group_by_bucket(lines: Sequence[StatementLine]) -> tuple[BucketGroup, ...]:
    """The lines in the Amex section order; empty buckets are left out."""
    groups = []
    for bucket in BUCKETS:
        members = tuple(line for line in lines if line.bucket == bucket)
        if members:
            total = to_money(sum((line.amount for line in members), ZERO))
            groups.append(BucketGroup(bucket, BUCKET_LABELS[bucket], total, members))
    return tuple(groups)


def near_boundary(
    session: Session, cycle: StatementCycle, days: int = BOUNDARY_DAYS
) -> tuple[Boundary, Boundary]:
    """Rows posted within ``days`` days either side of the cycle's start and closing date.

    Each boundary lists the last ``days`` days of the earlier cycle and the first ``days`` of the
    later one (``side``: ``previous``/``this`` at the start, ``this``/``next`` at the end), where
    mismatches usually come from. The closing boundary also lists pending rows dated in that
    window (they may still post into either statement).
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    posting = _posting_date()
    out = []
    for edge, boundary, before, after in (
        ("start", cycle.previous_closing, "previous", "this"),
        ("end", cycle.closing_date, "this", "next"),
    ):
        low = boundary - dt.timedelta(days=days - 1)
        high = boundary + dt.timedelta(days=days)
        query = _line_query(session).where(
            *_counted(cycle.account_id), posting >= low, posting <= high
        )
        lines = [
            _line(row, before if row.posting <= boundary else after)
            for row in session.execute(query)
        ]
        if edge == "end":
            pending = _line_query(session).where(
                Transaction.account_id == cycle.account_id,
                Transaction.pending.is_(True),
                Transaction.duplicate_status != "suspect",
                Transaction.date >= low,
                Transaction.date <= high,
            )
            lines.extend(_line(row, "pending") for row in session.execute(pending))
        out.append(Boundary(boundary, edge, days, tuple(lines)))
    return out[0], out[1]


def pending_in(session: Session, cycle: StatementCycle) -> PendingInfo:
    """Pending rows dated inside the cycle (never counted until they post)."""
    count, total = session.execute(
        select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == cycle.account_id,
            Transaction.pending.is_(True),
            Transaction.duplicate_status != "suspect",
            Transaction.date >= cycle.start,
            Transaction.date <= cycle.closing_date,
        )
    ).one()
    return PendingInfo(count, to_money(total))


def detail(
    session: Session, account_id: int, closing_date: dt.date, *, today: dt.date | None = None
) -> StatementDetail | None:
    """The cycle page's data, or ``None`` when no cycle of the account closes that day."""
    today = today or clock.today()
    account = session.get(Account, account_id)
    if account is None:
        return None
    every = cycles(session, account_id, today=today)
    index = next((i for i, c in enumerate(every) if c.closing_date == closing_date), None)
    if index is None:
        cycle = find_cycle(session, account_id, closing_date, today=today)
        if cycle is None:
            return None
        later = earlier = None
    else:
        cycle = every[index]
        later = every[index - 1] if index > 0 else None
        earlier = every[index + 1] if index + 1 < len(every) else None
    lines = transactions_in(session, cycle)
    return StatementDetail(
        account=_option(account),
        summary=summary(session, cycle),
        groups=group_by_bucket(lines),
        boundaries=near_boundary(session, cycle),
        pending=pending_in(session, cycle),
        earlier=earlier,
        later=later,
    )


# --------------------------------------------------------------------------- input parsing


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_date(value: Any) -> dt.date:
    """A date from a form (``YYYY-MM-DD``) or a ``date``; ``ValueError`` with a user message."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError("Enter a date like 2026-10-27.") from None


def parse_amount(value: Any, *, balance: bool = False) -> Decimal | None:
    """An amount typed from the statement; blank → ``None``.

    ``balance=True`` also accepts the statement's credit-balance suffix (``$12.34 CR`` → −12.34).
    """
    if _blank(value):
        return None
    if isinstance(value, Decimal | int) and not isinstance(value, bool):
        return to_money(value)
    text = str(value).strip()
    credit = False
    if balance and _CR_SUFFIX.search(text):
        text = _CR_SUFFIX.sub("", text)
        credit = True
    amount = parse_money_input(text)
    return -abs(amount) if credit else amount


def _parse_reported(values: Mapping[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for name, _label in REPORTED_FIELDS:
        try:
            amount = parse_amount(
                values.get(name), balance=name in ("previous_balance", "new_balance")
            )
        except ValueError as exc:
            errors[name] = str(exc)
            continue
        if name == "payments_credits" and amount is not None:
            amount = abs(amount)  # printed with a minus; stored positive
        parsed[name] = amount
    notes = values.get("notes")
    notes = notes.strip() if isinstance(notes, str) else notes
    if notes and len(notes) > MAX_NOTES_LENGTH:
        errors["notes"] = f"Keep notes under {MAX_NOTES_LENGTH:,} characters."
    parsed["notes"] = notes or None
    return parsed


# --------------------------------------------------------------------------- validation


def _closing_date_error(day: dt.date, today: dt.date) -> str | None:
    """Why ``day`` can't be a statement's closing date (a year typo, say), else None."""
    if day < EARLIEST_CLOSING_DATE:
        return f"Enter a closing date in {EARLIEST_CLOSING_DATE.year} or later."
    if day > today + dt.timedelta(days=MAX_FUTURE_DAYS):
        return "That date is too far in the future."
    return None


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def _too_close(others: Sequence[Statement], day: dt.date) -> str | None:
    for other in others:
        gap = abs((other.closing_date - day).days)
        if gap == 0:
            return f"A statement already closes on {_long(day)}."
        if gap <= SAME_STATEMENT_DAYS:
            return (
                f"That is {_days(gap)} from the statement that closes on "
                f"{_long(other.closing_date)}; edit that one instead."
            )
    return None


def _can_combine(
    row: Statement | None, target: Statement, current: dt.date, stored: Sequence[Statement]
) -> bool:
    """Moving ``row`` (closing on ``current``) onto ``target`` combines them: the target is the
    Amex statement next to it, within :data:`SAME_STATEMENT_DAYS`, and at most one of the two
    carries a Plaid issue date (the snapshot key)."""
    if row is None or target.id == row.id or target.closing_date_source != "liabilities":
        return False
    if abs((target.closing_date - current).days) > SAME_STATEMENT_DAYS:
        return False
    if row.plaid_issue_date is not None and target.plaid_issue_date is not None:
        return False
    low, high = sorted((current, target.closing_date))
    return not any(low < r.closing_date < high for r in stored)


def _move_check(
    stored: Sequence[Statement],
    row: Statement | None,
    current: dt.date,
    new: dt.date,
    period_start: dt.date | None,
    today: dt.date,
) -> tuple[str | None, Statement | None]:
    """``(error, combine_with)`` for moving the statement closing on ``current`` to ``new``:
    an error for the user, or the statement it combines with (both None: a plain move)."""
    if new == current:
        return None, None
    problem = _closing_date_error(new, today)
    if problem:
        return problem, None
    others = [r for r in stored if row is None or r.id != row.id]
    target = next((r for r in others if r.closing_date == new), None)
    if target is not None and _can_combine(row, target, current, stored):
        return None, target
    near = sorted(
        (r for r in others if abs((r.closing_date - new).days) <= SAME_STATEMENT_DAYS),
        key=lambda r: abs((r.closing_date - new).days),
    )
    if near and near[0].closing_date != new and _can_combine(row, near[0], current, stored):
        gap = abs((near[0].closing_date - new).days)
        amex = _long(near[0].closing_date)
        return (
            f"That is {_days(gap)} from the statement Amex reported for {amex}. To combine the "
            f"two, set this closing date to {amex}."
        ), None
    problem = _too_close(others, new)
    if problem:
        return problem, None
    before = [r.closing_date for r in others if r.closing_date < current]
    after = [r.closing_date for r in others if r.closing_date > current]
    if (before and new <= max(before)) or (after and new >= min(after)):
        low = _long(max(before)) if before else "the start"
        high = _long(min(after)) if after else "today"
        return (
            f"Keep the closing date between the statements on either side ({low} and {high})."
        ), None
    if period_start is not None and new < period_start:
        return f"The closing date can't be before the period start ({_long(period_start)}).", None
    return None, None


def _long(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


# --------------------------------------------------------------------------- changes


def _require_cycle(
    session: Session, account_id: int, closing_date: dt.date, today: dt.date | None = None
) -> StatementCycle:
    _account(session, account_id)
    cycle = find_cycle(session, account_id, closing_date, today=today)
    if cycle is None:
        raise LookupError(f"no statement of account {account_id} closes on {closing_date}")
    return cycle


def _row(session: Session, cycle: StatementCycle) -> Statement:
    """The cycle's ``statements`` row, created (closing date confirmed by the user) if needed."""
    if cycle.statement_id is not None:
        row = session.get(Statement, cycle.statement_id)
        if row is not None:
            return row
    row = Statement(
        account_id=cycle.account_id,
        closing_date=cycle.closing_date,
        closing_date_source="user",
    )
    session.add(row)
    session.flush()
    return row


def _not_yet_closed(cycle: StatementCycle) -> StatementError:
    return StatementError(
        {
            "period": (
                f"This period hasn't closed yet. Type its numbers once Amex issues the statement "
                f"(it should close on {_long(cycle.closing_date)})."
            )
        }
    )


def add_statement(
    session: Session, account_id: int, closing_date: Any, *, today: dt.date | None = None
) -> Statement:
    """Record a statement the app doesn't know yet, by its closing date (source ``user``)."""
    _account(session, account_id)
    today = today or clock.today()
    try:
        day = parse_date(closing_date)
    except ValueError as exc:
        raise StatementError({"closing_date": str(exc)}) from None
    stored = _stored(session, account_id)
    problem = _closing_date_error(day, today) or _too_close(stored, day)
    if problem:
        raise StatementError({"closing_date": problem})
    if stored and day < stored[0].closing_date:
        # The old earliest statement now has a previous closing date: its start is derived.
        stored[0].period_start_override = None
    row = Statement(account_id=account_id, closing_date=day, closing_date_source="user")
    session.add(row)
    session.flush()
    return row


def remove_statement(
    session: Session, account_id: int, closing_date: dt.date, *, today: dt.date | None = None
) -> dt.date:
    """Remove a statement the user added (a mistaken date); its typed values go with it.

    Amex's own statements (with a Plaid issue date) can't be removed: sync would add them back.
    Returns the removed closing date. ``LookupError`` when no stored statement closes that day.
    """
    cycle = _require_cycle(session, account_id, closing_date, today)
    row = session.get(Statement, cycle.statement_id) if cycle.statement_id else None
    if row is None:
        raise LookupError(f"no stored statement of account {account_id} closes on {closing_date}")
    if not _removable(row):
        raise StatementError(
            {"remove": "This statement came from Amex, so it can't be removed. Correct its date."}
        )
    removed = row.closing_date
    session.delete(row)
    session.flush()
    return removed


def set_closing_date(
    session: Session, account_id: int, closing_date: dt.date, new_closing_date: Any
) -> Statement:
    """Correct a cycle's closing date (the source becomes ``user``; sync never changes it)."""
    return save_statement(
        session, account_id, closing_date, {"closing_date": new_closing_date}, only=("closing",)
    )


def set_period_start(
    session: Session, account_id: int, closing_date: dt.date, period_start: Any
) -> Statement:
    """Set (or clear, with ``None``) the earliest statement's period start."""
    return save_statement(
        session, account_id, closing_date, {"period_start": period_start}, only=("period_start",)
    )


def record_reported(
    session: Session,
    account_id: int,
    closing_date: dt.date,
    *,
    previous_balance: Any = None,
    payments_credits: Any = None,
    new_charges: Any = None,
    fees: Any = None,
    interest: Any = None,
    new_balance: Any = None,
    notes: Any = None,
) -> Statement:
    """Store the values typed from the statement (``None``/blank clears a value).

    Amounts may be ``Decimal`` or text as printed (``$1,530.00``, ``−$1,530.00``, ``12.34 CR``
    for a credit balance). Payments/Credits is stored positive whatever its printed sign. A
    changed New Balance becomes ``user`` sourced (sync never overwrites it).
    """
    values = {
        "previous_balance": previous_balance,
        "payments_credits": payments_credits,
        "new_charges": new_charges,
        "fees": fees,
        "interest": interest,
        "new_balance": new_balance,
        "notes": notes,
    }
    return save_statement(session, account_id, closing_date, values, only=("reported",))


def save_statement(
    session: Session,
    account_id: int,
    closing_date: dt.date,
    values: Mapping[str, Any],
    *,
    only: Sequence[str] = ("reported", "closing", "period_start"),
    today: dt.date | None = None,
) -> Statement:
    """Validate every field of the statement form, then apply them (the page's Save).

    ``values`` holds ``closing_date``, ``period_start`` (earliest statement only), the six
    reported amounts and ``notes``. Raises :class:`StatementError` with every field's problem
    and changes nothing then (``period`` when the cycle is the open period's estimate);
    ``LookupError`` when no cycle closes on ``closing_date``. Moving a user-entered statement
    onto the Amex statement next to it combines the two and returns that statement's row.
    """
    today = today or clock.today()
    cycle = _require_cycle(session, account_id, closing_date, today)
    if cycle.is_estimate_open:
        raise _not_yet_closed(cycle)
    stored = _stored(session, account_id)
    current = next((r for r in stored if r.id == cycle.statement_id), None)
    errors: dict[str, str] = {}

    reported: dict[str, Any] | None = None
    if "reported" in only:
        reported = _parse_reported(values, errors)

    new_closing = closing_date
    if "closing" in only and not _blank(values.get("closing_date")):
        try:
            new_closing = parse_date(values.get("closing_date"))
        except ValueError as exc:
            errors["closing_date"] = str(exc)

    override_given = "period_start" in only and "period_start" in values
    new_override = cycle.period_start_override
    if override_given:
        if not cycle.can_set_period_start:
            errors["period_start"] = "Only the earliest statement's period start can be set."
        elif not _blank(values.get("period_start")):
            try:
                new_override = parse_date(values.get("period_start"))
            except ValueError as exc:
                errors["period_start"] = str(exc)
        else:
            new_override = None

    combine_with = None
    if "closing_date" not in errors and "period_start" not in errors:
        problem, combine_with = _move_check(
            stored, current, closing_date, new_closing, new_override, today
        )
        if problem:
            errors["closing_date"] = problem
        elif new_override is not None and new_override > new_closing:
            errors["period_start"] = "The period start must be on or before the closing date."
        elif new_override is not None and (new_closing - new_override).days > (
            MAX_FIRST_PERIOD_DAYS
        ):
            errors["period_start"] = (
                "The period start must be within two months before the closing date."
            )

    if errors:
        raise StatementError(errors)

    if combine_with is not None and current is not None:
        return _combine(session, current, combine_with, reported, new_override, override_given)

    row = _row(session, cycle)
    if reported is not None:
        _apply_reported(row, reported)
    if new_closing != row.closing_date:
        row.closing_date = new_closing
        row.closing_date_source = "user"
    if not cycle.can_set_period_start:
        # Not the earliest statement (any more): its start is the previous closing date + 1.
        row.period_start_override = None
    elif override_given and row.period_start_override != new_override:
        row.period_start_override = new_override
    session.flush()
    return row


def _combine(
    session: Session,
    row: Statement,
    target: Statement,
    reported: Mapping[str, Any] | None,
    override: dt.date | None,
    override_given: bool,
) -> Statement:
    """Fold the statement the user is editing (``row``) into the Amex statement it duplicates.

    The values being saved win; a value left empty keeps the Amex statement's. The issue date
    (sync's key) stays on the surviving row, so later snapshots keep landing on it.
    """
    values = reported if reported is not None else _row_values(row)
    for name, _label in REPORTED_FIELDS:
        if name == "new_balance" or values[name] is None:
            continue
        setattr(target, f"reported_{name}", values[name])
    new_balance = values["new_balance"]
    if new_balance is not None:
        source = "user" if reported is not None else row.reported_new_balance_source
        if new_balance != target.reported_new_balance:
            target.reported_new_balance = new_balance
            target.reported_new_balance_source = source or "user"
    if values["notes"]:
        target.notes = values["notes"]
    target.checked_at = row.checked_at or target.checked_at
    was_earliest = target.closing_date > row.closing_date and not any(
        r for r in _stored(session, row.account_id) if r.closing_date < row.closing_date
    )
    if was_earliest:
        target.period_start_override = override if override_given else row.period_start_override
    issue_date = row.plaid_issue_date
    session.delete(row)
    session.flush()
    if target.plaid_issue_date is None and issue_date is not None:
        target.plaid_issue_date = issue_date
    session.flush()
    return target


def _row_values(row: Statement) -> dict[str, Any]:
    reported = _reported(row)
    values: dict[str, Any] = {name: getattr(reported, name) for name, _label in REPORTED_FIELDS}
    values["notes"] = row.notes
    return values


def _apply_reported(row: Statement, values: Mapping[str, Any]) -> None:
    row.reported_previous_balance = values["previous_balance"]
    row.reported_payments_credits = values["payments_credits"]
    row.reported_new_charges = values["new_charges"]
    row.reported_fees = values["fees"]
    row.reported_interest = values["interest"]
    new_balance = values["new_balance"]
    if new_balance is None:
        row.reported_new_balance = None
        row.reported_new_balance_source = None
    elif new_balance != row.reported_new_balance:
        row.reported_new_balance = new_balance
        row.reported_new_balance_source = "user"
    row.notes = values["notes"]


def mark_checked(
    session: Session,
    account_id: int,
    closing_date: dt.date,
    *,
    checked: bool = True,
    today: dt.date | None = None,
) -> Statement | None:
    """Record that the user compared this statement (``checked=False`` clears it).

    An open period can't be checked (``StatementError``): its statement isn't issued yet.
    Clearing a period that has no stored statement changes nothing and returns None.
    """
    cycle = _require_cycle(session, account_id, closing_date, today)
    if checked and cycle.status == OPEN:
        raise _not_yet_closed(cycle)
    if not checked and cycle.statement_id is None:
        return None
    row = _row(session, cycle)
    row.checked_at = clock.now() if checked else None
    session.flush()
    return row
