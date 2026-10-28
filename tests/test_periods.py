from datetime import date, timedelta

import pytest

from budgetapp.periods import (
    MONTH,
    STATEMENT,
    YEAR,
    Period,
    add_months,
    budget_period_for,
    clamp_day,
    closing_dates,
    days_elapsed,
    days_in,
    days_left,
    month_of,
    month_period,
    months_between,
    next_period,
    previous_period,
    statement_cycle_for,
    statement_cycles,
    year_of,
)

D = date.fromisoformat


class TestCalendarHelpers:
    def test_clamp_day(self):
        assert clamp_day(2025, 2, 31) == D("2025-02-28")
        assert clamp_day(2024, 2, 31) == D("2024-02-29")
        assert clamp_day(2026, 4, 31) == D("2026-04-30")
        assert clamp_day(2026, 1, 15) == D("2026-01-15")

    def test_add_months_wraps_years(self):
        assert add_months(2026, 11, 3) == (2027, 2)
        assert add_months(2026, 1, -1) == (2025, 12)
        assert add_months(2026, 1, -13) == (2024, 12)

    def test_month_and_year_periods(self):
        feb = month_period(2024, 2)
        assert (feb.start, feb.end, feb.kind) == (D("2024-02-01"), D("2024-02-29"), MONTH)
        assert month_of(D("2026-10-15")) == month_period(2026, 10)
        assert year_of(D("2026-03-01")) == Period(D("2026-01-01"), D("2026-12-31"), YEAR)
        assert days_in(month_period(2025, 2)) == 28

    def test_days_left_counts_today(self):
        october = month_period(2026, 10)
        assert days_left(october, D("2026-10-01")) == 31
        assert days_left(october, D("2026-10-31")) == 1
        assert days_left(october, D("2026-11-01")) == 0
        assert days_left(october, D("2026-09-30")) == 31
        assert days_elapsed(october, D("2026-10-10")) == 9

    def test_months_between(self):
        months = months_between(D("2025-11-20"), D("2026-02-03"))
        assert [m.label for m in months] == ["Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026"]
        assert months_between(D("2026-02-01"), D("2026-01-01")) == []

    def test_period_validation_and_contains(self):
        with pytest.raises(ValueError):
            Period(D("2026-01-02"), D("2026-01-01"))
        p = Period(D("2026-01-01"), D("2026-01-31"))
        assert D("2026-01-31") in p
        assert D("2026-02-01") not in p

    def test_labels(self):
        assert month_period(2026, 10).label == "Oct 2026"
        assert Period(D("2026-09-28"), D("2026-10-27"), STATEMENT).label == "Sep 28 – Oct 27, 2026"
        assert (
            Period(D("2025-12-18"), D("2026-01-17"), STATEMENT).label
            == "Dec 18, 2025 – Jan 17, 2026"
        )


def _ends(cycles):
    return [c.end for c in cycles]


