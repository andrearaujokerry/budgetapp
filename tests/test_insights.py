"""services.insights (INS-1, INS-2) and spending.daily_spending: the dashboard's and the trends'
numbers, which come only from services.spending and services.budgets."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from budgetapp.models import get_app_settings
from budgetapp.money import to_money
from budgetapp.periods import month_period
from budgetapp.services import budgets, insights, spending
from budgetapp.services import sync as sync_service
from budgetapp.services.insights import Change, change, month_param
from tests.bank_factories import make_bank_account, make_bank_item
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_budget,
    make_category,
    make_split,
    make_statement,
    make_transaction,
    set_app_settings,
)

D = Decimal
OCT = month_period(2026, 10)
NOV = month_period(2026, 11)


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


@pytest.fixture
def account(db):
    return make_account(db, statement_closing_day=27)


@pytest.fixture
def cat(db):
    return lambda name: get_category(db, name)


@pytest.fixture
def spend(db, account):
    def make(category, amount, date, **kwargs):
        return make_transaction(db, account, amount=amount, category=category, date=date, **kwargs)

    return make


# --------------------------------------------------------------------------- change()


class TestChange:
    def test_up_and_down_with_whole_percent(self):
        assert change(D("120.00"), D("100.00")) == Change(
            D("120.00"), D("100.00"), D("20.00"), D("20"), insights.UP
        )
        down = change(D("66.66"), D("100.00"))
        assert (down.amount, down.percent, down.kind) == (D("-33.34"), D("-33"), insights.DOWN)
        assert change(D("100.50"), D("100.00")).percent == D("1")  # 0.5% rounds half-up

    def test_new_gone_same_and_nothing(self):
        assert change(D("50.00"), D("0.00")).kind == insights.NEW
        assert change(D("50.00"), D("0.00")).percent is None  # no percentage of nothing
        gone = change(D("0.00"), D("80.00"))
        assert (gone.kind, gone.percent, gone.amount) == (insights.GONE, D("-100"), D("-80.00"))
        assert change(D("5.00"), D("5.00")).kind == insights.SAME
        assert change(D("0.00"), D("0.00")).kind == insights.NONE

    def test_refunds_larger_than_charges_have_no_percentage(self):
        result = change(D("30.00"), D("-20.00"))  # last month's refunds outweighed its charges
        assert (result.amount, result.percent, result.kind) == (D("50.00"), None, insights.UP)

    def test_no_data_on_either_side_is_no_change(self):
        assert change(None, D("1.00")) is None
        assert change(D("1.00"), None) is None


def test_month_param():
    assert month_param("2026-10") == day("2026-10-01")
    assert month_param(" 2026-10-17 ") == day("2026-10-01")
    for bad in ("", None, "2026-13", "October", "0099-01", "1999-12", "9999-12", "2026-1"):
        assert month_param(bad) is None


# --------------------------------------------------------------------------- daily spending


class TestDailySpending:
    def test_every_day_listed_and_the_days_add_up_to_period_spending(self, db, cat, spend):
        spend(cat("Groceries"), "40.00", "2026-10-03")
        spend(cat("Groceries"), "-15.00", "2026-10-03")  # a refund the same day
        spend(cat("Dining & Drinks"), "22.50", "2026-10-31")
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-10")  # not spending
        make_transaction(db, amount="-500.00", category=card_payments(db), date="2026-10-11")
        days = spending.daily_spending(db, OCT)
        assert [d.day for d in days] == [OCT.start + dt.timedelta(days=i) for i in range(31)]
        amounts = {d.day: d.amount for d in days if d.amount}
        assert amounts == {day("2026-10-03"): D("25.00"), day("2026-10-31"): D("22.50")}
        assert sum(d.amount for d in days) == spending.period_spending(db, OCT).total

    def test_splits_count_their_expense_parts(self, db, cat, spend):
        txn = spend(cat("Groceries"), "100.00", "2026-10-05")
        make_split(db, txn, cat("Groceries"), "70.00")
        make_split(db, txn, cat("Credits & Rewards"), "30.00")
        days = spending.daily_spending(db, OCT)
        assert days[4].amount == D("70.00")

    def test_calendar_mode_dates_by_purchase_statement_mode_by_posting(self, db, cat, spend):
        spend(
            cat("Home"), "60.00", "2026-11-02", authorized_date=day("2026-10-30")
        )  # bought Oct 30, posted Nov 2
        assert spending.daily_spending(db, OCT)[29].amount == D("60.00")
        set_app_settings(db, budget_period_mode="statement")
        assert sum(d.amount for d in spending.daily_spending(db, OCT)) == 0
        assert spending.daily_spending(db, NOV)[1].amount == D("60.00")

    def test_pending_follows_the_budget_setting(self, db, cat, spend):
        spend(cat("Home"), "9.00", "2026-10-08", pending=True)
        assert spending.daily_spending(db, OCT)[7].amount == D("9.00")
        set_app_settings(db, include_pending_in_budgets=False)
        assert spending.daily_spending(db, OCT)[7].amount == 0


# --------------------------------------------------------------------------- dashboard


class TestDashboard:
    def test_budget_figures_are_the_budgets_pages(self, db, cat, spend):
        groceries = cat("Groceries")
        make_budget(db, groceries, "500.00", effective_from=day("2026-09-01"), rollover=True)
        make_budget(db, None, "2000.00", effective_from=day("2026-01-01"))
        spend(groceries, "450.00", "2026-09-10")
        spend(groceries, "120.00", "2026-10-05")
        d = insights.dashboard(db, today=day("2026-10-20"))
        today = day("2026-10-20")
        expected = budgets.overview(db, budgets.current_period(db, today=today), today=today)
        assert d.overview == expected
        assert d.period == OCT
        line = d.overview.line_for(groceries.id)
        assert (line.carry_in, line.available, line.spent) == (D("50.00"), D("550.00"), D("120.00"))
        assert d.overview.days_left == 12
        assert d.annual == budgets.annual_overviews(db, OCT, today=day("2026-10-20"))[-1]
        assert d.annual_other == ()

    def test_a_cycle_across_the_year_end_shows_the_year_we_are_in(self, db, account, cat, spend):
        """Review finding 1: a statement cycle Dec 28 – Jan 27 touches two years. On Dec 30 the
        annual budgets are 2026's to date (the Budgets page's 2026 tally for this cycle), not
        2027's, which hasn't started; on Jan 5 they are 2027's, with 2026's final tally beside."""
        for closing in ("2026-10-27", "2026-11-27", "2026-12-27"):
            make_statement(db, account, closing)
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        travel = cat("Travel")
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
        spend(travel, "2500.00", "2026-06-10")
        spend(travel, "300.00", "2026-12-29")
        spend(travel, "40.00", "2027-01-03")

        december = insights.dashboard(db, today=day("2026-12-30"))
        cycle = december.period
        assert (cycle.start, cycle.end) == (day("2026-12-28"), day("2027-01-27"))
        old, new = budgets.annual_overviews(db, cycle, today=day("2026-12-30"))
        assert december.annual == old and december.annual_other == (new,)
        assert (old.year, old.as_of, old.spent_total) == (2026, day("2026-12-31"), D("2800.00"))

        january = insights.dashboard(db, today=day("2027-01-05"))
        assert january.period == cycle
        assert (january.annual.year, january.annual.spent_total) == (2027, D("40.00"))
        (closed,) = january.annual_other
        assert (closed.year, closed.spent_total) == (2026, D("2800.00"))

    def test_top_categories_add_up_to_the_total(self, db, cat, spend):
        names = ["Groceries", "Dining & Drinks", "Home", "Shopping", "Travel", "Education", "Gifts"]
        amounts = ["700", "600", "500", "400", "300", "200", "100"]
        gifts = make_category(db, "Gifts", color="cat-9", is_hidden=True)
        for name, amount in zip(names, amounts, strict=True):
            spend(gifts if name == "Gifts" else cat(name), amount, "2026-10-04")
        spend(cat("Entertainment"), "-40.00", "2026-10-05")  # refunds outweigh charges
        d = insights.dashboard(db, today=day("2026-10-20"))
        top = d.top_categories
        assert [r.category.name for r in top.rows] == names[:6]
        assert [r.amount for r in top.rows] == [D(a) for a in amounts[:6]]
        assert top.total == d.overview.spending.total == D("2760.00")
        assert top.rest_count == 2 and top.rest_amount == D("60.00")  # Gifts 100 − 40 refund
        assert sum(r.amount for r in top.rows) + top.rest_amount == top.total
        assert top.rows[0].percent == D("25")  # 700 / 2760

    def test_hidden_categories_are_listed_and_marked(self, db, spend):
        hidden = make_category(db, "Side business", is_hidden=True)
        spend(hidden, "80.00", "2026-10-04")
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert [(r.category.name, r.category.is_hidden) for r in d.top_categories.rows] == [
            ("Side business", True)
        ]

    def test_nothing_spent_this_period(self, db, cat, spend):
        spend(cat("Home"), "30.00", "2026-09-29")
        d = insights.dashboard(db, today=day("2026-10-01"))
        assert d.has_transactions
        assert d.top_categories.rows == () and d.top_categories.total == 0
        assert d.top_merchants == ()
        assert d.pace.spent == 0 and d.pace.current == (D("0.00"),) + (None,) * 30
        assert d.pace.previous_spent == 0  # nothing by Sep 1
        assert d.pace.previous_total == D("30.00")

    def test_empty_database(self, db):
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert not d.has_transactions
        assert d.held_rows == 0
        assert d.pace.previous_spent is None and d.pace.change is None
        assert d.upcoming.charges == () and d.upcoming.total == 0

    def test_top_merchants_are_matched_as_the_transactions_list_matches_them(self, db, cat, spend):
        """Review finding 3: one merchant whatever its case and spacing (the list's
        ``merchant=`` key), named by its most frequent merchant name, with the spending
        categories its figure is in (a credit in Credits & Rewards isn't one)."""
        groceries, dining = cat("Groceries"), cat("Dining & Drinks")
        spend(groceries, "40.00", "2026-10-02", merchant_name="Whole Foods")
        spend(groceries, "25.00", "2026-10-09", merchant_name="Whole Foods")
        spend(dining, "15.00", "2026-10-03", merchant_name=None, name="WHOLE  FOODS ")
        spend(cat("Credits & Rewards"), "-10.00", "2026-10-05", merchant_name="whole foods")
        spend(groceries, "5.00", "2026-10-06", merchant_name="Whole Foods Market")
        rows = insights.dashboard(db, today=day("2026-10-15")).top_merchants
        assert [(m.merchant, m.amount, m.transaction_count) for m in rows] == [
            ("Whole Foods", D("80.00"), 3),
            ("Whole Foods Market", D("5.00"), 1),
        ]
        assert rows[0].category_ids == tuple(sorted((groceries.id, dining.id)))
        assert rows[1].category_ids == (groceries.id,)

    def test_a_merchant_known_only_by_its_description(self, db, cat, spend):
        spend(cat("Home"), "12.00", "2026-10-02", merchant_name=None, name="HARDWARE  STORE")
        spend(cat("Home"), "8.00", "2026-10-03", merchant_name=None, name="Hardware Store")
        spend(cat("Home"), "9.00", "2026-10-04", merchant_name=None, name="Hardware Store")
        (m,) = spending.top_merchants(
            db, OCT.start, OCT.end, basis=spending.BASIS_SPEND_DATE, include_pending=True
        )
        assert (m.merchant, m.amount, m.transaction_count) == ("Hardware Store", D("29.00"), 3)

    def test_top_merchants_follow_the_budget_basis(self, db, cat, spend):
        spend(cat("Dining & Drinks"), "12.00", "2026-10-03", merchant_name="Blue Bottle")
        spend(cat("Dining & Drinks"), "8.00", "2026-10-09", merchant_name="Blue Bottle")
        spend(cat("Groceries"), "55.00", "2026-10-09", name="SAFEWAY #12")
        spend(cat("Groceries"), "-55.00", "2026-10-10", name="SAFEWAY #12")  # refunded in full
        spend(cat("Home"), "99.00", "2026-11-02", authorized_date=day("2026-10-31"),
              merchant_name="IKEA")  # fmt: skip
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert [(m.merchant, m.amount, m.transaction_count) for m in d.top_merchants] == [
            ("IKEA", D("99.00"), 1),
            ("Blue Bottle", D("20.00"), 2),
        ]
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=None)
        # Statement mode without a card falls back to calendar months (with a notice).
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert d.overview.notice and d.period == OCT

    def test_pace_against_the_same_day_of_the_previous_period(self, db, cat, spend):
        home = cat("Home")
        spend(home, "100.00", "2026-09-01")
        spend(home, "50.00", "2026-09-10")
        spend(home, "500.00", "2026-09-30")
        spend(home, "20.00", "2026-10-01")
        spend(home, "30.00", "2026-10-09")
        pace = insights.dashboard(db, today=day("2026-10-09")).pace
        assert pace.period == OCT and pace.previous_period == month_period(2026, 9)
        assert len(pace.days) == len(pace.current) == len(pace.previous) == 31
        assert pace.current[:9] == (D("20.00"),) * 8 + (D("50.00"),)
        assert set(pace.current[9:]) == {None}  # days still to come
        assert pace.previous[8] == D("100.00") and pace.previous[9] == D("150.00")
        assert pace.previous[29] == D("650.00") and pace.previous[30] is None  # Sep has 30 days
        assert (pace.spent, pace.previous_spent, pace.previous_total) == (
            D("50.00"),
            D("100.00"),
            D("650.00"),
        )
        assert pace.previous_through == day("2026-09-09")
        assert pace.change.amount == D("-50.00") and not pace.previous_partial

    def test_pace_on_the_31st_after_a_30_day_month(self, db, cat, spend):
        spend(cat("Home"), "10.00", "2026-09-30")
        spend(cat("Home"), "5.00", "2026-10-31")
        pace = insights.dashboard(db, today=day("2026-10-31")).pace
        assert pace.previous_spent == D("10.00") and pace.previous_through == day("2026-09-30")

    def test_pace_when_the_history_starts_inside_the_previous_period(self, db, cat, spend):
        spend(cat("Home"), "10.00", "2026-09-20")
        pace = insights.dashboard(db, today=day("2026-10-05")).pace
        assert pace.previous_partial and pace.previous_spent == 0

    def test_a_row_dated_after_today_extends_the_line(self, db, cat, spend):
        spend(cat("Home"), "10.00", "2026-10-02")
        spend(cat("Home"), "7.00", "2026-10-06")  # Plaid can date a charge ahead
        pace = insights.dashboard(db, today=day("2026-10-03")).pace
        assert pace.current[5] == D("17.00") and pace.current[6] is None
        assert (
            pace.spent
            == D("17.00")
            == insights.dashboard(db, today=day("2026-10-03")).overview.spending.total
        )

    def test_statement_cycles(self, db, account, cat, spend):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        make_statement(db, account, "2026-09-27")
        make_statement(db, account, "2026-10-27")
        # Bought Oct 27 (old cycle by purchase), posted Oct 28: the new cycle counts it.
        spend(cat("Home"), "40.00", "2026-10-28", authorized_date=day("2026-10-27"))
        spend(cat("Home"), "10.00", "2026-10-20")
        d = insights.dashboard(db, today=day("2026-11-02"))
        assert (d.period.start, d.period.end, d.period.kind) == (
            day("2026-10-28"),
            day("2026-11-27"),
            "statement",
        )
        assert d.overview.mode == "statement" and d.overview.spending.basis == "posted_date"
        assert d.top_categories.total == D("40.00")
        assert d.pace.days[0] == day("2026-10-28") and d.pace.current[0] == D("40.00")
        assert d.pace.previous_period.end == day("2026-10-27")
        assert d.pace.previous_spent == D("0.00")  # by Sep 29 (day 6 of that cycle)

    def test_held_rows_are_counted(self, db, account, cat):
        make_transaction(
            db, account, amount="12.00", source="csv", duplicate_status="suspect",
            category=cat("Home"),
        )  # fmt: skip
        assert insights.dashboard(db, today=day("2026-10-20")).held_rows == 1

    def test_upcoming_recurring_charges_before_the_period_ends(self, db, cat, spend):
        subs = cat("Subscriptions & Streaming")
        for month in (7, 8, 9, 10):
            spend(subs, "15.49", f"2026-{month:02d}-12", merchant_name="Netflix")
        for month in (7, 8, 9, 10):
            spend(subs, "9.99", f"2026-{month:02d}-28", merchant_name="Spotify")
        early = insights.dashboard(db, today=day("2026-11-03")).upcoming
        assert [(s.merchant, s.next_expected) for s in early.charges] == [
            ("Netflix", day("2026-11-12")),
            ("Spotify", day("2026-11-28")),
        ]
        assert early.total == D("25.48")
        assert early.monthly_total == D("25.48")
        late = insights.dashboard(db, today=day("2026-11-20")).upcoming
        assert [s.merchant for s in late.charges] == ["Spotify"]  # Netflix is late, not upcoming
        assert [s.merchant for s in late.late] == ["Netflix"]


