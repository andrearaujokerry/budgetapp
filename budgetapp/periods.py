"""Budget and statement periods: calendar months, years and Amex statement cycles.

A :class:`Period` is an inclusive date range. Statement cycles follow ARCHITECTURE §6: a cycle is
``(previous closing date, closing date]``; closing dates come from the ``statements`` table and are
generated only before the first or after the last stored date (never between them), stepping one
month at a time with the account's ``statement_closing_day`` clamped to the month's length. A
generated date within 10 days of a stored one is dropped, and a cycle outside 25–35 days is flagged.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

MONTH = "month"
YEAR = "year"
STATEMENT = "statement"
CUSTOM = "custom"

MIN_CYCLE_DAYS = 25
MAX_CYCLE_DAYS = 35
DROP_WITHIN_DAYS = 10


@dataclass(frozen=True, slots=True)
class Period:
    """An inclusive date range ``[start, end]``.

    ``kind`` is ``month``, ``year``, ``statement`` or ``custom``. For statement cycles ``end`` is
    the closing date, ``generated`` says the closing date was generated from the closing day rather
    than stored, and ``flagged`` marks a cycle outside 25–35 days (usually a missing statement).
    """

    start: date
    end: date
    kind: str = CUSTOM
    generated: bool = False
    flagged: bool = False

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period ends ({self.end}) before it starts ({self.start})")

    @property
    def days(self) -> int:
        """Number of days in the period, both ends included."""
        return (self.end - self.start).days + 1

    @property
    def closing_date(self) -> date | None:
        """The statement closing date (``end``) for statement cycles, else ``None``."""
        return self.end if self.kind == STATEMENT else None

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def __contains__(self, day: object) -> bool:
        return isinstance(day, date) and self.contains(day)

    @property
    def label(self) -> str:
        """Short human label: ``Oct 2026``, ``2026`` or ``Sep 28 – Oct 27, 2026``."""
        if self.kind == MONTH:
            return self.start.strftime("%b %Y")
        if self.kind == YEAR:
            return str(self.start.year)
        if self.start.year == self.end.year:
            return (
                f"{self.start:%b} {self.start.day} – {self.end:%b} {self.end.day}, {self.end.year}"
            )
        return (
            f"{self.start:%b} {self.start.day}, {self.start.year} – "
            f"{self.end:%b} {self.end.day}, {self.end.year}"
        )


# --------------------------------------------------------------------------- calendar helpers


def clamp_day(year: int, month: int, day: int) -> date:
    """``date(year, month, day)`` with ``day`` clamped to the month's length (31 → Feb 28/29)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """``(year, month)`` shifted by ``delta`` months."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_period(year: int, month: int) -> Period:
    """The calendar month as a period."""
    return Period(date(year, month, 1), clamp_day(year, month, 31), MONTH)


def month_of(day: date) -> Period:
    """The calendar month containing ``day``."""
    return month_period(day.year, day.month)


def year_period(year: int) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31), YEAR)


def year_of(day: date) -> Period:
    return year_period(day.year)


def months_between(first: date, last: date) -> list[Period]:
    """Every calendar month from the one containing ``first`` to the one containing ``last``."""
    if last < first:
        return []
    out: list[Period] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(month_period(year, month))
        year, month = add_months(year, month, 1)
    return out


def days_in(period: Period) -> int:
    """Days in ``period``, both ends included."""
    return period.days


def days_left(period: Period, today: date) -> int:
    """Days left in ``period`` counting today: ``(end − today) + 1``, clamped to ``[0, days]``."""
    if today < period.start:
        return period.days
    if today > period.end:
        return 0
    return (period.end - today).days + 1


def days_elapsed(period: Period, today: date) -> int:
    """Days of ``period`` already started, counting today (0 before it starts)."""
    return period.days - days_left(period, today)


# --------------------------------------------------------------------------- statement cycles


def _too_close(candidate: date, stored: list[date]) -> bool:
    return any(abs((candidate - s).days) <= DROP_WITHIN_DAYS for s in stored)


