"""Budgets (BUD-1..5): monthly and annual budgets, rollover and the overall target.

The one place that says how much budget there is and how much of it is used: the Budgets page,
the dashboard (DASH) and the budget alerts (ALR) all call it. Spending comes only from
:func:`budgetapp.services.spending.period_spending` (ARCHITECTURE §6), so the mode's date basis
and the pending setting apply to every number here.

**Periods (BUD-2).** :func:`current_period` / :func:`period_for` follow
``app_settings.budget_period_mode``: calendar months, or the statement cycles of the card named by
``app_settings.budget_cycle_account_id`` (closing dates from ``statements``, extended with the
card's ``statement_closing_day``). Statement mode without a card or any known closing date falls
back to calendar months and :attr:`BudgetOverview.notice` says so. :func:`previous_period` /
:func:`next_period` step through them.

**Versioned amounts (ARCHITECTURE §5).** A ``budgets`` row holds an amount from its
``effective_from`` on; the amount for a period is the row with the latest
``effective_from <= period.end`` (an amount of ``NULL`` = no budget). An edit (:func:`set_budget`,
:func:`set_rollover`) updates the row that starts inside the edited period, else inserts one at the
period's start, so earlier periods keep their amounts. A category has at most one active type:
setting a monthly amount writes an amount-``NULL`` annual row from the same date, and vice versa.
Should both still be set for a period (for example after a category merge), the more recent row
wins (monthly on a tie).

**Rollover (BUD-4).** ``available = budget + carry_in``. With rollover on, ``carry_in`` is the
previous period's ``available − spent`` (negative after an overspend), chained back to the first
period of an unbroken run of rollover periods, whose carry-in is 0. It is computed on the fly, at
most :data:`MAX_ROLLOVER_PERIODS` periods back.

**Overall target (BUD-3) and annual budgets (BUD-5).** The overall target (the ``overall`` monthly
row) is compared with spending in every category without an active annual budget. Without a
target, the period's budget is the sum of the monthly category budgets (their available amounts)
against spending in those categories; unbudgeted spending is reported separately. Annual budgets
cover the calendar year, with year-to-date spending on the same date basis, and never count in
monthly totals.

**Status.** ``ok`` below 80 %, ``warn`` from 80 % up to 100 %, ``over`` once spending passes the
available amount (``remaining < 0``), ``none`` without a budget. ``percent_used`` is
``spent / available × 100`` (``None`` when ``available <= 0``), rounded to 0.01 toward the status
so it never shows a threshold the status hasn't reached. ``pace_percent`` is the share of the
period elapsed, counting today, for periods in progress (``None`` for past and future ones).

**Public API** (the Budgets page, DASH and ALR; every function takes the session first, reads
"today" from :mod:`budgetapp.clock` unless ``today`` is given, and never commits):

* :func:`current_period`, :func:`period_for`, :func:`previous_period`, :func:`next_period` and
  :func:`period_settings`: the budget periods.
* :func:`overview` → :class:`BudgetOverview` for one period: ``lines`` (monthly budgets; categories
  without one are in ``unbudgeted``, so a line's status is never ``none``), ``overall``,
  ``totals``, ``notice``, ``days_left``, ``pace_percent``. Budget alerts read ``lines`` (and
  ``overall``) of the current period.
* :func:`annual_overview` → :class:`AnnualOverview` for a calendar year.
* :func:`suggestions` / :func:`suggest`: the "Suggest" amounts.
* :func:`set_budget` / :func:`set_rollover` → :class:`BudgetChange`; the caller commits.
  :func:`parse_amount` / :func:`check_amount` validate amounts; :class:`BudgetError` messages are
  written for the user.
* :func:`usage` and :func:`pace_percent`: the status and pace rules, for widgets that show a
  budget-like meter of their own.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from types import MappingProxyType

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.models import (
    Account,
    Budget,
    Category,
    Statement,
    get_app_settings,
    v_allocations,
)
from budgetapp.money import CENT, MAX_AMOUNT, ZERO, is_cents, parse_money_input, to_money
from budgetapp.periods import Period
from budgetapp.services.spending import PeriodSpending, basis_date, basis_for_mode, period_spending

MONTHLY = "monthly"
ANNUAL = "annual"
KINDS = (MONTHLY, ANNUAL)
CALENDAR = "calendar"
STATEMENT = "statement"

OK = "ok"
WARN = "warn"
OVER = "over"
NONE = "none"

#: Rollover chains and suggestions look at most this many periods back.
MAX_ROLLOVER_PERIODS = 60
#: "Suggest" averages this many complete periods.
SUGGEST_PERIODS = 3
#: Suggestions are rounded up to a multiple of this many dollars.
SUGGEST_STEP = Decimal(5)
WARN_PERCENT = Decimal(80)

_HUNDRED = Decimal(100)
_INFINITY = Decimal("Infinity")

NOTICE_NO_CARD = (
    "Statement-cycle budgets need a card to follow, and none is chosen. Budgets use calendar "
    "months until you pick the card in Settings."
)


def notice_no_closing_date(card: str) -> str:
    """The fallback notice when the budget card has no known statement closing date."""
    return (
        f"No statement closing date is known for {card} yet. Budgets use calendar months until "
        "the first statement arrives, or until you enter the closing day in Settings."
    )


class BudgetError(ValueError):
    """A budget change that can't be made; ``str(err)`` is written for the user."""