# --------------------------------------------------------------------------- trends


@pytest.fixture
def history(db, cat, spend):
    """Groceries every month from Nov 2025; Travel in Aug 2026 only; Dining from Oct 2026."""
    groceries, travel, dining = cat("Groceries"), cat("Travel"), cat("Dining & Drinks")
    for year, month in [(2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 11)]:
        spend(groceries, "100.00", f"{year}-{month:02d}-10")
    spend(groceries, "60.00", "2026-10-20")  # Oct: 160
    spend(groceries, "30.00", "2026-11-02")
    spend(travel, "900.00", "2026-08-15")
    spend(dining, "45.00", "2026-10-03")
    spend(dining, "5.00", "2026-11-01")
    return {"groceries": groceries, "travel": travel, "dining": dining}


class TestTrends:
    def test_twelve_months_through_this_month(self, db, history):
        t = insights.trends(db, today=day("2026-11-05"))
        assert t.span == 12 and t.through == NOV
        assert [m.period.start for m in t.months] == [
            day("2025-12-01"),
            *(day(f"2026-{m:02d}-01") for m in range(1, 12)),
        ]
        assert t.months[-1].status == insights.CURRENT and t.current == t.months[-1]
        assert {m.status for m in t.months[:-1]} == {insights.COMPLETE}
        assert t.compare == OCT and t.previous == month_period(2026, 9)
        assert t.ended == t.months[-2] and t.ended.period == t.compare
        assert t.months[-2].total == D("205.00") and t.months[-2].change.amount == D("105.00")
        assert not t.trimmed and t.range_has_data

    def test_change_from_last_month_per_category(self, db, history):
        t = insights.trends(db, today=day("2026-11-05"))
        rows = {c.category.name: c for c in t.changed}
        assert list(rows) == ["Groceries", "Dining & Drinks"]  # Travel: nothing in Sep or Oct
        assert rows["Groceries"].change == Change(
            D("160.00"), D("100.00"), D("60.00"), D("60"), "up"
        )
        assert rows["Dining & Drinks"].change.kind == insights.NEW
        assert t.totals.change.current == D("205.00")
        assert t.biggest_increase.category.name == "Groceries"
        assert t.biggest_decrease is None

    def test_categories_that_disappear(self, db, history):
        t = insights.trends(db, through=day("2026-09-01"), today=day("2026-11-05"))
        travel = next(c for c in t.categories if c.category.name == "Travel")
        assert t.compare == month_period(2026, 9)
        assert travel.change.kind == insights.GONE and travel.change.amount == D("-900.00")
        assert t.current is None  # Nov isn't in this range
        assert t.biggest_decrease.category.name == "Travel"

    def test_the_first_month_shown_is_compared_with_the_month_before_the_range(
        self, db, cat, spend
    ):
        spend(cat("Home"), "70.00", "2024-11-03")
        spend(cat("Home"), "35.00", "2024-12-03")
        t = insights.trends(db, through=day("2025-11-01"), today=day("2026-11-05"))
        assert t.months[0].period == month_period(2024, 12) and not t.trimmed
        assert t.months[0].change.amount == D("-35.00")
        home = t.categories[0]
        assert home.changes[0].previous == D("70.00") and home.total == D("35.00")

    def test_the_month_in_progress_against_the_same_days_last_month(self, db, history):
        t = insights.trends(db, today=day("2026-11-05"))
        assert t.current_through == day("2026-10-05")
        dining = next(c for c in t.categories if c.category.name == "Dining & Drinks")
        assert dining.so_far == D("5.00") and dining.so_far_change.previous == D("45.00")
        assert t.totals.so_far == D("35.00")
        assert t.totals.so_far_change.previous == D("45.00")  # Groceries' Oct 10 not yet

    def test_same_days_clamp_to_a_shorter_month(self, db, cat, spend):
        spend(cat("Home"), "5.00", "2026-02-28")
        spend(cat("Home"), "6.00", "2026-03-30")
        t = insights.trends(db, today=day("2026-03-30"))
        assert t.current_through == day("2026-02-28")
        assert t.totals.so_far_change.previous == D("5.00")

    def test_averages_count_only_complete_months(self, db, history):
        t = insights.trends(db, today=day("2026-11-05"))
        # Dec 2025 – Oct 2026: 11 complete months; Nov is in progress.
        assert t.complete_months == 11
        groceries = next(c for c in t.categories if c.category.name == "Groceries")
        assert groceries.average == D("105.45")  # (10 × 100 + 160) / 11
        travel = next(c for c in t.categories if c.category.name == "Travel")
        assert travel.average == D("81.82") and travel.total == D("900.00")

    def test_months_before_the_history_are_left_out_and_the_first_is_partial(self, db, cat, spend):
        spend(cat("Home"), "40.00", "2026-06-15")
        spend(cat("Home"), "50.00", "2026-07-15")
        t = insights.trends(db, today=day("2026-08-10"))
        assert t.trimmed and [m.period.start.month for m in t.months] == [6, 7, 8]
        assert [m.status for m in t.months] == ["first", "complete", "current"]
        assert t.months[0].change is None  # nothing to compare June with
        assert t.compare == month_period(2026, 7) and t.months[1].change.amount == D("10.00")
        assert t.average == D("50.00") and t.complete_months == 1  # June is partial

    def test_the_historys_first_month_has_no_month_to_compare_with(self, db, cat, spend):
        """Review finding 2: history from Oct 15, today Nov 10. October has ended but September
        has no data, so there is no change from last month (not "no spending in either"), and
        November so far isn't set against Oct 1–10, which are before the history."""
        groceries = cat("Groceries")
        spend(groceries, "120.00", "2026-10-15")
        spend(groceries, "30.00", "2026-11-02")
        t = insights.trends(db, today=day("2026-11-10"))
        assert [m.status for m in t.months] == [insights.FIRST, insights.CURRENT]
        assert t.ended == t.months[0] and t.amount_in(t.totals, t.ended) == D("120.00")
        assert t.compare is None and t.previous is None
        assert t.totals.change is None and t.changed == () and t.biggest_increase is None
        assert t.current_through is None
        assert t.totals.so_far == D("30.00") and t.totals.so_far_change is None
        # From Oct 15 on, the same days of October are history: they are compared.
        later = insights.trends(db, today=day("2026-11-20"))
        assert later.current_through == day("2026-10-20")
        assert later.totals.so_far_change.previous == D("120.00")
        # The category view says the same.
        one = insights.trends(db, category_id=groceries.id, today=day("2026-11-10"))
        assert one.amount_in(one.selected, one.ended) == D("120.00")
        assert one.selected.change is None and one.selected.so_far_change is None

    def test_a_history_starting_on_the_first_has_no_month_before_it(self, db, cat, spend):
        spend(cat("Home"), "40.00", "2026-10-01")
        spend(cat("Home"), "10.00", "2026-11-03")
        t = insights.trends(db, today=day("2026-11-10"))
        assert [m.status for m in t.months] == [insights.COMPLETE, insights.CURRENT]
        assert t.ended.period == OCT and t.compare is None and t.totals.change is None
        assert t.complete_months == 1 and t.average == D("40.00")
        assert t.current_through == day("2026-10-10")  # Oct 1–10 are history
        assert t.totals.so_far_change.previous == D("40.00")

    def test_the_first_month_of_history_has_nothing_to_compare(self, db, cat, spend):
        spend(cat("Home"), "40.00", "2026-11-02")
        t = insights.trends(db, today=day("2026-11-05"))
        assert [m.status for m in t.months] == ["current"]
        assert t.compare is None and t.previous is None and t.changed == ()
        assert t.ended is None
        assert t.average is None and t.totals.so_far_change is None

    def test_a_range_wholly_before_the_history(self, db, cat, spend):
        spend(cat("Home"), "40.00", "2026-06-15")
        t = insights.trends(db, through=day("2025-12-01"), today=day("2026-08-10"))
        assert not t.range_has_data and t.has_data
        assert all(m.total is None for m in t.months) and t.categories == ()

    def test_months_without_spending_are_zero(self, db, cat, spend):
        spend(cat("Home"), "40.00", "2026-06-15")
        spend(cat("Home"), "30.00", "2026-08-15")
        t = insights.trends(db, today=day("2026-09-10"))
        july = next(m for m in t.months if m.period.start.month == 7)
        assert july.total == D("0.00") and july.change.kind == insights.GONE

    def test_empty_database(self, db):
        t = insights.trends(db, today=day("2026-11-05"))
        assert not t.has_data and not t.range_has_data
        assert t.history_start is None and t.categories == ()
        assert len(t.months) == 12 and all(m.status == insights.BEFORE for m in t.months)
        assert t.choices[0] == NOV and len(t.choices) == 12

    def test_hidden_categories_count_and_are_listed(self, db, cat, spend):
        hidden = make_category(db, "Side business", is_hidden=True)
        spend(cat("Home"), "1.00", "2026-09-01")
        spend(hidden, "80.00", "2026-10-04")
        t = insights.trends(db, today=day("2026-11-05"))
        assert [(c.category.name, c.category.is_hidden) for c in t.categories] == [
            ("Side business", True),
            ("Home", False),
        ]
        assert t.totals.change.current == D("80.00")

    def test_pending_follows_the_budget_setting(self, db, cat, spend):
        spend(cat("Home"), "1.00", "2026-09-01")
        spend(cat("Home"), "10.00", "2026-10-04")
        spend(cat("Home"), "7.00", "2026-10-05", pending=True)
        assert insights.trends(db, today=day("2026-11-05")).totals.change.current == D("17.00")
        set_app_settings(db, include_pending_in_budgets=False)
        t = insights.trends(db, today=day("2026-11-05"))
        assert t.totals.change.current == D("10.00") and not t.include_pending

    def test_always_calendar_months_by_purchase_date(self, db, account, cat, spend):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        make_statement(db, account, "2026-10-27")
        spend(cat("Home"), "60.00", "2026-11-02", authorized_date=day("2026-10-30"))
        t = insights.trends(db, today=day("2026-11-05"))
        assert t.months[-2].period == OCT and t.months[-2].total == D("60.00")

    def test_spans_and_through(self, db, history):
        today = day("2026-11-05")
        assert len(insights.trends(db, span=24, today=today).months) == 13  # trimmed to Nov 2025
        assert insights.trends(db, span=7, today=today).span == 12  # not offered
        future = insights.trends(db, through=day("2027-03-01"), today=today)
        assert future.through == NOV
        assert insights.trends(db, through=day("2026-05-20"), today=today).through == (
            month_period(2026, 5)
        )
        choices = insights.trends(db, today=today).choices
        assert choices[0] == NOV and choices[-1] == month_period(2025, 11)
        old = insights.trends(db, through=day("2024-02-01"), today=today)
        assert old.choices[-1] == month_period(2024, 2)  # the month shown stays selectable

    def test_one_category(self, db, history):
        t = insights.trends(db, category_id=history["travel"].id, today=day("2026-11-05"))
        assert t.selected_ref.name == "Travel"
        aug = [m.period.start.month for m in t.months].index(8)
        assert t.selected.amounts[aug] == D("900.00")
        assert t.selected.changes[aug].kind == insights.NEW
        assert t.selected.changes[aug + 1].kind == insights.GONE
        assert t.selected.counts[aug] == 1

    def test_one_category_without_spending_in_range(self, db, cat, history):
        education = cat("Education")
        t = insights.trends(db, category_id=education.id, today=day("2026-11-05"))
        assert t.selected_ref.name == "Education" and t.selected.total == 0

    def test_a_category_that_isnt_spending_or_doesnt_exist(self, db, cat, history):
        t = insights.trends(db, category_id=cat("Credits & Rewards").id, today=day("2026-11-05"))
        assert t.selected is None and t.selected_ref is None
        assert insights.trends(db, category_id=987654, today=day("2026-11-05")).selected is None

    def test_chart_series_fold_the_tail_into_one(self, db, cat, spend):
        names = ["Groceries", "Dining & Drinks", "Home", "Shopping", "Travel", "Education",
                 "Entertainment", "Personal Care", "Utilities & Bills"]  # fmt: skip
        for i, name in enumerate(names):
            spend(cat(name), str(100 - i), "2026-10-04")
        t = insights.trends(db, today=day("2026-11-05"))
        series = t.chart_series()
        assert len(series) == insights.CHART_SERIES
        assert [s.label for s in series[:6]] == names[:6]
        assert series[-1].category is None and series[-1].label == "3 more categories"
        assert series[-1].color == "other"
        assert series[-1].amounts[-2] == D(92 + 93 + 94)
        assert [c.color for c in [series[0]]] == [cat("Groceries").color]  # colour follows entity


