"""Spending over ``v_allocations`` — the one definition of each number (ARCHITECTURE §6).

* :func:`period_spending` is the ONLY definition of "spending in a budget period": the Budgets page,
  every "this period" dashboard widget and the budget alerts call it. It reads ``app_settings``:
  the date basis (posting date in statement mode, ``spend_date = COALESCE(authorized_date, date)``
  in calendar mode) and ``include_pending_in_budgets``. :func:`periods_spending` is the same for
  several periods in one query (a rollover chain's earlier periods); ``period_spending`` is it
  for one period.
* Only categories of kind ``expense`` count. Charges add; refunds (negative amounts in an expense
  category) subtract (TXN-3). Card payments (``transfer``) and credits/rewards (``income``) never
  count. Hidden categories still count.
* Splits count only when they sum exactly to the amount, and suspected duplicates are excluded —
  both handled by the ``v_allocations`` view.
* Trends (:func:`monthly_spending`) always use calendar months by ``spend_date``.
* :func:`daily_spending` splits a budget period into days on the same rules (the dashboard's
  pace chart); its days add up to :func:`period_spending`'s total.
* **Income** (wave 3, ARCHITECTURE §6 "Income and net"): money in, booked to categories of kind
  ``income`` from any account, as a POSITIVE number: ``−sum(v_allocations.amount)``. Each income
  function mirrors its spending counterpart's date basis, pending setting and suspect handling
  (:func:`period_income`/:func:`periods_income` ↔ :func:`period_spending`/
  :func:`periods_spending`, :func:`monthly_income` ↔ :func:`monthly_spending`,
  :func:`income_by_category` ↔ :func:`spending_by_category`), by running the same query with
  ``kinds=("income",)``. Net = income − spending. Transfers (``transfer``) are neither.

In the posting-date basis a pending row (no ``posted_date`` yet) is dated by its Plaid ``date``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ARRAY, Date, case, cast, func, literal, select
from sqlalchemy.orm import Session

from budgetapp.models import Category, Transaction, get_app_settings, v_allocations
from budgetapp.money import ZERO, to_money
from budgetapp.periods import Period, months_between

BASIS_SPEND_DATE = "spend_date"
BASIS_POSTED_DATE = "posted_date"
BASES = (BASIS_SPEND_DATE, BASIS_POSTED_DATE)
#: The category kinds that count as spending, and as income.
SPENDING_KINDS = ("expense",)
INCOME_KINDS = ("income",)


def basis_for_mode(mode: str) -> str:
    """``calendar`` → ``spend_date``; ``statement`` → ``posted_date``."""
    if mode == "calendar":
        return BASIS_SPEND_DATE
    if mode == "statement":
        return BASIS_POSTED_DATE
    raise ValueError(f"unknown budget period mode {mode!r}")


def basis_date(basis: str):
    """The ``v_allocations`` date expression for a basis."""
    if basis == BASIS_SPEND_DATE:
        return v_allocations.c.spend_date
    if basis == BASIS_POSTED_DATE:
        return func.coalesce(v_allocations.c.posted_date, v_allocations.c.date)
    raise ValueError(f"unknown date basis {basis!r}")


@dataclass(frozen=True, slots=True)
class CategorySpending:
    """Net spending in one category: ``amount = charges + credits`` (credits are negative)."""

    category_id: int
    name: str
    kind: str
    color: str | None
    icon: str | None
    is_hidden: bool
    amount: Decimal
    charges: Decimal
    credits: Decimal
    transaction_count: int


@dataclass(frozen=True, slots=True)
class PeriodSpending:
    """Spending in a budget period, per category (largest first) and in total."""

    period: Period
    basis: str
    include_pending: bool
    categories: tuple[CategorySpending, ...]
    total: Decimal

    def amount_for(self, category_id: int) -> Decimal:
        """Net spending in a category (``ZERO`` when it had none)."""
        for row in self.categories:
            if row.category_id == category_id:
                return row.amount
        return ZERO

    @property
    def by_category(self) -> dict[int, CategorySpending]:
        return {row.category_id: row for row in self.categories}


@dataclass(frozen=True, slots=True)
class MonthSpending:
    """One calendar month of :func:`monthly_spending`."""

    month: Period
    categories: tuple[CategorySpending, ...]
    total: Decimal

    def amount_for(self, category_id: int) -> Decimal:
        for row in self.categories:
            if row.category_id == category_id:
                return row.amount
        return ZERO


@dataclass(frozen=True, slots=True)
class MerchantSpending:
    merchant: str
    amount: Decimal
    transaction_count: int
    #: The spending categories the amount is in (where the Transactions page's ``category=``
    #: filter finds its rows), ascending.
    category_ids: tuple[int, ...] = ()


def _filters(start: dt.date, end: dt.date, basis: str, include_pending: bool, kinds: Iterable[str]):
    date_expr = basis_date(basis)
    conditions = [date_expr >= start, date_expr <= end, Category.kind.in_(tuple(kinds))]
    if not include_pending:
        conditions.append(v_allocations.c.pending.is_(False))
    return conditions


def _category_spending(r) -> CategorySpending:
    """A result row of :func:`_category_columns` as a :class:`CategorySpending`."""
    return CategorySpending(
        category_id=r.category_id,
        name=r.name,
        kind=r.kind,
        color=r.color,
        icon=r.icon,
        is_hidden=r.is_hidden,
        amount=to_money(r.amount),
        charges=to_money(r.charges),
        credits=to_money(r.credits),
        transaction_count=r.transaction_count,
    )


def _largest_first(row: CategorySpending) -> tuple[Decimal, str]:
    return (-row.amount, row.name.casefold())


def _category_rows(session: Session, query) -> list[CategorySpending]:
    rows = [_category_spending(r) for r in session.execute(query)]
    rows.sort(key=_largest_first)
    return rows


def _category_columns():
    amount = v_allocations.c.amount
    return (
        Category.id.label("category_id"),
        Category.name,
        Category.kind,
        Category.color,
        Category.icon,
        Category.is_hidden,
        func.coalesce(func.sum(amount), 0).label("amount"),
        func.coalesce(func.sum(case((amount > 0, amount), else_=0)), 0).label("charges"),
        func.coalesce(func.sum(case((amount < 0, amount), else_=0)), 0).label("credits"),
        func.count(v_allocations.c.transaction_id.distinct()).label("transaction_count"),
    )


def spending_by_category(
    session: Session,
    start: dt.date,
    end: dt.date,
    *,
    basis: str,
    include_pending: bool,
    kinds: Iterable[str] = ("expense",),
) -> list[CategorySpending]:
    """Net spending per category between ``start`` and ``end`` (inclusive) on ``basis``.

    Categories with no allocations in range are omitted. Sorted by amount, largest first.
    """
    query = (
        select(*_category_columns())
        .select_from(v_allocations)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(*_filters(start, end, basis, include_pending, kinds))
        .group_by(Category.id)
    )
    return _category_rows(session, query)


def period_spending(session: Session, period: Period) -> PeriodSpending:
    """THE spending for a budget period (see module docstring). Reads ``app_settings``."""
    return periods_spending(session, (period,))[(period.start, period.end)]


def periods_spending(
    session: Session, periods: Iterable[Period]
) -> dict[tuple[dt.date, dt.date], PeriodSpending]:
    """:func:`period_spending` of several budget periods, read in one query, keyed by
    ``(start, end)``. The periods must not overlap (budget periods never do).

    With the periods sorted, a day lies in period number ``started`` (1-based) when ``started``
    periods have started on or before it and fewer have ended before it (PostgreSQL's
    ``width_bucket`` counts both); otherwise it falls between two periods.
    """
    settings = get_app_settings(session)
    basis = basis_for_mode(settings.budget_period_mode)
    include_pending = settings.include_pending_in_budgets
    ordered = _ordered_periods(periods)
    found = _rows_by_period(session, ordered, basis, include_pending, SPENDING_KINDS)
    result = {}
    for number, period in enumerate(ordered, start=1):
        rows = sorted(found.get(number, ()), key=_largest_first)
        total = to_money(sum((row.amount for row in rows), Decimal("0")))
        result[(period.start, period.end)] = PeriodSpending(
            period, basis, include_pending, tuple(rows), total
        )
    return result


def _ordered_periods(periods: Iterable[Period]) -> list[Period]:
    """The distinct periods, oldest first; ``ValueError`` when two overlap."""
    ordered = sorted({(p.start, p.end): p for p in periods}.values(), key=lambda p: p.start)
    for before, after in zip(ordered, ordered[1:], strict=False):
        if after.start <= before.end:
            raise ValueError(f"budget periods overlap: {before.label} and {after.label}")
    return ordered


def _rows_by_period(
    session: Session,
    ordered: list[Period],
    basis: str,
    include_pending: bool,
    kinds: Iterable[str],
) -> dict[int, list[CategorySpending]]:
    """Per-category sums of ``kinds`` in each of ``ordered`` (non-overlapping, oldest first),
    keyed by the period's 1-based position; one query for all of them."""
    found: dict[int, list[CategorySpending]] = {}
    if not ordered:
        return found
    day = basis_date(basis)
    starts = [p.start for p in ordered]
    ends = [p.end + dt.timedelta(days=1) for p in ordered]  # the day after each period
    started = func.width_bucket(day, literal(starts, ARRAY(Date)))
    ended = func.width_bucket(day, literal(ends, ARRAY(Date)))
    conditions = _filters(starts[0], ordered[-1].end, basis, include_pending, kinds)
    query = (
        select(started.label("number"), *_category_columns())
        .select_from(v_allocations)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(*conditions, ended < started)
        .group_by(started, Category.id)
    )
    for r in session.execute(query):
        found.setdefault(r.number, []).append(_category_spending(r))
    return found