# --------------------------------------------------------------------------- result types


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """The category fields budget lists show (``ui.category_badge`` reads them)."""

    id: int
    name: str
    color: str | None
    icon: str | None
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class BudgetRow:
    """One ``budgets`` row: an amount (``None`` = no budget) from ``effective_from`` on."""

    id: int
    scope: str
    category_id: int | None
    period: str
    amount: Decimal | None
    rollover: bool
    effective_from: dt.date


@dataclass(frozen=True, slots=True)
class BudgetChange:
    """What :func:`set_budget` / :func:`set_rollover` did.

    ``row`` is the budget now in force for the edited period (``None`` = no budget), ``changed``
    is False when nothing needed changing, and ``ended`` is the other type's amount-``NULL`` row
    written so the category keeps a single active type.
    """

    row: BudgetRow | None
    changed: bool
    ended: BudgetRow | None = None


@dataclass(frozen=True, slots=True)
class BudgetLine:
    """A monthly category budget in one period (BUD-1, BUD-4)."""

    category: CategoryRef
    budget: Decimal
    carry_in: Decimal
    available: Decimal
    spent: Decimal
    remaining: Decimal
    percent_used: Decimal | None
    status: str
    pace_percent: Decimal | None
    rollover: bool
    effective_from: dt.date


@dataclass(frozen=True, slots=True)
class UnbudgetedLine:
    """A category with spending in the period but no budget of either type."""

    category: CategoryRef
    spent: Decimal


@dataclass(frozen=True, slots=True)
class OverallLine:
    """The period as a whole (BUD-3).

    With an overall target, ``budget`` is the target and ``spent`` is spending in every category
    without an active annual budget. Without one, ``budget`` is the sum of the monthly category
    budgets (available amounts, hidden categories included) and ``spent`` is spending in those
    categories. ``budget`` is ``None`` when nothing is budgeted.
    """

    target: Decimal | None
    budget: Decimal | None
    spent: Decimal
    remaining: Decimal | None
    percent_used: Decimal | None
    status: str
    pace_percent: Decimal | None

    @property
    def has_target(self) -> bool:
        return self.target is not None


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    """Where the period's spending went; ``spent = budgeted_spent + unbudgeted_spent +
    annual_spent``. Hidden categories count everywhere (``hidden_spent`` is their share)."""

    spent: Decimal
    budgeted: Decimal
    budgeted_spent: Decimal
    remaining: Decimal
    unbudgeted_spent: Decimal
    annual_spent: Decimal
    hidden_spent: Decimal


@dataclass(frozen=True, slots=True)
class PeriodSettings:
    """How budget periods are cut (BUD-2), from ``app_settings`` and the budget card.

    ``mode`` is the setting; ``effective_mode`` what is used: ``calendar`` when statement mode has
    no card or no known closing date, with ``notice`` saying so. ``closings``, ``closing_day`` and
    ``first_period_start`` describe the card's statement cycles (ARCHITECTURE §6).
    """

    mode: str
    effective_mode: str
    account_id: int | None = None
    account_name: str | None = None
    closings: tuple[dt.date, ...] = ()
    closing_day: int | None = None
    first_period_start: dt.date | None = None
    notice: str | None = None

    def period_for(self, day: dt.date) -> Period:
        """The budget period containing ``day``."""
        if self.effective_mode == CALENDAR:
            return periods.month_of(day)
        return periods.budget_period_for(
            day,
            mode=STATEMENT,
            closings=self.closings,
            closing_day=self.closing_day,
            first_period_start=self.first_period_start,
        )

    def previous(self, period: Period) -> Period:
        """The budget period before ``period``."""
        if period.kind == periods.STATEMENT:
            return periods.previous_period(period, **self._cycle_inputs())
        return self.period_for(period.start - dt.timedelta(days=1))

    def next(self, period: Period) -> Period:
        """The budget period after ``period``."""
        if period.kind == periods.STATEMENT:
            return periods.next_period(period, **self._cycle_inputs())
        return self.period_for(period.end + dt.timedelta(days=1))

    def _cycle_inputs(self) -> dict:
        return {
            "closings": self.closings,
            "closing_day": self.closing_day,
            "first_period_start": self.first_period_start,
        }