# --------------------------------------------------------------------------- income (wave 3)


def income_category(db, name: str):
    """An income category by name: MONEY's migration seeds Paycheck, Interest and Other Income
    (W3-PLAN C2); without it the test creates the category."""
    try:
        category = get_category(db, name)
    except LookupError:
        category = make_category(db, name, kind="income", color="other")
    assert category.kind == "income"
    return category


@pytest.fixture
def bank(db):
    return make_bank_item(db, institution_id="ins_109508", institution_name="First Platypus Bank")


@pytest.fixture
def checking(db, bank):
    return make_bank_account(db, bank, name="Plaid Checking", mask="0000")


@pytest.fixture
def paycheck(db):
    return income_category(db, "Paycheck")


class TestIncomeSwitch:
    def test_offered_once_money_is_booked_to_an_income_category(self, db, cat, spend):
        spend(cat("Groceries"), "40.00", "2026-10-02")
        switch = insights.income_switch(db)
        assert (switch.available, switch.on, switch.showing) == (False, True, False)
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-03")  # an Amex statement credit
        assert insights.income_switch(db) == insights.IncomeSwitch(available=True, on=True)
        assert insights.income_switch(db).showing

    def test_the_choice_is_saved(self, db, cat, spend):
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-03")
        switch = insights.set_show_income(db, False)
        assert (switch.available, switch.on, switch.showing) == (True, False, False)
        assert get_app_settings(db).show_income is False
        assert insights.set_show_income(db, True).showing

    def test_suspect_duplicates_and_income_kinds_only(self, db, cat):
        manual = make_account(db, manual=True)
        make_transaction(db, manual, source="csv", amount="-25.00", date="2026-10-03",
                         category=cat("Credits & Rewards"), duplicate_status="suspect")  # fmt: skip
        make_transaction(db, manual, source="csv", amount="-500.00", date="2026-10-03",
                         category=card_payments(db))  # a transfer, not income  # fmt: skip
        assert not insights.has_income(db)

    def test_a_split_part_in_an_income_category_counts(self, db, cat, spend):
        txn = spend(cat("Shopping"), "120.00", "2026-10-06")
        make_split(db, txn, cat("Shopping"), "100.00")
        make_split(db, txn, cat("Credits & Rewards"), "20.00")
        assert insights.has_income(db)