def closing_dates(
    stored_closings: Iterable[date],
    closing_day: int | None,
    start: date,
    end: date,
    *,
    first_period_start: date | None = None,
) -> list[tuple[date, bool]]:
    """Sorted ``(closing_date, generated)`` pairs covering ``[start, end]`` plus one before.

    See :func:`statement_cycles`. ``closing_day`` ``None`` uses the day of the nearest stored date.
    """
    stored = sorted(set(stored_closings))
    anchors = list(stored)
    if first_period_start is not None and stored and first_period_start <= stored[0]:
        # The earliest statement's start is known: the day before it acts as a stored closing.
        anchors = [first_period_start - timedelta(days=1), *stored]
    if closing_day is not None and not 1 <= closing_day <= 31:
        raise ValueError("closing_day must be between 1 and 31")

    result: dict[date, bool] = {d: False for d in anchors}

    if not anchors:
        if closing_day is None:
            return []
        year, month = add_months(start.year, start.month, -2)
        last_year, last_month = add_months(end.year, end.month, 2)
        while (year, month) <= (last_year, last_month):
            result[clamp_day(year, month, closing_day)] = True
            year, month = add_months(year, month, 1)
        return _trim(sorted(result.items()), start, end)

    # Backward from the first anchor until one closing date lies before `start`.
    first = anchors[0]
    day = closing_day if closing_day is not None else first.day
    offset = 0
    earliest = first
    while earliest >= start:
        candidate = clamp_day(*add_months(first.year, first.month, offset), day)
        offset -= 1
        if candidate >= first or _too_close(candidate, anchors):
            if offset < -600:
                break
            continue
        result[candidate] = True
        earliest = candidate
        if offset < -600:  # pragma: no cover - guards against absurd ranges
            break

    # Forward from the last anchor until one closing date lies on/after `end`.
    last = anchors[-1]
    day = closing_day if closing_day is not None else last.day
    offset = 0
    latest = last
    while latest < end:
        candidate = clamp_day(*add_months(last.year, last.month, offset), day)
        offset += 1
        if candidate <= last or _too_close(candidate, anchors):
            if offset > 600:
                break
            continue
        result[candidate] = True
        latest = candidate
        if offset > 600:  # pragma: no cover
            break

    if first_period_start is not None and stored and first_period_start <= stored[0]:
        # The implied anchor is not a real statement: report it as generated.
        result[first_period_start - timedelta(days=1)] = True
    return _trim(sorted(result.items()), start, end)


def _trim(items: list[tuple[date, bool]], start: date, end: date) -> list[tuple[date, bool]]:
    """Keep the closing dates whose cycles overlap ``[start, end]``, plus the one before."""
    if not items:
        return []
    first_index = 0
    for i, (closing, _) in enumerate(items):
        if closing < start:
            first_index = i
    last_index = len(items) - 1
    for i, (closing, _) in enumerate(items):
        if closing >= end:
            last_index = i
            break
    return items[first_index : last_index + 1]


def statement_cycles(
    stored_closings: Iterable[date],
    closing_day: int | None,
    start: date,
    end: date,
    *,
    first_period_start: date | None = None,
) -> list[Period]:
    """Statement cycles overlapping ``[start, end]``, oldest first (ARCHITECTURE §6).

    * ``stored_closings``: closing dates from the ``statements`` table for one account.
    * ``closing_day``: the account's ``statement_closing_day`` (1–31, clamped to month length), used
      only to generate dates before the first or after the last stored date. ``None`` falls back to
      the day of the nearest stored date; with no stored dates and no closing day the result is
      empty.
    * Generated dates within 10 days of a stored date are dropped; cycles outside 25–35 days are
      ``flagged`` (stored dates are never moved, so a missing statement shows as a long flagged
      cycle rather than a phantom one).
    * ``first_period_start``: the earliest statement's ``period_start_override``.
    """
    dates = closing_dates(
        stored_closings, closing_day, start, end, first_period_start=first_period_start
    )
    cycles: list[Period] = []
    for (previous, _), (closing, generated) in zip(dates, dates[1:], strict=False):
        cycle = Period(
            previous + timedelta(days=1),
            closing,
            STATEMENT,
            generated=generated,
            flagged=not MIN_CYCLE_DAYS <= (closing - previous).days <= MAX_CYCLE_DAYS,
        )
        if cycle.end >= start and cycle.start <= end:
            cycles.append(cycle)
    return cycles