@dataclass(frozen=True, slots=True)
class BudgetOverview:
    """Everything the Budgets page and the dashboard show for one period (BUD-1..4).

    ``lines``: monthly category budgets, hidden categories left out, sorted by percent used (most
    used first; over budget without a percentage counts as most), then name. ``unbudgeted``:
    visible categories with spending but no budget, largest first. ``overall`` and ``totals``
    include hidden categories. Annual budgets are reported by :func:`annual_overview` instead;
    ``annual_category_ids`` lists the categories whose spending they cover in this period.
    ``spending`` is the period's :func:`period_spending` result (every expense category).
    """

    period: Period
    mode: str
    days_left: int
    notice: str | None
    overall: OverallLine
    lines: tuple[BudgetLine, ...]
    unbudgeted: tuple[UnbudgetedLine, ...]
    totals: BudgetTotals
    today: dt.date
    pace_percent: Decimal | None
    previous_period: Period
    next_period: Period
    annual_category_ids: frozenset[int]
    settings: PeriodSettings
    spending: PeriodSpending

    @property
    def is_current(self) -> bool:
        return self.period.contains(self.today)

    @property
    def is_past(self) -> bool:
        return self.period.end < self.today

    @property
    def is_future(self) -> bool:
        return self.period.start > self.today

    def line_for(self, category_id: int) -> BudgetLine | None:
        for line in self.lines:
            if line.category.id == category_id:
                return line
        return None


@dataclass(frozen=True, slots=True)
class AnnualLine:
    """An annual category budget (BUD-5): spending from Jan 1 through ``as_of``.

    ``pace_amount`` is ``budget × elapsed days / days in the year`` (where spending "should" be by
    now) and ``pace_percent`` the same as a share of the year; both ``None`` outside the year
    in progress.
    """

    category: CategoryRef
    budget: Decimal
    spent: Decimal
    remaining: Decimal
    percent_used: Decimal | None
    status: str
    pace_amount: Decimal | None
    pace_percent: Decimal | None
    effective_from: dt.date


@dataclass(frozen=True, slots=True)
class AnnualOverview:
    """Annual budgets for a calendar year, in force on ``as_of`` (BUD-5)."""

    year: int
    period: Period
    as_of: dt.date
    days_left: int
    pace_percent: Decimal | None
    lines: tuple[AnnualLine, ...]
    budget_total: Decimal
    spent_total: Decimal
    remaining_total: Decimal


@dataclass(frozen=True, slots=True)
class Suggestions:
    """Average spending over recent complete periods, rounded up to $5 ("Suggest").

    ``periods`` are the periods averaged, newest first (empty: no history yet).
    """

    periods: tuple[Period, ...]
    by_category: Mapping[int, Decimal] = field(default_factory=lambda: MappingProxyType({}))
    overall: Decimal | None = None

    def for_category(self, category_id: int) -> Decimal | None:
        """The suggestion for a category: ``None`` without history, ``0.00`` without spending."""
        if not self.periods:
            return None
        return self.by_category.get(category_id, ZERO)


# --------------------------------------------------------------------------- periods


def period_settings(session: Session) -> PeriodSettings:
    """How budget periods are cut right now (see :class:`PeriodSettings`)."""
    settings = get_app_settings(session)
    mode = settings.budget_period_mode
    if mode != STATEMENT:
        return PeriodSettings(mode, CALENDAR)
    account = (
        session.get(Account, settings.budget_cycle_account_id)
        if settings.budget_cycle_account_id is not None
        else None
    )
    if account is None:
        return PeriodSettings(mode, CALENDAR, notice=NOTICE_NO_CARD)
    rows = session.execute(
        select(Statement.closing_date, Statement.period_start_override)
        .where(Statement.account_id == account.id)
        .order_by(Statement.closing_date)
    ).all()
    closings = tuple(row.closing_date for row in rows)
    closing_day = account.statement_closing_day
    if not closings and closing_day is None:
        return PeriodSettings(
            mode,
            CALENDAR,
            account.id,
            account.display_name,
            notice=notice_no_closing_date(account.display_name),
        )
    return PeriodSettings(
        mode,
        STATEMENT,
        account.id,
        account.display_name,
        closings,
        closing_day,
        rows[0].period_start_override if rows else None,
    )