def monthly_spending(
    session: Session,
    first_month: dt.date,
    last_month: dt.date,
    *,
    include_pending: bool | None = None,
    kinds: Iterable[str] = ("expense",),
) -> list[MonthSpending]:
    """Spending per calendar month (by ``spend_date``) for every month from ``first_month`` to
    ``last_month`` inclusive (any day within each month works), oldest first; empty months included.

    ``include_pending`` defaults to ``app_settings.include_pending_in_budgets`` (trends use the same
    pending setting as budgets).
    """
    if include_pending is None:
        include_pending = get_app_settings(session).include_pending_in_budgets
    months = months_between(first_month, last_month)
    if not months:
        return []
    month_start = cast(func.date_trunc("month", v_allocations.c.spend_date), Date).label("month")
    query = (
        select(month_start, *_category_columns())
        .select_from(v_allocations)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(*_filters(months[0].start, months[-1].end, BASIS_SPEND_DATE, include_pending, kinds))
        .group_by(month_start, Category.id)
    )
    buckets: dict[dt.date, list[CategorySpending]] = {m.start: [] for m in months}
    for r in session.execute(query):
        buckets.setdefault(r.month, []).append(
            CategorySpending(
                category_id=r.category_id,
                name=r.name,
                kind=r.kind,
                color=r.color,
                icon=r.icon,
                is_hidden=r.is_hidden,
                amount=to_money(r.amount),
                charges=to_money(r.charges),
                credits=to_money(r.credits),
                transaction_count=r.transaction_count,
            )
        )
    out = []
    for month in months:
        rows = sorted(buckets[month.start], key=lambda row: (-row.amount, row.name.casefold()))
        total = to_money(sum((row.amount for row in rows), Decimal("0")))
        out.append(MonthSpending(month, tuple(rows), total))
    return out