class TestStatementCycles:
    def test_stored_dates_define_cycles(self):
        stored = [D("2026-01-17"), D("2026-02-16"), D("2026-03-18")]
        cycles = statement_cycles(stored, 17, D("2026-01-18"), D("2026-03-18"))
        assert [(c.start, c.end) for c in cycles] == [
            (D("2026-01-18"), D("2026-02-16")),
            (D("2026-02-17"), D("2026-03-18")),
        ]
        assert all(c.kind == STATEMENT and not c.generated and not c.flagged for c in cycles)

    def test_closing_day_31_across_february(self):
        cycles = statement_cycles([], 31, D("2025-01-15"), D("2025-04-30"))
        assert _ends(cycles) == [D("2025-01-31"), D("2025-02-28"), D("2025-03-31"), D("2025-04-30")]
        assert cycles[1].start == D("2025-02-01")
        assert all(c.generated and not c.flagged for c in cycles)

    def test_closing_day_31_in_a_leap_year(self):
        cycles = statement_cycles([], 31, D("2024-02-10"), D("2024-03-10"))
        assert _ends(cycles) == [D("2024-02-29"), D("2024-03-31")]
        assert cycles[0].start == D("2024-02-01")
        assert cycles[1].start == D("2024-03-01")

    def test_closing_day_29_and_30_in_february(self):
        assert _ends(statement_cycles([], 30, D("2025-02-10"), D("2025-02-20"))) == [
            D("2025-02-28")
        ]
        assert _ends(statement_cycles([], 29, D("2024-02-10"), D("2024-02-20"))) == [
            D("2024-02-29")
        ]

    def test_drifting_stored_dates_never_produce_phantom_cycles(self):
        # Amex closing dates drift by 1-2 days (RESEARCH T4.3).
        stored = [
            D("2026-01-17"),
            D("2026-02-16"),
            D("2026-03-18"),
            D("2026-04-17"),
            D("2026-05-16"),
        ]
        cycles = statement_cycles(stored, 17, D("2025-12-01"), D("2026-07-31"))
        ends = _ends(cycles)
        assert set(stored) <= set(ends)
        assert all(25 <= c.days <= 35 for c in cycles)
        assert not any(c.flagged for c in cycles)
        # Generated dates only outside the stored range; May 17 is dropped (1 day from May 16).
        generated = [c.end for c in cycles if c.generated]
        assert all(d < stored[0] or d > stored[-1] for d in generated)
        assert D("2026-06-17") in ends and D("2026-05-17") not in ends

    def test_stored_date_crossing_a_month_boundary(self):
        # Closing day 30, real closings Oct 31 and Dec 1: no Nov 30 phantom cycle.
        stored = [D("2026-10-31"), D("2026-12-01")]
        cycles = statement_cycles(stored, 30, D("2026-11-01"), D("2026-12-01"))
        assert [(c.start, c.end) for c in cycles] == [(D("2026-11-01"), D("2026-12-01"))]
        assert not cycles[0].flagged

    def test_generated_date_near_a_stored_one_is_dropped(self):
        # Closing day 31 with a stored Mar 2 (drifted): Feb 28 is within 10 days and dropped.
        cycles = statement_cycles([D("2025-03-02")], 31, D("2025-02-01"), D("2025-03-02"))
        assert [(c.start, c.end) for c in cycles] == [(D("2025-02-01"), D("2025-03-02"))]
        assert not cycles[0].flagged

    def test_two_day_drift_around_february(self):
        stored = [D("2025-01-31"), D("2025-03-02"), D("2025-03-31")]
        cycles = statement_cycles(stored, 31, D("2025-02-01"), D("2025-03-31"))
        assert _ends(cycles) == [D("2025-03-02"), D("2025-03-31")]

    def test_gap_between_stored_dates_is_flagged_not_filled(self):
        stored = [D("2026-01-17"), D("2026-03-18")]  # February's statement is missing
        cycles = statement_cycles(stored, 17, D("2026-02-01"), D("2026-03-01"))
        assert len(cycles) == 1
        assert (cycles[0].start, cycles[0].end) == (D("2026-01-18"), D("2026-03-18"))
        assert cycles[0].flagged and not cycles[0].generated

    def test_first_period_start_override(self):
        cycles = statement_cycles(
            [D("2026-03-17")],
            None,
            D("2026-03-01"),
            D("2026-03-17"),
            first_period_start=D("2026-02-15"),
        )
        assert [(c.start, c.end) for c in cycles] == [(D("2026-02-15"), D("2026-03-17"))]
        assert not cycles[0].flagged

    def test_without_closing_day_the_nearest_stored_day_is_used(self):
        cycles = statement_cycles([D("2026-03-17")], None, D("2026-03-18"), D("2026-05-10"))
        assert _ends(cycles) == [D("2026-04-17"), D("2026-05-17")]

    def test_nothing_known_gives_no_cycles(self):
        assert statement_cycles([], None, D("2026-01-01"), D("2026-12-31")) == []
        assert closing_dates([], None, D("2026-01-01"), D("2026-12-31")) == []

    def test_invalid_closing_day(self):
        with pytest.raises(ValueError):
            statement_cycles([], 32, D("2026-01-01"), D("2026-02-01"))

    def test_every_day_belongs_to_exactly_one_cycle(self):
        stored = [D("2025-12-17"), D("2026-01-16"), D("2026-02-17"), D("2026-03-17")]
        start, end = D("2025-10-01"), D("2026-08-31")
        cycles = statement_cycles(stored, 17, start, end)
        day = start
        while day <= end:
            assert sum(1 for c in cycles if c.contains(day)) == 1, day
            day += timedelta(days=1)

    def test_cycle_for(self):
        stored = [D("2026-01-17"), D("2026-02-16")]
        cycle = statement_cycle_for(D("2026-02-16"), stored, 17)
        assert cycle is not None and cycle.start == D("2026-01-18")
        assert statement_cycle_for(D("2026-02-17"), stored, 17).end == D("2026-03-17")


class TestBudgetPeriods:
    def test_calendar_mode(self):
        assert budget_period_for(D("2026-10-31")) == month_period(2026, 10)

    def test_statement_mode(self):
        stored = [D("2026-09-27"), D("2026-10-27")]
        period = budget_period_for(
            D("2026-10-28"), mode="statement", closings=stored, closing_day=27
        )
        assert (period.start, period.end, period.kind) == (
            D("2026-10-28"),
            D("2026-11-27"),
            STATEMENT,
        )

    def test_statement_mode_falls_back_to_calendar(self):
        period = budget_period_for(D("2026-10-28"), mode="statement")
        assert period.kind == MONTH and period == month_period(2026, 10)

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            budget_period_for(D("2026-10-28"), mode="weekly")

    def test_previous_and_next(self):
        oct_ = month_period(2026, 10)
        assert previous_period(oct_) == month_period(2026, 9)
        assert next_period(month_period(2026, 12)) == month_period(2027, 1)
        assert previous_period(year_of(D("2026-05-05"))) == year_of(D("2025-05-05"))
        stored = [D("2026-09-27"), D("2026-10-27")]
        cycle = budget_period_for(
            D("2026-10-10"), mode="statement", closings=stored, closing_day=27
        )
        prev = previous_period(cycle, closings=stored, closing_day=27)
        nxt = next_period(cycle, closings=stored, closing_day=27)
        assert prev.end == D("2026-09-27") and nxt.start == D("2026-10-28")
        custom = Period(D("2026-01-01"), D("2026-01-10"))
        assert previous_period(custom) == Period(D("2025-12-22"), D("2025-12-31"))
        assert next_period(custom) == Period(D("2026-01-11"), D("2026-01-20"))


def test_statement_periods_need_their_closing_dates():
    """A missing argument must not silently turn a statement cycle into a calendar month."""
    stored = [date(2026, 9, 27), date(2026, 10, 27)]
    cycle = budget_period_for(date(2026, 10, 10), mode="statement", closings=stored, closing_day=27)
    assert cycle.kind == "statement"
    for step in (previous_period, next_period):
        with pytest.raises(ValueError, match="closing"):
            step(cycle)
        with pytest.raises(ValueError, match="closing"):
            step(cycle, closings=iter(()))  # an empty iterator is no closing dates either
    before = previous_period(cycle, closings=iter(stored), closing_day=27)
    assert before.kind == "statement" and before.end == date(2026, 9, 27)
    assert next_period(cycle, closing_day=27).kind == "statement"
