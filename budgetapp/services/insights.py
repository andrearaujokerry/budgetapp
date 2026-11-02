"""Dashboard (INS-1) and Trends (INS-2) data.

Every amount here comes from :mod:`budgetapp.services.spending` and
:mod:`budgetapp.services.budgets` (ARCHITECTURE §6, principle 7); nothing re-sums transactions.

**Dashboard** (:func:`dashboard`): the current budget period exactly as the Budgets page shows it
(``budgets.overview`` of ``budgets.current_period``: calendar month or statement cycle, rollover,
overall target, annual budgets, the pending setting), plus:

* the top categories of ``overview.spending`` (``period_spending``), largest first, the rest
  folded into one remainder so the rows add up to the period's total;
* the top merchants (``spending.period_top_merchants``, same basis and pending rule);
* the day-by-day pace (``spending.daily_spending``) of this period and the previous one, and
  how much had been spent by the same day of the previous period;
* recurring charges expected before the period ends (``recurring.detect``);
* how many imported rows are held for review (``csv_import.list_suspects``).

**Trends** (:func:`trends`): spending per expense category per CALENDAR month by ``spend_date``
(``spending.monthly_spending``; the same pending setting as budgets, whatever the budget period
mode), for 12, 24 or 36 months ending with a chosen month. "The change from last month"
compares the latest month that has ended with the month before it: a month in progress is
shown "so far" and compared with the same days of the previous month instead, so a few days are
never set against a whole month. Months before the first transaction have no data (``None``,
not zero); the month the history starts in is partial. Averages count only complete months.
Hidden categories are listed (marked) because their spending counts in every total.

Services never commit.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.models import Category, Transaction, get_app_settings, v_allocations
from budgetapp.money import ZERO, to_money
from budgetapp.periods import Period
from budgetapp.services import budgets, csv_import, recurring, spending
from budgetapp.services.budgets import AnnualOverview, BudgetOverview
from budgetapp.services.recurring import RecurringSeries
from budgetapp.services.spending import (
    BASIS_SPEND_DATE,
    CategorySpending,
    MerchantSpending,
    basis_date,
)

#: Categories the dashboard lists by name; the rest are one remainder row.
TOP_CATEGORIES = 6
TOP_MERCHANTS = 5
#: Month spans Trends offers ("12 months or more", INS-2).
SPANS = (12, 24, 36)
DEFAULT_SPAN = 12
#: Categories drawn as their own series in the Trends chart; the rest fold into "Other".
CHART_SERIES = 7

# Change kinds (``Change.kind``).
UP = "up"
DOWN = "down"
SAME = "same"
NEW = "new"  # nothing the month before, spending now
GONE = "gone"  # spending the month before, nothing now
NONE = "none"  # nothing in either month

# Trend month status (``TrendMonth.status``).
COMPLETE = "complete"
FIRST = "first"  # the history starts after the month's first day: partial
CURRENT = "current"  # in progress (contains today)
BEFORE = "before"  # before the history starts: no data

_HUNDRED = Decimal(100)
_ONE = Decimal(1)


# --------------------------------------------------------------------------- shared types


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """The category fields lists show (``ui.category_badge`` reads them)."""

    id: int
    name: str
    color: str | None
    icon: str | None
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class Change:
    """``current − previous``; ``percent`` is that change as a share of ``previous`` (whole
    percent, half-up) when ``previous`` is above zero, else ``None``."""

    current: Decimal
    previous: Decimal
    amount: Decimal
    percent: Decimal | None
    kind: str


def change(current: Decimal | None, previous: Decimal | None) -> Change | None:
    """The change from ``previous`` to ``current``; ``None`` when either has no data."""
    if current is None or previous is None:
        return None
    amount = to_money(current - previous)
    percent = None
    if previous > 0:
        percent = (amount * _HUNDRED / previous).quantize(_ONE, ROUND_HALF_UP)
    if current == 0 and previous == 0:
        kind = NONE
    elif previous == 0:
        kind = NEW
    elif current == 0:
        kind = GONE
    elif amount > 0:
        kind = UP
    elif amount < 0:
        kind = DOWN
    else:
        kind = SAME
    return Change(current, previous, amount, percent, kind)


def _ref(row: CategorySpending) -> CategoryRef:
    return CategoryRef(row.category_id, row.name, row.color, row.icon, row.is_hidden)


def history_start(session: Session, basis: str = BASIS_SPEND_DATE) -> dt.date | None:
    """The first day with any transaction counted on ``basis`` (``None`` with no data): months
    and periods before it have no data rather than no spending."""
    return session.scalar(select(func.min(basis_date(basis))).select_from(v_allocations))


def has_transactions(session: Session) -> bool:
    """Whether any transaction exists (linked or imported)."""
    return session.scalar(select(Transaction.id).limit(1)) is not None


# --------------------------------------------------------------------------- dashboard


@dataclass(frozen=True, slots=True)
class CategoryShare:
    """One of the period's top categories: net spending and its share of the period's total."""

    category: CategoryRef
    amount: Decimal
    percent: Decimal | None
    transaction_count: int


@dataclass(frozen=True, slots=True)
class TopCategories:
    """The period's largest categories; ``rest_amount`` is everything else (smaller categories,
    and any category whose refunds exceed its charges), so ``rows + rest = total``."""

    rows: tuple[CategoryShare, ...]
    rest_amount: Decimal
    rest_count: int
    total: Decimal


@dataclass(frozen=True, slots=True)
class Pace:
    """Spending day by day, added up, this period and the previous one (aligned by day number).

    ``current[i]`` is the spending from the period's start through ``days[i]`` (``None`` after
    the last day that has happened); ``previous[i]`` the same for the previous period's i-th day
    (``None`` past its end, or everywhere when it is before the history). ``spent`` is this
    period's spending so far; ``previous_spent`` the previous period's by the same day number
    (its whole spending when it was shorter).
    """

    period: Period
    previous_period: Period
    days: tuple[dt.date, ...]
    current: tuple[Decimal | None, ...]
    previous: tuple[Decimal | None, ...]
    elapsed: int
    spent: Decimal
    previous_spent: Decimal | None
    previous_total: Decimal | None
    #: The history starts inside the previous period, so it holds only part of its spending.
    previous_partial: bool
    #: The previous period's day the comparison runs to (``None`` without a comparison).
    previous_through: dt.date | None

    @property
    def change(self) -> Change | None:
        return change(self.spent, self.previous_spent)


@dataclass(frozen=True, slots=True)
class Upcoming:
    """Recurring charges expected from today through the period's end, soonest first."""

    charges: tuple[RecurringSeries, ...]
    total: Decimal
    monthly_total: Decimal
    #: Active series whose expected date has passed without a charge.
    late: tuple[RecurringSeries, ...] = ()


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Everything the Dashboard shows (INS-1). ``overview`` is the Budgets page's overview of the
    current period, so every budget figure matches that page; ``annual`` is the Budgets page's
    annual section for that period (the year of its end)."""

    today: dt.date
    overview: BudgetOverview
    annual: AnnualOverview
    top_categories: TopCategories
    top_merchants: tuple[MerchantSpending, ...]
    pace: Pace
    upcoming: Upcoming
    held_rows: int
    has_transactions: bool

    @property
    def period(self) -> Period:
        return self.overview.period


def top_categories(
    rows: Sequence[CategorySpending], total: Decimal, *, limit: int
) -> TopCategories:
    """The ``limit`` largest categories with spending (above zero) and the remainder."""
    shown = [r for r in rows if r.amount > 0][:limit]
    listed = ZERO
    out = []
    for row in shown:
        percent = (
            (row.amount * _HUNDRED / total).quantize(_ONE, ROUND_HALF_UP) if total > 0 else None
        )
        out.append(CategoryShare(_ref(row), row.amount, percent, row.transaction_count))
        listed += row.amount
    rest = [r for r in rows if r not in shown and r.amount != 0]
    return TopCategories(tuple(out), to_money(total - listed), len(rest), total)


def _cumulative(days: Iterable[spending.DaySpending]) -> list[Decimal]:
    running = ZERO
    out = []
    for day in days:
        running += day.amount
        out.append(to_money(running))
    return out


def pace(
    session: Session,
    period: Period,
    previous_period: Period,
    today: dt.date,
    *,
    first_day: dt.date | None,
) -> Pace:
    """:class:`Pace` for ``period`` against ``previous_period`` (``first_day``: the history's
    start on the budget basis)."""
    daily = spending.daily_spending(session, period)
    cumulative = _cumulative(daily)
    # Days of the period that have started, today included.
    elapsed = min(max((today - period.start).days + 1, 0), period.days)
    # Through today, or through the last day with spending when a row is dated later.
    last = elapsed
    for index, day in enumerate(daily):
        if day.amount != 0:
            last = max(last, index + 1)
    current = tuple(value if i < last else None for i, value in enumerate(cumulative))
    spent = cumulative[last - 1] if last else ZERO

    has_previous = first_day is not None and first_day <= previous_period.end
    if has_previous:
        previous_cumulative = _cumulative(spending.daily_spending(session, previous_period))
        previous = tuple(
            previous_cumulative[i] if i < len(previous_cumulative) else None
            for i in range(period.days)
        )
        index = min(max(elapsed, 1), len(previous_cumulative))
        previous_spent: Decimal | None = previous_cumulative[index - 1]
        previous_total: Decimal | None = previous_cumulative[-1]
        through: dt.date | None = previous_period.start + dt.timedelta(days=index - 1)
    else:
        previous = tuple(None for _ in range(period.days))
        previous_spent = previous_total = through = None
    return Pace(
        period=period,
        previous_period=previous_period,
        days=tuple(d.day for d in daily),
        current=current,
        previous=previous,
        elapsed=elapsed,
        spent=spent,
        previous_spent=previous_spent,
        previous_total=previous_total,
        previous_partial=has_previous
        and first_day is not None
        and first_day > previous_period.start,
        previous_through=through,
    )


def upcoming(session: Session, period: Period, today: dt.date) -> Upcoming:
    """Active recurring charges expected from today through ``period.end``."""
    found = recurring.detect(session, today=today)
    due = tuple(
        s
        for s in found.active
        if s.next_expected is not None and today <= s.next_expected <= period.end
    )
    due = tuple(sorted(due, key=lambda s: (s.next_expected, s.merchant.casefold())))
    late = tuple(s for s in found.active if s.late)
    return Upcoming(
        charges=due,
        total=to_money(sum((s.typical_amount for s in due), ZERO)),
        monthly_total=found.monthly_total,
        late=late,
    )


def dashboard(session: Session, *, today: dt.date | None = None) -> Dashboard:
    """The Dashboard for the budget period containing ``today`` (see the module docstring)."""
    today = today or clock.today()
    period = budgets.current_period(session, today=today)
    ov = budgets.overview(session, period, today=today)
    *_, annual = budgets.annual_overviews(session, period, today=today)
    first_day = history_start(session, ov.spending.basis)
    return Dashboard(
        today=today,
        overview=ov,
        annual=annual,
        top_categories=top_categories(
            ov.spending.categories, ov.spending.total, limit=TOP_CATEGORIES
        ),
        top_merchants=tuple(spending.period_top_merchants(session, period, limit=TOP_MERCHANTS)),
        pace=pace(session, period, ov.previous_period, today, first_day=first_day),
        upcoming=upcoming(session, period, today),
        held_rows=len(csv_import.list_suspects(session)),
        has_transactions=has_transactions(session),
    )


# --------------------------------------------------------------------------- trends


@dataclass(frozen=True, slots=True)
class TrendMonth:
    """One calendar month of Trends: its total (``None`` before the history) and the change
    from the month before (``None`` when either has no data)."""

    period: Period
    status: str
    total: Decimal | None
    change: Change | None

    @property
    def has_data(self) -> bool:
        return self.status != BEFORE


@dataclass(frozen=True, slots=True)
class CategoryTrend:
    """One category over the months of :class:`Trends` (``amounts[i]`` is ``months[i]``).

    ``category`` is ``None`` for all categories together (:attr:`Trends.totals`).
    ``changes[i]`` is ``months[i]`` against the month before it. ``compared``/``previous``/
    ``change`` are the comparison months'; ``so_far`` is the month in progress, ``so_far_change``
    the same against the same days of the previous month.
    """

    category: CategoryRef | None
    amounts: tuple[Decimal | None, ...]
    counts: tuple[int, ...]
    changes: tuple[Change | None, ...]
    total: Decimal
    average: Decimal | None
    change: Change | None
    so_far: Decimal | None
    so_far_change: Change | None

    @property
    def compared(self) -> Decimal | None:
        return self.change.current if self.change else None

    @property
    def previous(self) -> Decimal | None:
        return self.change.previous if self.change else None


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """A series of the Trends chart: a category, or ``category=None`` for "Other"."""

    category: CategoryRef | None
    label: str
    color: str | None
    amounts: tuple[Decimal | None, ...]


@dataclass(frozen=True, slots=True)
class Trends:
    """Spending per category per calendar month (INS-2); see the module docstring.

    ``months`` oldest first (``span`` of them, ending with ``through``, less any before the
    history: ``trimmed``). ``compare`` is the
    latest month in range that has ended and has data, ``previous`` the month before it (each
    ``None`` when there is no such month). ``current`` is the month in progress when it is in
    range, ``current_through`` the previous month's day it is compared to. ``categories`` holds
    every category with spending in range (largest total first); ``totals`` is the same for all
    categories together (``category`` is ``None``). ``selected`` is the requested category
    (``None``: all categories).
    """

    today: dt.date
    span: int
    through: Period
    months: tuple[TrendMonth, ...]
    compare: Period | None
    previous: Period | None
    current: TrendMonth | None
    current_through: dt.date | None
    categories: tuple[CategoryTrend, ...]
    totals: CategoryTrend
    average: Decimal | None
    complete_months: int
    history_start: dt.date | None
    include_pending: bool
    choices: tuple[Period, ...]
    selected: CategoryTrend | None = None
    selected_ref: CategoryRef | None = None
    #: Months before the history were left out, so fewer than ``span`` months are shown.
    trimmed: bool = False

    @property
    def has_data(self) -> bool:
        """Any spending history at all."""
        return self.history_start is not None

    @property
    def range_has_data(self) -> bool:
        """Any month shown has data (the range isn't wholly before the history)."""
        return any(m.has_data for m in self.months)

    @property
    def changed(self) -> tuple[CategoryTrend, ...]:
        """Categories with spending in the comparison month or the month before, largest
        comparison-month spending first."""
        rows = [
            c
            for c in self.categories
            if c.change is not None and (c.change.current != 0 or c.change.previous != 0)
        ]
        return tuple(
            sorted(
                rows,
                key=lambda c: (-c.change.current, -c.change.previous, c.category.name.casefold()),
            )
        )

    @property
    def biggest_increase(self) -> CategoryTrend | None:
        """The category whose spending rose the most into the comparison month."""
        ups = [c for c in self.changed if c.change.amount > 0]
        return max(ups, key=lambda c: (c.change.amount, c.category.name)) if ups else None

    @property
    def biggest_decrease(self) -> CategoryTrend | None:
        downs = [c for c in self.changed if c.change.amount < 0]
        return min(downs, key=lambda c: (c.change.amount, c.category.name)) if downs else None

    def chart_series(self, limit: int = CHART_SERIES) -> tuple[ChartSeries, ...]:
        """The stacked chart's series: the ``limit`` categories with the largest total, each in
        its own colour, then one "Other" series for the rest (when there is any)."""
        source = [self.selected] if self.selected is not None else list(self.categories)
        ranked = sorted(source, key=lambda c: (-c.total, c.category.name.casefold()))
        if len(ranked) > limit:
            ranked, rest = ranked[: limit - 1], ranked[limit - 1 :]
        else:
            rest = []
        out = [
            ChartSeries(c.category, c.category.name, c.category.color, c.amounts) for c in ranked
        ]
        if rest:
            amounts = []
            for i, month in enumerate(self.months):
                if not month.has_data:
                    amounts.append(None)
                else:
                    amounts.append(to_money(sum((c.amounts[i] or ZERO for c in rest), ZERO)))
            label = f"{len(rest)} more categories"
            out.append(ChartSeries(None, label, "other", tuple(amounts)))
        return tuple(out)


def month_param(text: str | None) -> dt.date | None:
    """A ``YYYY-MM`` (or ``YYYY-MM-DD``) query value as the month's first day; ``None`` when
    it isn't one."""
    if not text:
        return None
    value = text.strip()
    try:
        day = dt.date.fromisoformat(f"{value}-01" if len(value) == 7 else value)
    except ValueError:
        return None
    if not 2000 <= day.year < 3000:  # (the app holds nothing older; keeps the picker short)
        return None
    return day.replace(day=1)


def _status(month: Period, today: dt.date, first_day: dt.date | None) -> str:
    if first_day is None or month.end < first_day:
        return BEFORE
    if month.contains(today):
        return CURRENT
    if month.start < first_day:
        return FIRST
    return COMPLETE


def _average(values: Iterable[Decimal]) -> Decimal | None:
    items = list(values)
    if not items:
        return None
    return to_money(sum(items, ZERO) / len(items))


def trends(
    session: Session,
    *,
    through: dt.date | None = None,
    span: int = DEFAULT_SPAN,
    category_id: int | None = None,
    today: dt.date | None = None,
) -> Trends:
    """Trends for ``span`` calendar months ending with the month of ``through`` (default: this
    month; a later month is pulled back to this month). ``category_id`` selects one category
    (``Trends.selected``; ``None`` when it has no spending in range or isn't a spending
    category). See the module docstring.
    """
    today = today or clock.today()
    if span not in SPANS:
        span = DEFAULT_SPAN
    this_month = periods.month_of(today)
    last = periods.month_of(min(through or today, today))
    first_year, first_month = periods.add_months(last.start.year, last.start.month, -span)
    # One month before the range, for the first month's change.
    all_months = periods.months_between(dt.date(first_year, first_month, 1), last.end)
    include_pending = get_app_settings(session).include_pending_in_budgets
    data = spending.monthly_spending(
        session, all_months[0].start, all_months[-1].end, include_pending=include_pending
    )
    first_day = history_start(session)
    statuses = [_status(m.month, today, first_day) for m in data]

    def amount(month_index: int, category_id: int | None) -> Decimal | None:
        if statuses[month_index] == BEFORE:
            return None
        month = data[month_index]
        return month.total if category_id is None else month.amount_for(category_id)

    # Months before the history have no data: the range starts with the history's first month
    # (``Trends.trimmed`` says so), unless no month in it has data.
    start = next((i for i in range(1, len(data)) if statuses[i] != BEFORE), 1)
    shown = range(start, len(data))
    months = tuple(
        TrendMonth(
            data[i].month,
            statuses[i],
            amount(i, None),
            change(amount(i, None), amount(i - 1, None)),
        )
        for i in shown
    )
    compare_index = next((i for i in reversed(shown) if statuses[i] in (COMPLETE, FIRST)), None)
    current_index = next((i for i in shown if statuses[i] == CURRENT), None)
    complete = [i for i in shown if statuses[i] == COMPLETE]

    # The month in progress is compared with the same days of the month before.
    current_through = None
    previous_to_date: dict[int, Decimal] | None = None
    previous_to_date_total: Decimal | None = None
    if current_index is not None and statuses[current_index - 1] != BEFORE:
        previous_month = data[current_index - 1].month
        current_through = periods.clamp_day(
            previous_month.start.year, previous_month.start.month, today.day
        )
        rows = spending.spending_by_category(
            session,
            previous_month.start,
            current_through,
            basis=BASIS_SPEND_DATE,
            include_pending=include_pending,
        )
        previous_to_date = {r.category_id: r.amount for r in rows}
        previous_to_date_total = to_money(sum((r.amount for r in rows), ZERO))

    refs: dict[int, CategoryRef] = {}
    totals_by_id: dict[int, Decimal] = {}
    for i in shown:
        for row in data[i].categories:
            refs.setdefault(row.category_id, _ref(row))
            totals_by_id[row.category_id] = totals_by_id.get(row.category_id, ZERO) + row.amount

    def trend(category: CategoryRef | None) -> CategoryTrend:
        cid = category.id if category is not None else None
        amounts = tuple(amount(i, cid) for i in shown)
        counts = tuple(
            sum(r.transaction_count for r in data[i].categories)
            if cid is None
            else next((r.transaction_count for r in data[i].categories if r.category_id == cid), 0)
            for i in shown
        )
        changes = tuple(change(amount(i, cid), amount(i - 1, cid)) for i in shown)
        so_far = so_far_change = None
        if current_index is not None:
            so_far = amount(current_index, cid)
            if previous_to_date is not None:
                before = previous_to_date_total if cid is None else previous_to_date.get(cid, ZERO)
                so_far_change = change(so_far, before)
        return CategoryTrend(
            category=category,
            amounts=amounts,
            counts=counts,
            changes=changes,
            total=to_money(sum((a or ZERO for a in amounts), ZERO)),
            average=_average(amount(i, cid) or ZERO for i in complete),
            change=(
                change(amount(compare_index, cid), amount(compare_index - 1, cid))
                if compare_index is not None
                else None
            ),
            so_far=so_far,
            so_far_change=so_far_change,
        )

    ordered = sorted(refs.values(), key=lambda r: (-totals_by_id[r.id], r.name.casefold()))
    categories = tuple(trend(ref) for ref in ordered)
    totals = trend(None)

    selected = selected_ref = None
    if category_id is not None:
        selected = next((c for c in categories if c.category.id == category_id), None)
        if selected is None:
            row = session.get(Category, category_id)
            if row is not None and row.kind == "expense":
                selected_ref = CategoryRef(row.id, row.name, row.color, row.icon, row.is_hidden)
                selected = trend(selected_ref)
        else:
            selected_ref = selected.category

    return Trends(
        today=today,
        span=span,
        through=last,
        months=months,
        compare=data[compare_index].month if compare_index is not None else None,
        previous=data[compare_index - 1].month if compare_index is not None else None,
        current=months[current_index - start] if current_index is not None else None,
        current_through=current_through,
        categories=categories,
        totals=totals,
        average=totals.average,
        complete_months=len(complete),
        history_start=first_day,
        include_pending=include_pending,
        choices=_choices(first_day, this_month, last),
        selected=selected,
        selected_ref=selected_ref,
        trimmed=start > 1,
    )


def _choices(first_day: dt.date | None, this_month: Period, shown: Period) -> tuple[Period, ...]:
    """The months Trends can end with, newest first: this month back to the history's first
    month (at least the last 12 months, and at least back to the month shown)."""
    floor_year, floor_month = periods.add_months(
        this_month.start.year, this_month.start.month, -(DEFAULT_SPAN - 1)
    )
    floor = dt.date(floor_year, floor_month, 1)
    if first_day is not None and first_day < floor:
        floor = first_day
    floor = min(floor, shown.start)
    return tuple(reversed(periods.months_between(floor, this_month.start)))