def top_merchants(
    session: Session,
    start: dt.date,
    end: dt.date,
    *,
    basis: str,
    include_pending: bool,
    limit: int = 10,
    kinds: Iterable[str] = ("expense",),
) -> list[MerchantSpending]:
    """Merchants with the most net spending (``merchant_name``, else the description).

    A merchant is matched the way the Transactions page's ``merchant=`` filter matches it
    (``transactions.merchant_key_expr``: case and runs of spaces ignored), so "Whole Foods" and
    "WHOLE  FOODS" are one merchant, shown by its most frequent merchant name (else its most
    frequent description). ``category_ids`` are the categories its spending is in.
    """
    # The list filter's own key, so a merchant's link lists exactly the rows it adds up
    # (imported here: services.transactions imports this module).
    from budgetapp.services.transactions import merchant_key_expr

    key = merchant_key_expr()
    shown = func.coalesce(
        func.mode().within_group(Transaction.merchant_name),
        func.mode().within_group(Transaction.name),
    )
    amount = func.sum(v_allocations.c.amount)
    query = (
        select(
            shown.label("merchant"),
            amount.label("amount"),
            func.count(v_allocations.c.transaction_id.distinct()).label("transaction_count"),
            func.array_agg(v_allocations.c.category_id.distinct()).label("category_ids"),
        )
        .select_from(v_allocations)
        .join(Transaction, Transaction.id == v_allocations.c.transaction_id)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(*_filters(start, end, basis, include_pending, kinds))
        .group_by(key)
        .having(amount > 0)
        .order_by(amount.desc(), key)
        .limit(limit)
    )
    return [
        MerchantSpending(
            r.merchant, to_money(r.amount), r.transaction_count, tuple(sorted(r.category_ids))
        )
        for r in session.execute(query)
    ]