class TestDashboardIncome:
    def test_income_and_net_this_period(self, db, cat, spend, checking, paycheck):
        travel = cat("Travel")
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
        spend(cat("Groceries"), "300.00", "2026-10-03")
        spend(travel, "400.00", "2026-10-05")  # annual budget: not in the target's Spent
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-06")
        make_transaction(db, checking, amount="-2400.00", category=paycheck, date="2026-10-09")
        make_transaction(db, checking, amount="-2400.00", category=paycheck, date="2026-09-25")
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert d.income_switch.showing
        inc = d.income
        assert inc.income == spending.period_income(db, OCT)
        assert (inc.total, inc.spent, inc.net) == (D("2425.00"), D("700.00"), D("1725.00"))
        assert inc.spent == d.overview.spending.total  # all spending, annual included
        assert inc.largest.name == "Paycheck"
        assert inc.category_ids == tuple(sorted((paycheck.id, cat("Credits & Rewards").id)))
        assert not inc.cards_only  # a bank account is linked

    @pytest.mark.parametrize("hidden", [False, True])
    def test_card_credits_only_until_a_bank_account_exists(self, db, cat, spend, bank, hidden):
        """Without a bank account the only income is card credits, which the page says next to
        the net; any bank account ends that, a hidden one too (its money still counts)."""
        spend(cat("Groceries"), "300.00", "2026-10-03")
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-06")
        inc = insights.dashboard(db, today=day("2026-10-20")).income
        assert inc.cards_only and (inc.total, inc.net) == (D("25.00"), D("-275.00"))
        assert insights.has_bank_account(db) is False
        make_bank_account(db, bank, subtype="savings", name="Plaid Saving", is_hidden=hidden)
        assert insights.has_bank_account(db) is True
        assert not insights.dashboard(db, today=day("2026-10-20")).income.cards_only

    def test_switched_off_or_no_income(self, db, cat, spend):
        spend(cat("Groceries"), "300.00", "2026-10-03")
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert d.income is None and not d.income_switch.available
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-06")
        insights.set_show_income(db, False)
        d = insights.dashboard(db, today=day("2026-10-20"))
        assert d.income is None and d.income_switch == insights.IncomeSwitch(True, False)

    def test_no_income_this_period_is_zero(self, db, cat, spend):
        spend(cat("Credits & Rewards"), "-25.00", "2026-09-06")
        spend(cat("Home"), "80.00", "2026-10-02")
        inc = insights.dashboard(db, today=day("2026-10-20")).income
        assert (inc.total, inc.net, inc.largest, inc.category_ids) == (0, D("-80.00"), None, ())

    def test_statement_cycles_count_income_by_posting_date(self, db, account, cat, checking,
                                                           paycheck):  # fmt: skip
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        make_statement(db, account, "2026-09-27")
        make_statement(db, account, "2026-10-27")
        # Bought on the old cycle's last day, posted in this one: this cycle counts it.
        make_transaction(db, checking, amount="-900.00", category=paycheck, date="2026-10-28",
                         authorized_date=day("2026-10-27"))  # fmt: skip
        make_transaction(db, checking, amount="-50.00", category=paycheck, date="2026-10-20")
        inc = insights.dashboard(db, today=day("2026-11-02")).income
        assert inc.income.basis == spending.BASIS_POSTED_DATE
        assert inc.total == D("900.00")

    def test_pending_income_follows_the_budget_setting(self, db, checking, paycheck):
        make_transaction(db, checking, amount="-2400.00", category=paycheck, date="2026-10-16",
                         pending=True)  # fmt: skip
        assert insights.dashboard(db, today=day("2026-10-20")).income.total == D("2400.00")
        set_app_settings(db, include_pending_in_budgets=False)
        assert insights.dashboard(db, today=day("2026-10-20")).income.total == 0