def period_for(session: Session, day: dt.date) -> Period:
    """The budget period containing ``day`` (calendar month or statement cycle)."""
    return period_settings(session).period_for(day)


def current_period(session: Session, *, today: dt.date | None = None) -> Period:
    """The budget period containing today."""
    return period_for(session, today or clock.today())


def previous_period(session: Session, period: Period) -> Period:
    """The budget period before ``period``."""
    return period_settings(session).previous(period)


def next_period(session: Session, period: Period) -> Period:
    """The budget period after ``period``."""
    return period_settings(session).next(period)


# --------------------------------------------------------------------------- math helpers


def usage(available: Decimal, spent: Decimal) -> tuple[Decimal | None, str]:
    """``(percent_used, status)`` for an available amount and the spending against it."""
    remaining = available - spent
    if remaining < 0:
        status = OVER
    elif available > 0 and spent * _HUNDRED >= available * WARN_PERCENT:
        status = WARN
    else:
        status = OK
    if available <= 0:
        return None, status
    exact = spent * _HUNDRED / available
    rounding = ROUND_CEILING if status == OVER else ROUND_FLOOR
    return exact.quantize(CENT, rounding=rounding), status


def pace_percent(period: Period, today: dt.date) -> Decimal | None:
    """Share of ``period`` elapsed, counting today (``None`` unless today is in it)."""
    if not period.contains(today):
        return None
    elapsed = (today - period.start).days + 1
    return (Decimal(elapsed) * _HUNDRED / Decimal(period.days)).quantize(CENT, ROUND_HALF_UP)


def round_up_suggestion(mean: Decimal) -> Decimal:
    """``mean`` rounded up to the next multiple of $5 (``0.00`` for nothing or credits)."""
    if mean <= 0:
        return ZERO
    steps = (mean / SUGGEST_STEP).to_integral_value(rounding=ROUND_CEILING)
    return to_money(steps * SUGGEST_STEP)


def parse_amount(text: str) -> Decimal | None:
    """A budget amount typed by the user: empty means "no budget" (``None``).

    Raises :class:`BudgetError` with a message for the user (not a number, negative, more than
    two decimal places, too large).
    """
    if not text or not text.strip():
        return None
    try:
        amount = parse_money_input(text)
    except ValueError as err:
        raise BudgetError(str(err)) from None
    return check_amount(amount)


def check_amount(amount: Decimal | int | None) -> Decimal | None:
    """Validate a budget amount: ``None`` or a whole number of cents ≥ 0."""
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, Decimal | int):
        raise TypeError(f"a budget amount must be a Decimal, not {type(amount).__name__}")
    value = Decimal(amount)
    if not value.is_finite():
        raise BudgetError("Enter a number like 250 or 1,250.50.")
    if value < 0:
        raise BudgetError("A budget can't be negative. Leave it empty for no budget.")
    if abs(value) > MAX_AMOUNT:
        raise BudgetError("Amounts must be under $1,000,000,000,000.")
    if not is_cents(value):
        raise BudgetError("Amounts can have at most two decimal places.")
    return to_money(value)


# --------------------------------------------------------------------------- versions


def _row(budget: Budget) -> BudgetRow:
    return BudgetRow(
        id=budget.id,
        scope=budget.scope,
        category_id=budget.category_id,
        period=budget.period,
        amount=budget.amount,
        rollover=budget.rollover,
        effective_from=budget.effective_from,
    )