def period_top_merchants(
    session: Session, period: Period, *, limit: int = 10
) -> list[MerchantSpending]:
    """:func:`top_merchants` for a budget period with the same basis/pending rules as
    :func:`period_spending` (for "this period" dashboard widgets)."""
    settings = get_app_settings(session)
    return top_merchants(
        session,
        period.start,
        period.end,
        basis=basis_for_mode(settings.budget_period_mode),
        include_pending=settings.include_pending_in_budgets,
        limit=limit,
    )


@dataclass(frozen=True, slots=True)
class DaySpending:
    """Net spending on one day of :func:`daily_spending`."""

    day: dt.date
    amount: Decimal


def daily_spending(session: Session, period: Period) -> list[DaySpending]:
    """Net spending per day of a budget period, on the same date basis and pending setting as
    :func:`period_spending` (the dashboard's day-by-day pace).

    Every day of the period is listed, oldest first, days without spending as ``0.00``; the
    amounts add up to ``period_spending(session, period).total``.
    """
    settings = get_app_settings(session)
    basis = basis_for_mode(settings.budget_period_mode)
    day = basis_date(basis)
    query = (
        select(day.label("day"), func.coalesce(func.sum(v_allocations.c.amount), 0).label("amount"))
        .select_from(v_allocations)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(
            *_filters(
                period.start,
                period.end,
                basis,
                settings.include_pending_in_budgets,
                ("expense",),
            )
        )
        .group_by(day)
    )
    amounts = {r.day: to_money(r.amount) for r in session.execute(query)}
    return [
        DaySpending(
            period.start + dt.timedelta(days=i),
            amounts.get(period.start + dt.timedelta(days=i), ZERO),
        )
        for i in range(period.days)
    ]


# --------------------------------------------------------------------------- income (wave 3)


@dataclass(frozen=True, slots=True)
class CategoryIncome:
    """Income in one category of kind ``income``, as a positive number.

    ``amount = received − taken_back``: ``received`` is the money in (Plaid's negative amounts,
    made positive) and ``taken_back`` any money out booked to the category (a reversed statement
    credit, a returned deposit), so ``amount = −sum(v_allocations.amount)``.
    """

    category_id: int
    name: str
    color: str | None
    icon: str | None
    is_hidden: bool
    amount: Decimal
    received: Decimal
    taken_back: Decimal
    transaction_count: int


