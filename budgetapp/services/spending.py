"""Spending over ``v_allocations`` — the one definition of each number (ARCHITECTURE §6).

* :func:`period_spending` is the ONLY definition of "spending in a budget period": the Budgets page,
  every "this period" dashboard widget and the budget alerts call it. It reads ``app_settings``:
  the date basis (posting date in statement mode, ``spend_date = COALESCE(authorized_date, date)``
  in calendar mode) and ``include_pending_in_budgets``.
* Only categories of kind ``expense`` count. Charges add; refunds (negative amounts in an expense
  category) subtract (TXN-3). Card payments (``transfer``) and credits/rewards (``income``) never
  count. Hidden categories still count.
* Splits count only when they sum exactly to the amount, and suspected duplicates are excluded —
  both handled by the ``v_allocations`` view.
* Trends (:func:`monthly_spending`) always use calendar months by ``spend_date``.

In the posting-date basis a pending row (no ``posted_date`` yet) is dated by its Plaid ``date``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.orm import Session

from budgetapp.models import Category, Transaction, get_app_settings, v_allocations
from budgetapp.money import ZERO, to_money
from budgetapp.periods import Period, months_between

BASIS_SPEND_DATE = "spend_date"
BASIS_POSTED_DATE = "posted_date"
BASES = (BASIS_SPEND_DATE, BASIS_POSTED_DATE)


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


def _filters(start: dt.date, end: dt.date, basis: str, include_pending: bool, kinds: Iterable[str]):
    date_expr = basis_date(basis)
    conditions = [date_expr >= start, date_expr <= end, Category.kind.in_(tuple(kinds))]
    if not include_pending:
        conditions.append(v_allocations.c.pending.is_(False))
    return conditions


def _category_rows(session: Session, query) -> list[CategorySpending]:
    rows = []
    for r in session.execute(query):
        rows.append(
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
    rows.sort(key=lambda row: (-row.amount, row.name.casefold()))
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
    settings = get_app_settings(session)
    basis = basis_for_mode(settings.budget_period_mode)
    include_pending = settings.include_pending_in_budgets
    rows = spending_by_category(
        session, period.start, period.end, basis=basis, include_pending=include_pending
    )
    total = to_money(sum((row.amount for row in rows), Decimal("0")))
    return PeriodSpending(period, basis, include_pending, tuple(rows), total)


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
    """Merchants with the most net spending (``merchant_name``, else the description)."""
    merchant = func.coalesce(Transaction.merchant_name, Transaction.name)
    amount = func.sum(v_allocations.c.amount)
    query = (
        select(
            merchant.label("merchant"),
            amount.label("amount"),
            func.count(v_allocations.c.transaction_id.distinct()).label("transaction_count"),
        )
        .select_from(v_allocations)
        .join(Transaction, Transaction.id == v_allocations.c.transaction_id)
        .join(Category, Category.id == v_allocations.c.category_id)
        .where(*_filters(start, end, basis, include_pending, kinds))
        .group_by(merchant)
        .having(amount > 0)
        .order_by(amount.desc(), merchant)
        .limit(limit)
    )
    return [
        MerchantSpending(r.merchant, to_money(r.amount), r.transaction_count)
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