class _Versions:
    """Budget rows by ``(category_id, period)``, oldest first, for "in force on" lookups."""

    def __init__(self, rows: Iterable[BudgetRow]) -> None:
        self._rows: dict[tuple[int | None, str], list[BudgetRow]] = {}
        for row in sorted(rows, key=lambda r: (r.effective_from, r.id)):
            self._rows.setdefault((row.category_id, row.period), []).append(row)
        self._dates = {key: [r.effective_from for r in rows] for key, rows in self._rows.items()}

    def in_force(self, category_id: int | None, kind: str, on: dt.date) -> BudgetRow | None:
        """The row of this type with the latest ``effective_from <= on`` (amount may be None)."""
        dates = self._dates.get((category_id, kind))
        if not dates:
            return None
        index = bisect_right(dates, on)
        return self._rows[(category_id, kind)][index - 1] if index else None

    def inside(
        self, category_id: int | None, kind: str, start: dt.date, end: dt.date
    ) -> BudgetRow | None:
        """The latest row of this type starting within ``[start, end]``."""
        row = self.in_force(category_id, kind, end)
        return row if row is not None and row.effective_from >= start else None

    def active(self, category_id: int | None, on: dt.date) -> BudgetRow | None:
        """The budget that applies on ``on``: the monthly or annual row with an amount (the more
        recent one if both are set; monthly on a tie). The overall target is monthly only."""
        monthly = self.in_force(category_id, MONTHLY, on)
        monthly = monthly if monthly is not None and monthly.amount is not None else None
        if category_id is None:
            return monthly
        annual = self.in_force(category_id, ANNUAL, on)
        annual = annual if annual is not None and annual.amount is not None else None
        if monthly is not None and annual is not None:
            return annual if annual.effective_from > monthly.effective_from else monthly
        return monthly or annual


def budget_rows(session: Session) -> list[BudgetRow]:
    """Every budget row, oldest first (the table is small: a few rows per category)."""
    query = select(Budget).order_by(Budget.effective_from, Budget.id)
    return [_row(b) for b in session.scalars(query)]


def _load_versions(session: Session) -> _Versions:
    return _Versions(budget_rows(session))


def _expense_categories(session: Session) -> dict[int, CategoryRef]:
    rows = session.scalars(
        select(Category).where(Category.kind == "expense").order_by(func.lower(Category.name))
    )
    return {
        c.id: CategoryRef(id=c.id, name=c.name, color=c.color, icon=c.icon, is_hidden=c.is_hidden)
        for c in rows
    }


class _Context:
    """One read of settings, budget rows and categories, with spending cached per period."""

    def __init__(self, session: Session, settings: PeriodSettings | None = None) -> None:
        self.session = session
        self.settings = settings or period_settings(session)
        self.versions = _load_versions(session)
        self.categories = _expense_categories(session)
        self._spending: dict[tuple[dt.date, dt.date], PeriodSpending] = {}
        self._history: dt.date | None = None
        self._history_loaded = False

    def spending(self, period: Period) -> PeriodSpending:
        key = (period.start, period.end)
        if key not in self._spending:
            self._spending[key] = period_spending(self.session, period)
        return self._spending[key]

    def history_start(self) -> dt.date | None:
        """The earliest date with any allocation, on the budget mode's date basis."""
        if not self._history_loaded:
            mode = get_app_settings(self.session).budget_period_mode
            self._history = self.session.scalar(
                select(func.min(basis_date(basis_for_mode(mode)))).select_from(v_allocations)
            )
            self._history_loaded = True
        return self._history

    def spent(self, period: Period, category_id: int) -> Decimal:
        history = self.history_start()
        if history is None or period.end < history:
            return ZERO
        return self.spending(period).amount_for(category_id)

    def carry_in(self, category_id: int, period: Period, row: BudgetRow) -> Decimal:
        """BUD-4: what rolls into ``period`` (0 without rollover or in a chain's first period)."""
        if not row.rollover:
            return ZERO
        chain: list[tuple[Period, Decimal]] = []
        earlier = period
        for _ in range(MAX_ROLLOVER_PERIODS):
            earlier = self.settings.previous(earlier)
            previous = self.versions.active(category_id, earlier.end)
            if previous is None or previous.period != MONTHLY or not previous.rollover:
                break
            assert previous.amount is not None  # active rows always have one
            chain.append((earlier, previous.amount))
        carry = ZERO
        for earlier, amount in reversed(chain):
            carry = amount + carry - self.spent(earlier, category_id)
        return carry

    def annual_ids(self, on: dt.date) -> frozenset[int]:
        """Categories whose active budget on ``on`` is annual."""
        return frozenset(
            cid
            for cid in self.categories
            if (row := self.versions.active(cid, on)) is not None and row.period == ANNUAL
        )


# --------------------------------------------------------------------------- overview


def _sort_key(line: BudgetLine | AnnualLine) -> tuple[Decimal, str]:
    """Most used first (over budget without a percentage counts as most), then by name."""
    if line.percent_used is not None:
        used = line.percent_used
    else:
        used = _INFINITY if line.status == OVER else ZERO
    return (-used, line.category.name.casefold())