def statement_cycle_for(
    day: date,
    stored_closings: Iterable[date],
    closing_day: int | None,
    *,
    first_period_start: date | None = None,
) -> Period | None:
    """The statement cycle containing ``day``, or ``None`` when no cycle can be computed."""
    for cycle in statement_cycles(
        stored_closings, closing_day, day, day, first_period_start=first_period_start
    ):
        if cycle.contains(day):
            return cycle
    return None


# --------------------------------------------------------------------------- budget periods


def budget_period_for(
    day: date,
    *,
    mode: str = "calendar",
    closings: Iterable[date] = (),
    closing_day: int | None = None,
    first_period_start: date | None = None,
) -> Period:
    """The budget period containing ``day``.

    ``mode`` is ``calendar`` (calendar month) or ``statement`` (the cycle of the budget card, from
    its stored closing dates and closing day). Statement mode falls back to the calendar month
    when no cycle can be computed; callers detect that with ``period.kind == "month"`` and show a
    notice.
    """
    if mode == "calendar":
        return month_of(day)
    if mode != "statement":
        raise ValueError(f"unknown budget period mode {mode!r}")
    cycle = statement_cycle_for(day, closings, closing_day, first_period_start=first_period_start)
    return cycle if cycle is not None else month_of(day)


def _require_cycle_inputs(closings: Iterable[date], closing_day: int | None) -> tuple[date, ...]:
    stored = tuple(closings)
    if not stored and closing_day is None:
        raise ValueError(
            "statement periods need the card's closing dates or closing day "
            "(pass closings= and/or closing_day=)"
        )
    return stored


def previous_period(
    period: Period,
    *,
    closings: Iterable[date] = (),
    closing_day: int | None = None,
    first_period_start: date | None = None,
) -> Period:
    """The period before ``period`` (same kind; statement cycles need the closing dates).

    For a statement cycle pass the card's ``closings`` and/or ``closing_day``; without either a
    ``ValueError`` is raised (a silent calendar-month answer would corrupt rollover chains).
    """
    if period.kind == MONTH:
        year, month = add_months(period.start.year, period.start.month, -1)
        return month_period(year, month)
    if period.kind == YEAR:
        return year_period(period.start.year - 1)
    if period.kind == STATEMENT:
        closings = _require_cycle_inputs(closings, closing_day)
        return budget_period_for(
            period.start - timedelta(days=1),
            mode="statement",
            closings=closings,
            closing_day=closing_day,
            first_period_start=first_period_start,
        )
    length = period.days
    return Period(period.start - timedelta(days=length), period.start - timedelta(days=1), CUSTOM)


def next_period(
    period: Period,
    *,
    closings: Iterable[date] = (),
    closing_day: int | None = None,
    first_period_start: date | None = None,
) -> Period:
    """The period after ``period`` (same kind; statement cycles need the closing dates).

    Like :func:`previous_period`, a statement cycle without ``closings`` or ``closing_day``
    raises ``ValueError``.
    """
    if period.kind == MONTH:
        year, month = add_months(period.start.year, period.start.month, 1)
        return month_period(year, month)
    if period.kind == YEAR:
        return year_period(period.start.year + 1)
    if period.kind == STATEMENT:
        closings = _require_cycle_inputs(closings, closing_day)
        return budget_period_for(
            period.end + timedelta(days=1),
            mode="statement",
            closings=closings,
            closing_day=closing_day,
            first_period_start=first_period_start,
        )
    length = period.days
    return Period(period.end + timedelta(days=1), period.end + timedelta(days=length), CUSTOM)