class TestAccountBalances:
    def test_banks_then_cards_with_cash_and_owed(self, db, bank, freeze_today):
        now = dt.datetime(2026, 10, 20, 12, tzinfo=dt.UTC)
        freeze_today("2026-10-20", now=now)
        checking = make_bank_account(db, bank, name="Plaid Checking", mask="0000",
                                     current_balance=D("110.00"),
                                available_balance=D("100.00"),
                                balances_as_of=now - dt.timedelta(hours=2))  # fmt: skip
        savings = make_bank_account(db, bank, name="Plaid Saving", mask="1111",
                                    subtype="savings", current_balance=D("210.00"),
                               balances_as_of=now - dt.timedelta(hours=5))  # fmt: skip
        card = make_account(db, name="Blue Cash Preferred®", mask="3005",
                            current_balance=D("1234.56"), balances_as_of=now)  # fmt: skip
        credit = make_account(db, bank, name="Plaid Credit Card", mask="3333",
                              current_balance=D("-12.00"))  # a credit in your favour  # fmt: skip
        manual = make_account(db, manual=True)  # an Amex CSV account: no balance
        make_bank_account(db, bank, name="Hidden", current_balance=D("999.00"), is_hidden=True)
        # A loan is never linked by this app (filtered in Plaid Link) and never shown.
        make_account(db, bank, name="Plaid Student Loan", type="loan", subtype="student",
                     current_balance=D("65000.00"))  # fmt: skip
        a = insights.account_balances(db)
        assert [x.id for x in a.banks] == [checking.id, savings.id]
        assert [x.id for x in a.cards] == [card.id, credit.id, manual.id]
        assert (a.cash, a.owed) == (D("320.00"), D("1222.56"))
        assert (a.banks_without_balance, a.cards_without_balance) == (0, 1)
        assert a.as_of == now - dt.timedelta(hours=5)  # the oldest balance shown
        first = a.banks[0]
        assert (first.name, first.mask, first.institution, first.is_card) == (
            "Plaid Checking", "0000", "First Platypus Bank", False,
        )  # fmt: skip
        assert (first.current, first.available) == (D("110.00"), D("100.00"))
        assert a.cards[0].institution == "American Express"
        assert a.cards[2].is_manual and a.cards[2].institution is None
        assert a.any

    def test_no_accounts(self, db):
        a = insights.account_balances(db)
        assert not a.any and (a.cash, a.owed, a.as_of) == (None, None, None)

    def test_a_total_with_no_balance_to_add_up_is_unknown(self, db, bank):
        """ux-2: a group whose accounts have no balance (an Amex CSV account, a bank account
        before its first sync) has no total, not $0.00; once one has a balance the total is
        that balance and the others are counted as left out."""
        manual = make_account(db, manual=True)
        checking = make_bank_account(db, bank, name="Plaid Checking", mask="0000")
        a = insights.account_balances(db)
        assert (a.cash, a.owed) == (None, None)
        assert (a.banks_without_balance, a.cards_without_balance) == (1, 1)
        assert [x.id for x in a.cards] == [manual.id] and a.cards[0].current is None
        checking.current_balance = D("0.00")  # a known zero is a total
        make_account(db, name="Platinum Card", current_balance=D("12.34"))
        db.flush()
        a = insights.account_balances(db)
        assert (a.cash, a.owed) == (D("0.00"), D("12.34"))
        assert (a.banks_without_balance, a.cards_without_balance) == (0, 1)

    def test_a_group_of_accounts_plaid_no_longer_shares_has_no_total(self, db, bank):
        card = make_account(db, bank, name="Plaid Credit Card", current_balance=D("410.00"))
        assert sync_service.mark_not_shared(card, dt.datetime(2026, 10, 20, tzinfo=dt.UTC))
        db.flush()
        a = insights.account_balances(db)
        assert a.cards[0].no_longer_shared and a.owed is None and a.cards_without_balance == 0

    def test_the_dashboard_carries_them(self, db, account):
        assert insights.dashboard(db, today=day("2026-10-20")).accounts.cards[0].id == account.id