def overview(session: Session, period: Period, *, today: dt.date | None = None) -> BudgetOverview:
    """Budgets, spending and status for ``period`` (see :class:`BudgetOverview`)."""
    today = today or clock.today()
    ctx = _Context(session)
    spending = ctx.spending(period)
    pace = pace_percent(period, today)
    on = period.end

    lines: list[BudgetLine] = []
    unbudgeted: list[UnbudgetedLine] = []
    annual_ids: set[int] = set()
    budgeted = budgeted_spent = unbudgeted_spent = annual_spent = hidden_spent = ZERO
    has_monthly = False
    for category in ctx.categories.values():
        spent = spending.amount_for(category.id)
        if category.is_hidden:
            hidden_spent += spent
        row = ctx.versions.active(category.id, on)
        if row is None:
            unbudgeted_spent += spent
            if spent != 0 and not category.is_hidden:
                unbudgeted.append(UnbudgetedLine(category, spent))
            continue
        if row.period == ANNUAL:
            annual_ids.add(category.id)
            annual_spent += spent
            continue
        assert row.amount is not None
        carry = ctx.carry_in(category.id, period, row)
        available = row.amount + carry
        percent, status = usage(available, spent)
        has_monthly = True
        budgeted += available
        budgeted_spent += spent
        if category.is_hidden:
            continue
        lines.append(
            BudgetLine(
                category=category,
                budget=row.amount,
                carry_in=carry,
                available=available,
                spent=spent,
                remaining=available - spent,
                percent_used=percent,
                status=status,
                pace_percent=pace,
                rollover=row.rollover,
                effective_from=row.effective_from,
            )
        )
    lines.sort(key=_sort_key)
    unbudgeted.sort(key=lambda u: (-u.spent, u.category.name.casefold()))

    target_row = ctx.versions.active(None, on)
    target = target_row.amount if target_row is not None else None
    if target is not None:
        overall_budget: Decimal | None = target
        overall_spent = spending.total - annual_spent
    else:
        overall_budget = budgeted if has_monthly else None
        overall_spent = budgeted_spent
    if overall_budget is None:
        overall = OverallLine(None, None, overall_spent, None, None, NONE, pace)
    else:
        percent, status = usage(overall_budget, overall_spent)
        overall = OverallLine(
            target,
            overall_budget,
            overall_spent,
            overall_budget - overall_spent,
            percent,
            status,
            pace,
        )

    return BudgetOverview(
        period=period,
        mode=ctx.settings.effective_mode,
        days_left=periods.days_left(period, today),
        notice=ctx.settings.notice,
        overall=overall,
        lines=tuple(lines),
        unbudgeted=tuple(unbudgeted),
        totals=BudgetTotals(
            spent=spending.total,
            budgeted=budgeted,
            budgeted_spent=budgeted_spent,
            remaining=budgeted - budgeted_spent,
            unbudgeted_spent=unbudgeted_spent,
            annual_spent=annual_spent,
            hidden_spent=hidden_spent,
        ),
        today=today,
        pace_percent=pace,
        previous_period=ctx.settings.previous(period),
        next_period=ctx.settings.next(period),
        annual_category_ids=frozenset(annual_ids),
        settings=ctx.settings,
        spending=spending,
    )