@dataclass(frozen=True, slots=True)
class PeriodIncome:
    """Income in a budget period, per income category (largest first) and in total, on the
    same date basis and pending setting as :class:`PeriodSpending`."""

    period: Period
    basis: str
    include_pending: bool
    categories: tuple[CategoryIncome, ...]
    total: Decimal

    def amount_for(self, category_id: int) -> Decimal:
        """Income in a category (``ZERO`` when it had none)."""
        for row in self.categories:
            if row.category_id == category_id:
                return row.amount
        return ZERO

    @property
    def largest(self) -> CategoryIncome | None:
        """The category that brought in the most (``None`` when none brought anything in)."""
        return next((row for row in self.categories if row.amount > 0), None)


@dataclass(frozen=True, slots=True)
class MonthIncome:
    """One calendar month of :func:`monthly_income`."""

    month: Period
    categories: tuple[CategoryIncome, ...]
    total: Decimal

    def amount_for(self, category_id: int) -> Decimal:
        for row in self.categories:
            if row.category_id == category_id:
                return row.amount
        return ZERO


def _income(row: CategorySpending) -> CategoryIncome:
    """A :class:`CategorySpending` of an income category, signed as income."""
    return CategoryIncome(
        category_id=row.category_id,
        name=row.name,
        color=row.color,
        icon=row.icon,
        is_hidden=row.is_hidden,
        amount=to_money(-row.amount),
        received=to_money(-row.credits),
        taken_back=to_money(row.charges),
        transaction_count=row.transaction_count,
    )


def _income_rows(rows: Iterable[CategorySpending]) -> tuple[CategoryIncome, ...]:
    """Income rows, largest first (ties by name)."""
    return tuple(sorted((_income(r) for r in rows), key=lambda r: (-r.amount, r.name.casefold())))


def _income_total(rows: Iterable[CategoryIncome]) -> Decimal:
    return to_money(sum((row.amount for row in rows), Decimal("0")))


def income_by_category(
    session: Session, start: dt.date, end: dt.date, *, basis: str, include_pending: bool
) -> list[CategoryIncome]:
    """Income per income category between ``start`` and ``end`` (inclusive) on ``basis``:
    :func:`spending_by_category` of the ``income`` kind, signed as income. Largest first."""
    rows = spending_by_category(
        session, start, end, basis=basis, include_pending=include_pending, kinds=INCOME_KINDS
    )
    return list(_income_rows(rows))


def period_income(session: Session, period: Period) -> PeriodIncome:
    """Income in a budget period, next to :func:`period_spending` (reads ``app_settings`` the
    same way: the budget period mode's date basis and the pending setting)."""
    return periods_income(session, (period,))[(period.start, period.end)]


def periods_income(
    session: Session, periods: Iterable[Period]
) -> dict[tuple[dt.date, dt.date], PeriodIncome]:
    """:func:`period_income` of several budget periods, read in one query, keyed by
    ``(start, end)`` (the periods must not overlap), like :func:`periods_spending`."""
    settings = get_app_settings(session)
    basis = basis_for_mode(settings.budget_period_mode)
    include_pending = settings.include_pending_in_budgets
    ordered = _ordered_periods(periods)
    found = _rows_by_period(session, ordered, basis, include_pending, INCOME_KINDS)
    result = {}
    for number, period in enumerate(ordered, start=1):
        rows = _income_rows(found.get(number, ()))
        result[(period.start, period.end)] = PeriodIncome(
            period, basis, include_pending, rows, _income_total(rows)
        )
    return result


def monthly_income(
    session: Session,
    first_month: dt.date,
    last_month: dt.date,
    *,
    include_pending: bool | None = None,
) -> list[MonthIncome]:
    """Income per calendar month (by ``spend_date``) for every month from ``first_month`` to
    ``last_month`` inclusive, oldest first, empty months included: :func:`monthly_spending` of
    the ``income`` kind (the same pending default: the budgets' setting), signed as income."""
    months = monthly_spending(
        session, first_month, last_month, include_pending=include_pending, kinds=INCOME_KINDS
    )
    out = []
    for month in months:
        rows = _income_rows(month.categories)
        out.append(MonthIncome(month.month, rows, _income_total(rows)))
    return out