class TestTrendsIncome:
    @pytest.fixture
    def earnings(self, db, cat, spend, checking, paycheck, history):
        """Paychecks from Dec 2025; interest in Sep and Oct; an Amex credit in Oct. The checking
        account's history starts with the card's (Nov 10, 2025: a card payment, neither spending
        nor income), so every month of the history has income data."""
        make_transaction(db, checking, amount="100.00", category=card_payments(db),
                         date="2025-11-10", name="AMEX PAYMENT")  # fmt: skip
        interest = income_category(db, "Interest")
        for month in (12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            year = 2025 if month == 12 else 2026
            make_transaction(db, checking, amount="-2000.00", category=paycheck,
                             date=f"{year}-{month:02d}-15")  # fmt: skip
        make_transaction(db, checking, amount="-500.00", category=paycheck, date="2026-10-30")
        make_transaction(db, checking, amount="-3.00", category=interest, date="2026-09-30")
        make_transaction(db, checking, amount="-4.00", category=interest, date="2026-10-31")
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-12")
        make_transaction(db, checking, amount="-2000.00", category=paycheck, date="2026-11-01",
                         pending=True)  # fmt: skip
        return {"paycheck": paycheck, "interest": interest, "credits": cat("Credits & Rewards")}

    def test_off_by_default(self, db, earnings):
        assert insights.trends(db, today=day("2026-11-05")).income is None

    def test_income_and_net_per_month(self, db, earnings):
        t = insights.trends(db, today=day("2026-11-05"), income=True)
        inc = t.income
        assert [m.period.start for m in t.months][0] == day("2025-12-01")
        assert inc.totals.amounts == (
            *(D("2000.00"),) * 9,
            D("2003.00"),
            D("2529.00"),
            D("2000.00"),
        )  # Dec … Aug, Sep (+ interest), Oct (+ a second paycheck, interest, credit), Nov
        assert [line.category.name for line in inc.categories] == [
            "Paycheck", "Credits & Rewards", "Interest",
        ]  # fmt: skip
        paycheck = inc.categories[0]
        assert paycheck.total == D("24500.00") and paycheck.counts[-2] == 2
        assert inc.net == tuple(
            earned - spent
            for earned, spent in zip(inc.totals.amounts, t.totals.amounts, strict=True)
        )
        assert inc.net[-2] == D("2529.00") - D("205.00")
        # Averages count complete months only (Dec – Oct), like spending's.
        assert inc.totals.average == D("2048.36")
        assert inc.net_average == to_money(
            sum(n for n, m in zip(inc.net, t.months, strict=True) if m.status == insights.COMPLETE)
            / 11
        )
        assert inc.change == insights.change(D("2529.00"), D("2003.00"))
        assert inc.category_ids == tuple(sorted(c.id for c in earnings.values()))
        assert inc.largest_in(len(t.months) - 2).category.name == "Paycheck"
        assert t.amount_in(t.totals, t.ended) == D("205.00")  # spending is unchanged
        assert not inc.cards_only  # a bank account is linked

    def test_card_credits_only_until_a_bank_account_exists(self, db, cat, spend, history, bank):
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-12")
        t = insights.trends(db, today=day("2026-11-05"), income=True)
        assert t.income.cards_only and t.income.totals.amounts[-2] == D("25.00")
        assert (t.income.starts, t.income.partial) == (None, False)  # card credits: every month
        make_bank_account(db, bank, name="Plaid Checking", is_hidden=True)
        assert not insights.trends(db, today=day("2026-11-05"), income=True).income.cards_only

    def test_pending_income_follows_the_budget_setting(self, db, earnings):
        assert insights.trends(db, today=day("2026-11-05"), income=True).income.totals.amounts[
            -1
        ] == D("2000.00")
        set_app_settings(db, include_pending_in_budgets=False)
        t = insights.trends(db, today=day("2026-11-05"), income=True)
        assert t.income.totals.amounts[-1] == D("0.00")

    def test_months_before_the_history_have_no_income(self, db, cat, spend, checking, paycheck):
        spend(cat("Home"), "40.00", "2026-06-15")
        # The checking account's history starts the same day (a card payment: neither).
        make_transaction(db, checking, amount="40.00", category=card_payments(db),
                         date="2026-06-15", name="AMEX PAYMENT")  # fmt: skip
        make_transaction(db, checking, amount="-1000.00", category=paycheck, date="2026-07-15")
        t = insights.trends(db, through=day("2026-07-01"), today=day("2026-08-10"), income=True)
        assert [m.status for m in t.months] == [insights.FIRST, insights.COMPLETE]
        assert t.income.totals.amounts == (D("0.00"), D("1000.00"))
        assert t.income.net == (D("-40.00"), D("1000.00"))
        assert t.income.change.kind == insights.NEW  # July against June: nothing came in then
        june = insights.trends(db, through=day("2026-06-01"), today=day("2026-08-10"), income=True)
        assert june.income.change is None  # the history's first month has none before it
        early = insights.trends(db, through=day("2025-12-01"), today=day("2026-08-10"),
                                income=True)  # fmt: skip
        assert set(early.income.totals.amounts) == {None} and set(early.income.net) == {None}
        assert early.income.categories == () and early.income.net_average is None

    @pytest.mark.parametrize("hidden", [False, True])
    def test_months_before_the_bank_history_have_no_income(
        self, db, cat, spend, bank, paycheck, hidden
    ):
        """ux-3: the card's history starts in January, the bank's (where paychecks arrive) on
        Apr 15, a hidden account's too. January to March have no income data: no income and no
        net (not −all spending), and none of their card credits; April is the income's partial
        first month; averages, complete months and the change count May only."""
        checking = make_bank_account(db, bank, name="Plaid Checking", is_hidden=hidden)
        for month in range(1, 7):
            spend(cat("Groceries"), "900.00", f"2026-{month:02d}-10")
        spend(cat("Credits & Rewards"), "-25.00", "2026-02-12")  # before the bank's history
        for month in range(4, 7):
            make_transaction(db, checking, amount="-3000.00", category=paycheck,
                             date=f"2026-{month:02d}-15")  # fmt: skip
        t = insights.trends(db, through=day("2026-06-01"), today=day("2026-06-20"), income=True)
        inc = t.income
        assert [m.period.start.month for m in t.months] == [1, 2, 3, 4, 5, 6]
        assert [m.status for m in t.months] == [insights.FIRST, *[insights.COMPLETE] * 4,
                                                insights.CURRENT]  # fmt: skip
        assert inc.totals.amounts == (None, None, None, *(D("3000.00"),) * 3)
        assert inc.net == (None, None, None, *(D("2100.00"),) * 3)
        assert (inc.starts, inc.partial) == (day("2026-04-15"), True)
        assert inc.complete_months == 1 and t.complete_months == 4
        assert (inc.totals.average, inc.net_average) == (D("3000.00"), D("2100.00"))
        assert (inc.totals.total, inc.net_total) == (D("9000.00"), D("6300.00"))
        assert inc.change == insights.change(D("3000.00"), D("3000.00"))  # May against April
        assert [line.category.name for line in inc.categories] == ["Paycheck"]
        assert not inc.cards_only
        assert t.totals.amounts[:3] == (D("900.00"),) * 3  # the spending months stay

        # Before the bank's history: no income at all.
        march = insights.trends(db, through=day("2026-03-01"), today=day("2026-06-20"),
                                income=True)  # fmt: skip
        assert set(march.income.totals.amounts) == {None} and set(march.income.net) == {None}
        assert (march.income.totals.total, march.income.net_total) == (None, None)
        assert march.income.partial and march.income.categories == ()

    def test_a_bank_account_whose_history_hasnt_arrived(self, db, cat, spend, bank, history):
        spend(cat("Credits & Rewards"), "-25.00", "2026-10-12")
        make_bank_account(db, bank, name="Plaid Checking")  # linked, no transactions yet
        inc = insights.trends(db, today=day("2026-11-05"), income=True).income
        assert set(inc.totals.amounts) == {None} and set(inc.net) == {None}
        assert (inc.starts, inc.partial, inc.cards_only) == (None, True, False)
        assert inc.net_average is None and inc.change is None and inc.complete_months == 0

    def test_income_alone_is_history_too(self, db, checking, paycheck):
        make_transaction(db, checking, amount="-1000.00", category=paycheck, date="2026-10-15")
        t = insights.trends(db, today=day("2026-11-05"), income=True)
        assert t.has_data and t.categories == ()
        assert t.income.totals.amounts == (D("1000.00"), D("0.00"))
        assert t.income.net == (D("1000.00"), D("0.00"))

    def test_largest_in_a_month_without_income(self, db, earnings):
        t = insights.trends(db, through=day("2026-11-01"), today=day("2026-11-05"), income=True)
        set_app_settings(db, include_pending_in_budgets=False)
        quiet = insights.trends(db, today=day("2026-11-05"), income=True)
        assert quiet.income.largest_in(len(quiet.months) - 1) is None
        assert t.income.largest_in(len(t.months) - 1).category.name == "Paycheck"