def annual_overview(
    session: Session,
    year: int,
    *,
    today: dt.date | None = None,
    as_of: dt.date | None = None,
) -> AnnualOverview:
    """Annual budgets for calendar ``year`` (BUD-5), as they stand on ``as_of``.

    The annual rows in force on ``as_of`` apply, against spending from Jan 1 through ``as_of``
    (year to date, same date basis and pending setting as :func:`period_spending`). ``as_of``
    defaults to the end of the current budget period when that falls in ``year`` (so: year to
    date), else Dec 31; it is clamped into the year. The Budgets page passes the end of the period
    it shows, so its annual section matches that period. Hidden categories are left out.
    """
    today = today or clock.today()
    year_period = periods.year_period(year)
    ctx = _Context(session)
    if as_of is None:
        as_of = (
            ctx.settings.period_for(today).end if year_period.contains(today) else year_period.end
        )
    as_of = min(max(as_of, year_period.start), year_period.end)
    to_date = Period(year_period.start, as_of, periods.CUSTOM)
    spending = ctx.spending(to_date)

    pace_day = min(today, as_of)
    in_progress = year_period.start <= pace_day and today <= year_period.end
    elapsed = (pace_day - year_period.start).days + 1 if in_progress else None
    days = Decimal(year_period.days)

    year_pace = (
        None if elapsed is None else (elapsed * _HUNDRED / days).quantize(CENT, ROUND_HALF_UP)
    )
    lines: list[AnnualLine] = []
    for category in ctx.categories.values():
        row = ctx.versions.active(category.id, as_of)
        if category.is_hidden or row is None or row.period != ANNUAL:
            continue
        assert row.amount is not None
        spent = spending.amount_for(category.id)
        percent, status = usage(row.amount, spent)
        lines.append(
            AnnualLine(
                category=category,
                budget=row.amount,
                spent=spent,
                remaining=row.amount - spent,
                percent_used=percent,
                status=status,
                pace_amount=None if elapsed is None else to_money(row.amount * elapsed / days),
                pace_percent=year_pace,
                effective_from=row.effective_from,
            )
        )
    lines.sort(key=_sort_key)
    budget_total = sum((a.budget for a in lines), ZERO)
    spent_total = sum((a.spent for a in lines), ZERO)
    return AnnualOverview(
        year=year,
        period=year_period,
        as_of=as_of,
        days_left=periods.days_left(year_period, today),
        pace_percent=year_pace,
        lines=tuple(lines),
        budget_total=budget_total,
        spent_total=spent_total,
        remaining_total=budget_total - spent_total,
    )


# --------------------------------------------------------------------------- suggestions


def suggestions(
    session: Session,
    *,
    periods: int = SUGGEST_PERIODS,
    today: dt.date | None = None,
    before: Period | None = None,
) -> Suggestions:
    """Mean spending per category (and for the overall target) over the last ``periods``
    COMPLETE budget periods before ``before`` (default: the current period), rounded up to $5.

    Fewer periods are averaged when the history is shorter (periods that end before the first
    transaction don't count; a period with no spending in a category counts as 0). No complete
    period with history → no suggestions. The overall suggestion leaves out categories whose
    budget in ``before`` is annual, like the overall target does.
    """
    today = today or clock.today()
    ctx = _Context(session)
    anchor = before or ctx.settings.period_for(today)
    history = ctx.history_start()
    if history is None or periods < 1:
        return Suggestions(())
    chosen: list[Period] = []
    earlier = anchor
    for _ in range(MAX_ROLLOVER_PERIODS):
        if len(chosen) >= periods:
            break
        earlier = ctx.settings.previous(earlier)
        if earlier.end < history:
            break
        if earlier.end < today:
            chosen.append(earlier)
    if not chosen:
        return Suggestions(())
    annual = ctx.annual_ids(anchor.end)
    totals: dict[int, Decimal] = {}
    overall = ZERO
    for earlier in chosen:
        spending = ctx.spending(earlier)
        for row in spending.categories:
            totals[row.category_id] = totals.get(row.category_id, ZERO) + row.amount
            if row.category_id not in annual:
                overall += row.amount
    count = Decimal(len(chosen))
    by_category = {cid: round_up_suggestion(total / count) for cid, total in totals.items()}
    return Suggestions(
        tuple(chosen), MappingProxyType(by_category), round_up_suggestion(overall / count)
    )


def suggest(
    session: Session,
    category_id: int | None,
    *,
    periods: int = SUGGEST_PERIODS,
    today: dt.date | None = None,
    before: Period | None = None,
) -> Decimal | None:
    """The "Suggest" amount for a category (``None``: the overall target); see
    :func:`suggestions`. ``None`` when there is no complete period with history."""
    result = suggestions(session, periods=periods, today=today, before=before)
    return result.overall if category_id is None else result.for_category(category_id)


# --------------------------------------------------------------------------- changes


def _edit_window(
    session: Session, effective_from: Period | dt.date | None, today: dt.date | None
) -> tuple[dt.date, dt.date]:
    if effective_from is None:
        effective_from = current_period(session, today=today)
    if isinstance(effective_from, Period):
        return effective_from.start, effective_from.end
    if isinstance(effective_from, dt.date) and not isinstance(effective_from, dt.datetime):
        return effective_from, effective_from
    raise TypeError("effective_from must be a Period or a date")


def _budget_category(session: Session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise LookupError(f"no category with id {category_id}")
    if category.kind != "expense":
        raise BudgetError(f"{category.name} isn't a spending category, so it can't have a budget.")
    return category


def _write(
    session: Session,
    versions: _Versions,
    category_id: int | None,
    kind: str,
    window: tuple[dt.date, dt.date],
    start: dt.date,
    amount: Decimal | None,
    rollover: bool,
) -> BudgetRow:
    """Update the row of this type that starts inside ``window``, else insert one at ``start``."""
    existing = versions.inside(category_id, kind, *window)
    if existing is not None:
        budget = session.get(Budget, existing.id)
        assert budget is not None
        budget.amount = amount
        budget.rollover = rollover
    else:
        budget = Budget(
            scope="overall" if category_id is None else "category",
            category_id=category_id,
            period=kind,
            amount=amount,
            rollover=rollover,
            effective_from=start,
        )
        session.add(budget)
    session.flush()
    return _row(budget)


def set_budget(
    session: Session,
    category_id: int | None,
    amount: Decimal | None,
    *,
    effective_from: Period | dt.date | None = None,
    period: str = MONTHLY,
    today: dt.date | None = None,
) -> BudgetChange:
    """Set a category's budget (``category_id=None``: the overall monthly target) from
    ``effective_from`` on; ``amount=None`` removes it from then on (BUD-1, BUD-3, BUD-5).

    ``effective_from`` is the budget period being edited (default: the current one): the row that
    starts inside it is updated, else a row is inserted at its start, so earlier periods keep
    their amounts (ARCHITECTURE §5). An exact ``date`` edits the row for that date. ``period`` is
    ``monthly`` or ``annual``; an annual amount covers the calendar year of each period it is in
    force for. Setting one type writes an amount-``NULL`` row of the other type from the same date
    when that one is set, so a category keeps one active type. A new monthly row keeps the rollover
    setting of the one before it. Nothing is written when the amount in force is already the same.

    Raises :class:`BudgetError` (amount below 0, more than two decimals, not a spending category)
    and ``LookupError`` for an unknown category.
    """
    if period not in KINDS:
        raise ValueError(f"unknown budget period type {period!r}; use 'monthly' or 'annual'")
    if category_id is None and period != MONTHLY:
        raise BudgetError("The overall target is monthly.")
    amount = check_amount(amount)
    if category_id is not None:
        _budget_category(session, category_id)
    window = _edit_window(session, effective_from, today)
    start, end = window
    versions = _load_versions(session)
    active = versions.active(category_id, end)
    same_type = active is not None and active.period == period
    if amount is None:
        if not same_type:
            return BudgetChange(active, changed=False)
        _write(session, versions, category_id, period, window, start, None, False)
        return BudgetChange(_load_versions(session).active(category_id, end), changed=True)
    if same_type and active is not None and active.amount == amount:
        return BudgetChange(active, changed=False)
    previous = versions.in_force(category_id, period, end)
    rollover = (
        period == MONTHLY
        and previous is not None
        and previous.amount is not None
        and previous.rollover
    )
    row = _write(session, versions, category_id, period, window, start, amount, rollover)
    ended = None
    if category_id is not None:
        other = ANNUAL if period == MONTHLY else MONTHLY
        other_row = versions.in_force(category_id, other, end)
        if other_row is not None and other_row.amount is not None:
            ended = _write(
                session, versions, category_id, other, window, row.effective_from, None, False
            )
    return BudgetChange(row, changed=True, ended=ended)


def set_rollover(
    session: Session,
    category_id: int,
    enabled: bool,
    *,
    effective_from: Period | dt.date | None = None,
    today: dt.date | None = None,
) -> BudgetChange:
    """Turn rollover (BUD-4) on or off for a category's monthly budget from ``effective_from``
    (default: the current period) on, versioned like :func:`set_budget`.

    Turning it on starts a chain: that period's carry-in is 0 and what's left of it carries into
    the next. Turning it off stops the chain. Raises :class:`BudgetError` when the category has no
    monthly budget in that period.
    """
    category = _budget_category(session, category_id)
    window = _edit_window(session, effective_from, today)
    start, end = window
    versions = _load_versions(session)
    active = versions.active(category_id, end)
    if active is None or active.period != MONTHLY:
        raise BudgetError(
            f"Give {category.name} a monthly budget first: rollover carries what's left of it "
            "into the next period."
        )
    if active.rollover == enabled:
        return BudgetChange(active, changed=False)
    row = _write(session, versions, category_id, MONTHLY, window, start, active.amount, enabled)
    return BudgetChange(row, changed=True)
