"""Budgets service (BUD-1..5): periods, spending, versioning, rollover, overall target, annual
budgets and suggestions (ARCHITECTURE §5–§6)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp.models import Budget
from budgetapp.periods import month_period, year_period
from budgetapp.services import budgets
from budgetapp.services.budgets import (
    ANNUAL,
    MONTHLY,
    BudgetError,
    annual_overview,
    current_period,
    overview,
    set_budget,
    set_rollover,
    suggest,
    suggestions,
)
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
TODAY = dt.date(2026, 10, 15)
OCT = month_period(2026, 10)
SEP = month_period(2026, 9)
AUG = month_period(2026, 8)
NOV = month_period(2026, 11)


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


@pytest.fixture
def account(db):
    return make_account(db)


@pytest.fixture
def cats(db):
    names = (
        "Groceries",
        "Dining & Drinks",
        "Home",
        "Travel",
        "Shopping",
        "Personal Care",
        "Gifts & Donations",
        "Credits & Rewards",
    )
    return {name.split()[0].lower(): get_category(db, name) for name in names}


@pytest.fixture
def spend(db, account):
    """``spend(category, amount, "2026-10-03", **kwargs)``: a posted charge (or refund)."""

    def make(category, amount, date, **kwargs):
        return make_transaction(db, account, amount=amount, category=category, date=date, **kwargs)

    return make


def statement_mode(db, account, *closings: str, closing_day: int | None = None):
    """Budget periods follow ``account``'s statement cycles."""
    account.statement_closing_day = closing_day
    for closing in closings:
        make_statement(db, account, closing)
    set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)


def line(result, category):
    found = result.line_for(category.id)
    assert found is not None, f"no budget line for {category.name}"
    return found


def rows(db, category=None, kind=None):
    query = select(Budget).order_by(Budget.effective_from, Budget.id)
    if category is not None:
        query = query.where(Budget.category_id == category.id)
    if kind is not None:
        query = query.where(Budget.period == kind)
    return [(b.period, b.effective_from, b.amount, b.rollover) for b in db.scalars(query)]


# --------------------------------------------------------------------------- periods (BUD-2)


class TestPeriods:
    def test_calendar_mode_uses_calendar_months(self, db):
        assert current_period(db, today=TODAY) == OCT
        assert budgets.period_for(db, day("2026-02-28")) == month_period(2026, 2)
        assert budgets.previous_period(db, OCT) == SEP
        assert budgets.next_period(db, OCT) == NOV

    def test_statement_mode_uses_the_budget_cards_cycles(self, db, account):
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        period = current_period(db, today=TODAY)
        assert (period.start, period.end, period.kind) == (
            day("2026-09-28"),
            day("2026-10-27"),
            "statement",
        )
        previous = budgets.previous_period(db, period)
        assert (previous.start, previous.end, previous.generated) == (
            day("2026-08-28"),
            day("2026-09-27"),
            False,
        )
        following = budgets.next_period(db, period)
        assert (following.start, following.end) == (day("2026-10-28"), day("2026-11-27"))
        result = overview(db, period, today=TODAY)
        assert result.mode == "statement" and result.notice is None
        assert result.days_left == 13

    def test_statement_mode_with_only_a_closing_day(self, db, account):
        statement_mode(db, account, closing_day=5)
        period = current_period(db, today=TODAY)
        assert (period.start, period.end) == (day("2026-10-06"), day("2026-11-05"))

    def test_statement_mode_without_a_card_falls_back_to_calendar_months(self, db):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=None)
        assert current_period(db, today=TODAY) == OCT
        result = overview(db, OCT, today=TODAY)
        assert result.mode == "calendar"
        assert result.settings.mode == "statement"
        assert result.notice == budgets.NOTICE_NO_CARD

    def test_statement_mode_without_closing_dates_falls_back_and_names_the_card(self, db, account):
        statement_mode(db, account)
        assert current_period(db, today=TODAY) == OCT
        notice = overview(db, OCT, today=TODAY).notice
        assert notice == budgets.notice_no_closing_date(account.display_name)
        assert account.display_name in notice

    def test_the_earliest_statements_start_override_is_used(self, db, account):
        account.statement_closing_day = 27
        make_statement(db, account, "2026-09-27", period_start_override=day("2026-09-01"))
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        period = budgets.period_for(db, day("2026-09-10"))
        assert (period.start, period.end) == (day("2026-09-01"), day("2026-09-27"))

    def test_days_left_counts_today(self, db):
        assert overview(db, OCT, today=day("2026-10-31")).days_left == 1
        assert overview(db, OCT, today=day("2026-10-01")).days_left == 31
        assert overview(db, SEP, today=TODAY).days_left == 0
        assert overview(db, NOV, today=TODAY).days_left == 30

    def test_pace_is_the_share_of_the_period_elapsed_counting_today(self, db):
        assert overview(db, OCT, today=day("2026-10-31")).pace_percent == D("100.00")
        assert overview(db, OCT, today=TODAY).pace_percent == D("48.39")  # 15 / 31
        assert overview(db, SEP, today=TODAY).pace_percent is None  # past
        assert overview(db, NOV, today=TODAY).pace_percent is None  # future

    def test_past_current_and_future(self, db):
        assert overview(db, OCT, today=TODAY).is_current
        assert overview(db, SEP, today=TODAY).is_past
        assert overview(db, NOV, today=TODAY).is_future


# --------------------------------------------------------------------------- spending (BUD-1)


class TestSpent:
    def test_spent_remaining_and_percent_with_refunds(self, db, cats, spend):
        make_budget(db, cats["groceries"], "500.00")
        spend(cats["groceries"], "300.00", "2026-10-03")
        spend(cats["groceries"], "-50.00", "2026-10-04")
        result = line(overview(db, OCT, today=TODAY), cats["groceries"])
        assert (result.budget, result.available, result.spent, result.remaining) == (
            D("500.00"),
            D("500.00"),
            D("250.00"),
            D("250.00"),
        )
        assert (result.percent_used, result.status) == (D("50.00"), "ok")
        assert result.carry_in == D("0.00") and result.rollover is False

    def test_card_payments_and_credits_never_count(self, db, cats, spend):
        make_budget(db, cats["groceries"], "500.00")
        spend(card_payments(db), "-1200.00", "2026-10-05")
        spend(cats["credits"], "-25.00", "2026-10-05")
        spend(cats["groceries"], "40.00", "2026-10-05")
        result = overview(db, OCT, today=TODAY)
        assert line(result, cats["groceries"]).spent == D("40.00")
        assert result.totals.spent == D("40.00")
        assert result.unbudgeted == ()

    def test_splits_count_in_each_category(self, db, cats, spend):
        make_budget(db, cats["groceries"], "200.00")
        make_budget(db, cats["home"], "100.00")
        txn = spend(cats["home"], "90.00", "2026-10-06")
        make_split(db, txn, cats["groceries"], "60.00")
        make_split(db, txn, cats["home"], "30.00")
        result = overview(db, OCT, today=TODAY)
        assert line(result, cats["groceries"]).spent == D("60.00")
        assert line(result, cats["home"]).spent == D("30.00")

    def test_pending_charges_follow_the_setting(self, db, cats, spend):
        make_budget(db, cats["dining"], "100.00")
        spend(cats["dining"], "30.00", "2026-10-07", pending=True)
        spend(cats["dining"], "10.00", "2026-10-07")
        assert line(overview(db, OCT, today=TODAY), cats["dining"]).spent == D("40.00")
        set_app_settings(db, include_pending_in_budgets=False)
        assert line(overview(db, OCT, today=TODAY), cats["dining"]).spent == D("10.00")

    def test_calendar_and_statement_bases(self, db, cats, account, spend):
        """A charge authorized Oct 31 that posts Nov 2: October in calendar mode, the statement
        containing Nov 2 in statement mode."""
        make_budget(db, cats["home"], "100.00")
        spend(
            cats["home"],
            "40.00",
            "2026-11-02",
            authorized_date=day("2026-10-31"),
            posted_date=day("2026-11-02"),
        )
        assert line(overview(db, OCT, today=TODAY), cats["home"]).spent == D("40.00")
        assert line(overview(db, NOV, today=TODAY), cats["home"]).spent == D("0.00")

        statement_mode(db, account, "2026-10-01", closing_day=1)
        cycle = budgets.period_for(db, day("2026-10-31"))
        assert (cycle.start, cycle.end) == (day("2026-10-02"), day("2026-11-01"))
        assert line(overview(db, cycle, today=TODAY), cats["home"]).spent == D("0.00")
        containing = budgets.period_for(db, day("2026-11-02"))
        assert (containing.start, containing.end) == (day("2026-11-02"), day("2026-12-01"))
        assert line(overview(db, containing, today=TODAY), cats["home"]).spent == D("40.00")

    def test_status_thresholds_round_toward_the_status(self, db, cats, spend):
        cases = {
            "groceries": ("79.99", D("79.99"), "ok"),
            "dining": ("80.00", D("80.00"), "warn"),
            "home": ("100.00", D("100.00"), "warn"),
            "shopping": ("100.01", D("100.01"), "over"),
        }
        for key, (spent, _, _) in cases.items():
            make_budget(db, cats[key], "100.00")
            spend(cats[key], spent, "2026-10-08")
        make_budget(db, cats["personal"], "300.00")
        spend(cats["personal"], "239.99", "2026-10-08")  # 79.996…%: still ok, shown as 79.99
        result = overview(db, OCT, today=TODAY)
        for key, (_, percent, status) in cases.items():
            assert (line(result, cats[key]).percent_used, line(result, cats[key]).status) == (
                percent,
                status,
            ), key
        personal = line(result, cats["personal"])
        assert (personal.percent_used, personal.status) == (D("79.99"), "ok")

    def test_lines_are_sorted_by_percent_used_then_name(self, db, cats, spend):
        for key, budget, spent in (
            ("groceries", "100.00", "50.00"),
            ("dining", "100.00", "90.00"),
            ("home", "200.00", "100.00"),
            ("shopping", "0.00", "5.00"),
            ("personal", "0.00", None),
        ):
            make_budget(db, cats[key], budget)
            if spent:
                spend(cats[key], spent, "2026-10-09")
        names = [entry.category.name for entry in overview(db, OCT, today=TODAY).lines]
        assert names == ["Shopping", "Dining & Drinks", "Groceries", "Home", "Personal Care"]

    def test_zero_budgets(self, db, cats, spend):
        make_budget(db, cats["groceries"], "0.00")
        make_budget(db, cats["home"], "0.00")
        spend(cats["home"], "12.00", "2026-10-09")
        result = overview(db, OCT, today=TODAY)
        idle = line(result, cats["groceries"])
        assert (idle.percent_used, idle.status, idle.remaining) == (None, "ok", D("0.00"))
        used = line(result, cats["home"])
        assert (used.percent_used, used.status, used.remaining) == (None, "over", D("-12.00"))

    def test_none_budgets_mean_no_budget(self, db, cats, spend):
        make_budget(db, cats["groceries"], "300.00", effective_from=day("2026-01-01"))
        make_budget(db, cats["groceries"], None, effective_from=day("2026-10-01"))
        spend(cats["groceries"], "20.00", "2026-10-09")
        result = overview(db, OCT, today=TODAY)
        assert result.line_for(cats["groceries"].id) is None
        assert [(u.category.name, u.spent) for u in result.unbudgeted] == [
            ("Groceries", D("20.00"))
        ]
        assert line(overview(db, SEP, today=TODAY), cats["groceries"]).budget == D("300.00")

    def test_unbudgeted_categories_with_spending(self, db, cats, spend):
        make_budget(db, cats["groceries"], "300.00")
        spend(cats["dining"], "45.00", "2026-10-02")
        spend(cats["shopping"], "80.00", "2026-10-02")
        spend(cats["home"], "15.00", "2026-10-02")
        spend(cats["home"], "-15.00", "2026-10-03")  # nets to nothing: not listed
        result = overview(db, OCT, today=TODAY)
        assert [(u.category.name, u.spent) for u in result.unbudgeted] == [
            ("Shopping", D("80.00")),
            ("Dining & Drinks", D("45.00")),
        ]
        assert result.totals.unbudgeted_spent == D("125.00")

    def test_hidden_categories_are_left_out_of_lists_but_still_count(self, db, cats, spend):
        make_budget(db, cats["home"], "100.00")
        cats["home"].is_hidden = True
        cats["shopping"].is_hidden = True
        spend(cats["home"], "60.00", "2026-10-02")
        spend(cats["shopping"], "30.00", "2026-10-02")
        make_budget(db, None, "1000.00")
        result = overview(db, OCT, today=TODAY)
        assert result.lines == () and result.unbudgeted == ()
        assert result.totals.hidden_spent == D("90.00")
        assert result.totals.budgeted == D("100.00")
        assert result.overall.spent == D("90.00")

    def test_hidden_unbudgeted_spending_is_reported_apart(self, db, cats, spend):
        """Review finding: the unbudgeted rows leave hidden categories out, so their spending is
        reported separately and the listed total matches the rows."""
        education = get_category(db, "Education")
        education.is_hidden = True
        spend(cats["dining"], "45.00", "2026-10-02")
        spend(education, "300.00", "2026-10-02")
        totals = overview(db, OCT, today=TODAY).totals
        result = overview(db, OCT, today=TODAY)
        assert [(u.category.name, u.spent) for u in result.unbudgeted] == [
            ("Dining & Drinks", D("45.00"))
        ]
        assert (totals.unbudgeted_spent, totals.unbudgeted_hidden_spent) == (
            D("345.00"),
            D("300.00"),
        )
        assert totals.unbudgeted_listed_spent == D("45.00")
        assert totals.hidden_spent == D("300.00")

    def test_an_empty_database(self, db):
        result = overview(db, OCT, today=TODAY)
        assert result.lines == () and result.unbudgeted == ()
        assert result.overall.status == "none" and result.overall.budget is None
        assert result.totals.spent == D("0.00")


# --------------------------------------------------------------------------- versioning (§5)


class TestVersioning:
    def test_an_edit_inserts_at_the_period_start_and_past_periods_keep_their_amounts(
        self, db, cats, freeze_today
    ):
        freeze_today(TODAY)
        make_budget(db, cats["groceries"], "600.00", effective_from=day("2026-01-01"))
        change = set_budget(db, cats["groceries"].id, D("700.00"))
        assert change.changed and change.row.effective_from == OCT.start
        assert line(overview(db, OCT), cats["groceries"]).budget == D("700.00")
        assert line(overview(db, SEP), cats["groceries"]).budget == D("600.00")
        assert line(overview(db, NOV), cats["groceries"]).budget == D("700.00")

    def test_a_second_edit_in_the_same_period_updates_the_same_row(self, db, cats):
        set_budget(db, cats["groceries"].id, D("700.00"), effective_from=OCT)
        set_budget(db, cats["groceries"].id, D("725.50"), effective_from=OCT)
        assert rows(db, cats["groceries"]) == [(MONTHLY, OCT.start, D("725.50"), False)]

    def test_setting_the_same_amount_changes_nothing(self, db, cats):
        make_budget(db, cats["groceries"], "600.00", effective_from=day("2026-01-01"))
        change = set_budget(db, cats["groceries"].id, D("600"), effective_from=OCT)
        assert change.changed is False and change.row.effective_from == day("2026-01-01")
        assert len(rows(db, cats["groceries"])) == 1

    def test_removing_a_budget_applies_from_that_period_on(self, db, cats, spend):
        make_budget(db, cats["groceries"], "600.00", effective_from=day("2026-01-01"))
        spend(cats["groceries"], "50.00", "2026-10-02")
        change = set_budget(db, cats["groceries"].id, None, effective_from=OCT)
        assert change.changed and change.row is None
        assert overview(db, OCT, today=TODAY).line_for(cats["groceries"].id) is None
        assert line(overview(db, SEP, today=TODAY), cats["groceries"]).budget == D("600.00")
        assert set_budget(db, cats["groceries"].id, None, effective_from=OCT).changed is False

    def test_editing_a_past_period_applies_until_the_next_change(self, db, cats):
        make_budget(db, cats["groceries"], "600.00", effective_from=day("2026-01-01"))
        make_budget(db, cats["groceries"], "700.00", effective_from=OCT.start)
        set_budget(db, cats["groceries"].id, D("650.00"), effective_from=AUG)
        amounts = {
            p.label: line(overview(db, p, today=TODAY), cats["groceries"]).budget
            for p in (month_period(2026, 7), AUG, SEP, OCT)
        }
        assert amounts == {
            "Jul 2026": D("600.00"),
            "Aug 2026": D("650.00"),
            "Sep 2026": D("650.00"),
            "Oct 2026": D("700.00"),
        }

    def test_planning_a_future_period(self, db, cats):
        set_budget(db, cats["groceries"].id, D("600.00"), effective_from=OCT)
        set_budget(db, cats["groceries"].id, D("800.00"), effective_from=NOV)
        assert line(overview(db, OCT, today=TODAY), cats["groceries"]).budget == D("600.00")
        assert line(overview(db, NOV, today=TODAY), cats["groceries"]).budget == D("800.00")

    def test_an_exact_date(self, db, cats):
        set_budget(db, cats["groceries"].id, D("10.00"), effective_from=day("2026-10-20"))
        assert rows(db, cats["groceries"]) == [(MONTHLY, day("2026-10-20"), D("10.00"), False)]
        # The lookup uses the latest effective_from <= the period's end.
        assert line(overview(db, OCT, today=TODAY), cats["groceries"]).budget == D("10.00")

    def test_the_overall_target_is_versioned_too(self, db):
        make_budget(db, None, "3000.00", effective_from=day("2026-01-01"))
        set_budget(db, None, D("3200.00"), effective_from=OCT)
        assert overview(db, OCT, today=TODAY).overall.target == D("3200.00")
        assert overview(db, SEP, today=TODAY).overall.target == D("3000.00")
        set_budget(db, None, None, effective_from=NOV)
        assert overview(db, NOV, today=TODAY).overall.has_target is False

    @pytest.mark.parametrize(
        ("amount", "message"),
        [
            (D("-1.00"), "can't be negative"),
            (D("10.005"), "two decimal places"),
            (D("1000000000000.00"), "under $1,000,000,000,000"),
            (D("NaN"), "Enter a number"),
        ],
    )
    def test_invalid_amounts(self, db, cats, amount, message):
        with pytest.raises(BudgetError, match=re.escape(message)):
            set_budget(db, cats["groceries"].id, amount, effective_from=OCT)
        assert rows(db, cats["groceries"]) == []

    def test_only_spending_categories_can_have_budgets(self, db, cats):
        with pytest.raises(BudgetError, match="isn't a spending category"):
            set_budget(db, card_payments(db).id, D("10.00"), effective_from=OCT)
        with pytest.raises(BudgetError, match="isn't a spending category"):
            set_budget(db, cats["credits"].id, D("10.00"), effective_from=OCT)
        with pytest.raises(LookupError):
            set_budget(db, 999_999_999, D("10.00"), effective_from=OCT)
        with pytest.raises(BudgetError, match="overall target is monthly"):
            set_budget(db, None, D("10.00"), effective_from=OCT, period=ANNUAL)
        with pytest.raises(ValueError, match="unknown budget period type"):
            set_budget(db, cats["groceries"].id, D("10.00"), effective_from=OCT, period="weekly")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", None),
            ("   ", None),
            ("650", D("650.00")),
            ("$1,250.5", D("1250.50")),
            ("0", D("0.00")),
        ],
    )
    def test_parse_amount(self, text, expected):
        assert budgets.parse_amount(text) == expected

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("abc", "is not an amount"),
            ("-5", "can't be negative"),
            ("(4.20)", "can't be negative"),
            ("1.234", "two decimal places"),
            ("1000000000000", "under $1,000,000,000,000"),
        ],
    )
    def test_parse_amount_errors_are_written_for_the_user(self, text, message):
        with pytest.raises(BudgetError, match=re.escape(message)):
            budgets.parse_amount(text)

    def test_a_new_monthly_row_keeps_the_rollover_setting(self, db, cats):
        make_budget(db, cats["personal"], "100.00", effective_from=AUG.start, rollover=True)
        change = set_budget(db, cats["personal"].id, D("120.00"), effective_from=OCT)
        assert change.row.rollover is True
        assert line(overview(db, OCT, today=TODAY), cats["personal"]).rollover is True


class TestOneActiveType:
    def test_setting_monthly_ends_the_annual_budget_from_the_same_date(self, db, cats):
        make_budget(db, cats["travel"], "6000.00", effective_from=day("2026-01-01"), period=ANNUAL)
        change = set_budget(db, cats["travel"].id, D("400.00"), effective_from=OCT)
        assert change.ended is not None and change.ended.amount is None
        assert rows(db, cats["travel"], ANNUAL) == [
            (ANNUAL, day("2026-01-01"), D("6000.00"), False),
            (ANNUAL, OCT.start, None, False),
        ]
        assert line(overview(db, OCT, today=TODAY), cats["travel"]).budget == D("400.00")
        assert cats["travel"].id in overview(db, SEP, today=TODAY).annual_category_ids
        assert annual_overview(db, 2026, today=TODAY, as_of=SEP.end).lines[0].budget == D("6000.00")
        assert annual_overview(db, 2026, today=TODAY, as_of=OCT.end).lines == ()

    def test_setting_annual_ends_the_monthly_budget_from_the_same_date(self, db, cats):
        make_budget(db, cats["gifts"], "50.00", effective_from=day("2026-01-01"))
        change = set_budget(db, cats["gifts"].id, D("800.00"), effective_from=OCT, period=ANNUAL)
        assert change.row.period == ANNUAL and change.row.effective_from == OCT.start
        assert rows(db, cats["gifts"], MONTHLY)[-1] == (MONTHLY, OCT.start, None, False)
        assert overview(db, OCT, today=TODAY).line_for(cats["gifts"].id) is None
        assert line(overview(db, SEP, today=TODAY), cats["gifts"]).budget == D("50.00")
        annual = annual_overview(db, 2026, today=TODAY)
        assert [(a.category.name, a.budget) for a in annual.lines] == [
            ("Gifts & Donations", D("800.00"))
        ]

    def test_without_the_other_type_nothing_extra_is_written(self, db, cats):
        change = set_budget(db, cats["gifts"].id, D("800.00"), effective_from=OCT, period=ANNUAL)
        assert change.ended is None
        assert rows(db, cats["gifts"]) == [(ANNUAL, OCT.start, D("800.00"), False)]

    def test_if_both_are_set_the_more_recent_row_wins(self, db, cats):
        # As a category merge could leave them: both types set for the same period.
        make_budget(db, cats["travel"], "300.00", effective_from=day("2026-03-01"))
        make_budget(db, cats["travel"], "5000.00", effective_from=day("2026-06-01"), period=ANNUAL)
        make_budget(db, cats["shopping"], "200.00", effective_from=day("2026-06-01"))
        make_budget(db, cats["shopping"], "900.00", effective_from=day("2026-06-01"), period=ANNUAL)
        result = overview(db, OCT, today=TODAY)
        assert result.line_for(cats["travel"].id) is None
        assert cats["travel"].id in result.annual_category_ids
        assert line(result, cats["shopping"]).budget == D("200.00")  # a tie: monthly
        assert line(overview(db, month_period(2026, 5), today=TODAY), cats["travel"]).budget == D(
            "300.00"
        )


class TestStatementModeEdits:
    def test_a_closing_date_shift_keeps_the_rows_in_their_cycle(self, db, cats, account):
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        cycle = budgets.period_for(db, TODAY)  # Sep 28 – Oct 27 (generated)
        set_budget(db, cats["groceries"].id, D("700.00"), effective_from=cycle)
        # A cycle's new row starts on its middle day, not on its (movable) first day.
        assert rows(db, cats["groceries"]) == [(MONTHLY, day("2026-10-12"), D("700.00"), False)]
        # The statement arrives and closes two days early: the cycle is now Sep 28 – Oct 25.
        make_statement(db, account, "2026-10-25")
        shifted = budgets.period_for(db, TODAY)
        assert (shifted.start, shifted.end) == (day("2026-09-28"), day("2026-10-25"))
        assert line(overview(db, shifted, today=TODAY), cats["groceries"]).budget == D("700.00")
        set_budget(db, cats["groceries"].id, D("720.00"), effective_from=shifted)
        assert rows(db, cats["groceries"]) == [(MONTHLY, day("2026-10-12"), D("720.00"), False)]
        following = budgets.next_period(db, shifted)
        assert following.start == day("2026-10-26")
        assert line(overview(db, following, today=TODAY), cats["groceries"]).budget == D("720.00")
        before = budgets.previous_period(db, shifted)
        assert before.end == day("2026-09-27")
        assert overview(db, before, today=TODAY).line_for(cats["groceries"].id) is None

    def test_a_planned_cycle_stays_out_of_the_current_one_when_the_closing_date_is_later(
        self, db, cats, account
    ):
        """Review finding: next cycle's budget, planned while its start was generated, must not
        reach back into the current cycle when the statement then closes two days later."""
        groceries = cats["groceries"]
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        make_budget(db, groceries, "700.00", effective_from=day("2026-01-01"))
        current = budgets.current_period(db, today=TODAY)
        upcoming = budgets.next_period(db, current)
        assert (upcoming.start, upcoming.end) == (day("2026-10-28"), day("2026-11-27"))
        set_budget(db, groceries.id, D("900.00"), effective_from=upcoming, today=TODAY)
        make_statement(db, account, "2026-10-29")  # closes on the 29th, not the generated 27th
        current = budgets.current_period(db, today=TODAY)
        assert (current.start, current.end) == (day("2026-09-28"), day("2026-10-29"))
        assert line(overview(db, current, today=TODAY), groceries).budget == D("700.00")
        upcoming = budgets.next_period(db, current)
        assert upcoming.start == day("2026-10-30")
        assert line(overview(db, upcoming, today=TODAY), groceries).budget == D("900.00")

    def test_an_edit_made_before_the_statement_arrives_stays_out_of_the_closed_cycle(
        self, db, cats, account
    ):
        """Review finding: on Oct 28 the cycle starts at the generated Oct 28; the user edits it,
        then the statement arrives closing Oct 29. The closed cycle keeps its budget, and so does
        the overall target, rollover and the other type's end row (every write of the edit)."""
        groceries, care, travel = cats["groceries"], cats["personal"], cats["travel"]
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        for category in (groceries, care, travel):
            make_budget(db, category, "700.00", effective_from=day("2026-01-01"))
        make_budget(db, None, "3000.00", effective_from=day("2026-01-01"))
        today = day("2026-10-28")
        current = budgets.current_period(db, today=today)
        assert (current.start, current.end) == (day("2026-10-28"), day("2026-11-27"))
        set_budget(db, groceries.id, D("400.00"), effective_from=current, today=today)
        set_budget(db, None, D("2500.00"), effective_from=current, today=today)
        set_rollover(db, care.id, True, effective_from=current, today=today)
        set_budget(db, travel.id, D("5000.00"), effective_from=current, period=ANNUAL)
        middle = day("2026-11-12")  # Oct 28 + 15 of the 31 days
        assert {(r[1], r[0]) for r in rows(db) if r[1] != day("2026-01-01")} == {
            (middle, MONTHLY),
            (middle, ANNUAL),
        }
        make_statement(db, account, "2026-10-29")
        closed = budgets.period_for(db, day("2026-10-20"))
        assert (closed.start, closed.end) == (day("2026-09-28"), day("2026-10-29"))
        result = overview(db, closed, today=today)
        assert line(result, groceries).budget == D("700.00")
        assert line(result, care).rollover is False
        assert line(result, travel).budget == D("700.00")  # still monthly
        assert result.overall.target == D("3000.00")
        after = budgets.next_period(db, closed)
        assert (after.start, after.end) == (day("2026-10-30"), day("2026-11-27"))
        result = overview(db, after, today=today)
        assert line(result, groceries).budget == D("400.00")
        assert line(result, care).rollover is True
        assert result.line_for(travel.id) is None and travel.id in result.annual_category_ids
        assert result.overall.target == D("2500.00")

    def test_the_edit_day(self, db, account):
        assert budgets.edit_day(OCT) == OCT.start
        assert budgets.middle_day(OCT) == day("2026-10-16")
        statement_mode(db, account, "2026-09-27", closing_day=27)
        cycle = budgets.period_for(db, TODAY)  # Sep 28 – Oct 27: 30 days
        assert budgets.edit_day(cycle) == budgets.middle_day(cycle) == day("2026-10-12")
        assert budgets.middle_day(budgets.period_for(db, day("2026-10-28"))) == day("2026-11-12")

    def test_calendar_rows_apply_in_statement_mode_and_back(self, db, cats, account):
        set_budget(db, cats["groceries"].id, D("600.00"), effective_from=SEP)
        set_budget(db, cats["groceries"].id, D("700.00"), effective_from=OCT)
        statement_mode(db, account, "2026-09-27", closing_day=27)
        cycle = budgets.period_for(db, TODAY)  # Sep 28 – Oct 27 holds the Oct 1 row
        assert line(overview(db, cycle, today=TODAY), cats["groceries"]).budget == D("700.00")
        previous = budgets.previous_period(db, cycle)  # Aug 28 – Sep 27: the Sep 1 row
        assert line(overview(db, previous, today=TODAY), cats["groceries"]).budget == D("600.00")
        # An edit in the cycle updates the row that starts inside it (Oct 1): no duplicate.
        set_budget(db, cats["groceries"].id, D("750.00"), effective_from=cycle)
        assert rows(db, cats["groceries"]) == [
            (MONTHLY, SEP.start, D("600.00"), False),
            (MONTHLY, OCT.start, D("750.00"), False),
        ]
        # Back to calendar months: October has the edit, September keeps its amount.
        set_app_settings(db, budget_period_mode="calendar")
        assert line(overview(db, OCT, today=TODAY), cats["groceries"]).budget == D("750.00")
        assert line(overview(db, SEP, today=TODAY), cats["groceries"]).budget == D("600.00")

    def test_statement_rows_apply_in_calendar_mode(self, db, cats, account):
        statement_mode(db, account, "2026-09-27", closing_day=27)
        cycle = budgets.period_for(db, TODAY)  # Sep 28 – Oct 27
        set_budget(db, cats["dining"].id, D("500.00"), effective_from=cycle)  # row at Oct 12
        set_app_settings(db, budget_period_mode="calendar")
        # The cycle's row applies from the month it mostly covers, not the month before.
        assert line(overview(db, OCT, today=TODAY), cats["dining"]).budget == D("500.00")
        assert overview(db, SEP, today=TODAY).line_for(cats["dining"].id) is None
        # An October edit updates the row that starts inside October: no duplicate.
        set_budget(db, cats["dining"].id, D("450.00"), effective_from=OCT)
        assert rows(db, cats["dining"]) == [(MONTHLY, day("2026-10-12"), D("450.00"), False)]
        set_budget(db, cats["dining"].id, D("400.00"), effective_from=NOV)
        assert rows(db, cats["dining"]) == [
            (MONTHLY, day("2026-10-12"), D("450.00"), False),
            (MONTHLY, NOV.start, D("400.00"), False),
        ]


class TestFlaggedCycles:
    """Review finding: a missing statement makes a ~60-day cycle; the overview says so."""

    def test_a_long_cycle_says_a_statement_seems_to_be_missing(self, db, cats, account, spend):
        statement_mode(db, account, "2026-08-27", "2026-10-27", closing_day=27)
        make_budget(db, cats["groceries"], "500.00", effective_from=day("2026-01-01"))
        spend(cats["groceries"], "400.00", "2026-09-05")
        spend(cats["groceries"], "400.00", "2026-10-05")
        period = budgets.current_period(db, today=TODAY)
        assert (period.start, period.end, period.flagged) == (
            day("2026-08-28"),
            day("2026-10-27"),
            True,
        )
        result = overview(db, period, today=TODAY)
        assert line(result, cats["groceries"]).status == "over"
        assert result.cycle_notice == (
            "This cycle is 61 days long, so a statement seems to be missing: the monthly budgets "
            "here cover 61 days of spending. Add the missing statement on the Statements page."
        )
        assert result.notice is None
        following = overview(db, budgets.next_period(db, period), today=TODAY)
        assert following.period.flagged is False and following.cycle_notice is None

    def test_a_short_cycle_says_a_date_may_be_wrong(self, db, cats, account):
        statement_mode(db, account, "2026-09-27", closing_day=10)  # the closing day changed
        period = budgets.current_period(db, today=day("2026-10-05"))
        assert (period.start, period.end, period.days) == (day("2026-09-28"), day("2026-10-10"), 13)
        notice = overview(db, period, today=day("2026-10-05")).cycle_notice
        assert notice.startswith("This cycle is only 13 days long, so a closing date may be wrong")
        assert notice.endswith("Check the closing dates on the Statements page.")

    def test_a_flagged_cycle_ends_the_rollover_chain(self, db, cats, account, spend):
        statement_mode(db, account, "2026-06-27", "2026-07-27", "2026-09-27", closing_day=27)
        home = cats["home"]
        make_budget(db, home, "100.00", effective_from=day("2026-06-28"), rollover=True)
        spend(home, "50.00", "2026-07-10")  # Jun 28 – Jul 27: $50 left
        spend(home, "180.00", "2026-08-10")  # Jul 28 – Sep 27, 62 days: flagged
        flagged = budgets.period_for(db, day("2026-08-10"))
        assert flagged.flagged
        result = overview(db, flagged, today=TODAY)
        assert (line(result, home).carry_in, line(result, home).available) == (
            D("50.00"),
            D("150.00"),
        )
        assert "rollover starts again after this cycle" in result.cycle_notice
        # What is "left" of the flagged cycle (−$30) doesn't carry: a new chain starts after it.
        after = overview(db, budgets.next_period(db, flagged), today=TODAY)
        assert line(after, home).carry_in == D("0.00")


# --------------------------------------------------------------------------- rollover (BUD-4)


class TestRollover:
    @pytest.fixture
    def care(self, cats, db, spend):
        """Personal Care: $100 a month with rollover from July; spent 80, 130, 50, 20."""
        make_budget(db, cats["personal"], "100.00", effective_from=day("2026-07-01"), rollover=True)
        for amount, date in (("80.00", "07-10"), ("130.00", "08-10"), ("50.00", "09-10")):
            spend(cats["personal"], amount, f"2026-{date}")
        spend(cats["personal"], "20.00", "2026-10-10")
        return cats["personal"]

    def test_carry_chains_with_positive_and_negative_carry(self, db, care):
        july, august = month_period(2026, 7), AUG
        assert line(overview(db, july, today=TODAY), care).carry_in == D("0.00")  # first period
        aug = line(overview(db, august, today=TODAY), care)
        assert (aug.carry_in, aug.available, aug.remaining) == (
            D("20.00"),
            D("120.00"),
            D("-10.00"),
        )
        assert aug.status == "over"
        sep = line(overview(db, SEP, today=TODAY), care)
        assert (sep.carry_in, sep.available) == (D("-10.00"), D("90.00"))  # overspend reduces it
        oct_ = line(overview(db, OCT, today=TODAY), care)
        assert (oct_.carry_in, oct_.available, oct_.spent, oct_.remaining) == (
            D("40.00"),
            D("140.00"),
            D("20.00"),
            D("120.00"),
        )
        assert oct_.percent_used == D("14.28")
        assert overview(db, OCT, today=TODAY).totals.budgeted == D("140.00")

    def test_turning_rollover_off_stops_the_chain(self, db, care):
        change = set_rollover(db, care.id, False, effective_from=SEP)
        assert change.changed and change.row.rollover is False
        assert line(overview(db, SEP, today=TODAY), care).carry_in == D("0.00")
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("0.00")
        assert line(overview(db, AUG, today=TODAY), care).carry_in == D("20.00")  # unchanged
        # On again in October: October starts a new chain.
        set_rollover(db, care.id, True, effective_from=OCT)
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("0.00")
        assert line(overview(db, NOV, today=TODAY), care).carry_in == D("80.00")

    def test_toggling_within_a_period_updates_its_row(self, db, care):
        set_rollover(db, care.id, False, effective_from=OCT)
        set_rollover(db, care.id, True, effective_from=OCT)
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("40.00")
        assert rows(db, care) == [
            (MONTHLY, day("2026-07-01"), D("100.00"), True),
            (MONTHLY, OCT.start, D("100.00"), True),
        ]
        assert set_rollover(db, care.id, True, effective_from=OCT).changed is False

    def test_a_changed_amount_carries_the_amount_of_each_period(self, db, care):
        set_budget(db, care.id, D("150.00"), effective_from=SEP)  # keeps rollover
        sep = line(overview(db, SEP, today=TODAY), care)
        assert (sep.budget, sep.carry_in, sep.available) == (D("150.00"), D("-10.00"), D("140.00"))
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("90.00")

    def test_rollover_switched_on_in_a_past_month_carries_forward(self, db, cats, spend):
        care = cats["personal"]
        make_budget(db, care, "100.00", effective_from=day("2026-01-01"))
        spend(care, "80.00", "2026-08-10")
        spend(care, "50.00", "2026-09-10")
        set_rollover(db, care.id, True, effective_from=AUG, today=TODAY)
        assert line(overview(db, SEP, today=TODAY), care).carry_in == D("20.00")
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("70.00")
        july = line(overview(db, month_period(2026, 7), today=TODAY), care)
        assert (july.rollover, july.carry_in) == (False, D("0.00"))

    def test_rollover_stays_off_through_later_edits(self, db, cats):
        care = cats["personal"]
        make_budget(db, care, "100.00", effective_from=day("2026-06-01"), rollover=True)
        set_rollover(db, care.id, False, effective_from=OCT, today=TODAY)
        assert set_budget(db, care.id, D("150.00"), effective_from=OCT).row.rollover is False
        assert set_budget(db, care.id, D("160.00"), effective_from=NOV).row.rollover is False

    def test_removing_and_adding_a_budget_back_starts_without_rollover(self, db, cats):
        care = cats["personal"]
        make_budget(db, care, "100.00", effective_from=day("2026-06-01"), rollover=True)
        set_budget(db, care.id, None, effective_from=SEP)
        assert set_budget(db, care.id, D("100.00"), effective_from=OCT).row.rollover is False
        assert line(overview(db, OCT, today=TODAY), care).carry_in == D("0.00")

    def test_rollover_needs_a_monthly_budget(self, db, cats):
        with pytest.raises(BudgetError, match="monthly budget first"):
            set_rollover(db, cats["groceries"].id, True, effective_from=OCT)
        make_budget(db, cats["travel"], "6000.00", effective_from=day("2026-01-01"), period=ANNUAL)
        with pytest.raises(BudgetError, match="monthly budget first"):
            set_rollover(db, cats["travel"].id, True, effective_from=OCT)

    def test_an_annual_period_breaks_the_chain(self, db, cats, spend):
        travel = cats["travel"]
        make_budget(db, travel, "100.00", effective_from=day("2026-06-01"), rollover=True)
        make_budget(db, travel, None, effective_from=AUG.start)
        make_budget(db, travel, "1000.00", effective_from=AUG.start, period=ANNUAL)
        make_budget(db, travel, "100.00", effective_from=SEP.start, rollover=True)
        make_budget(db, travel, None, effective_from=SEP.start, period=ANNUAL)
        assert line(overview(db, SEP, today=TODAY), travel).carry_in == D("0.00")
        assert line(overview(db, OCT, today=TODAY), travel).carry_in == D("100.00")

    def test_the_chain_is_bounded(self, db, cats):
        make_budget(db, cats["home"], "10.00", effective_from=day("2010-01-01"), rollover=True)
        carry = line(overview(db, OCT, today=TODAY), cats["home"]).carry_in
        assert carry == D("10.00") * budgets.MAX_ROLLOVER_PERIODS

    def test_rollover_in_statement_mode_steps_through_cycles(self, db, cats, account, spend):
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        make_budget(db, cats["home"], "100.00", effective_from=day("2026-08-28"), rollover=True)
        spend(cats["home"], "30.00", "2026-09-27")  # last day of the Aug 28 – Sep 27 cycle
        spend(cats["home"], "10.00", "2026-09-28")  # first day of the next
        cycle = budgets.period_for(db, TODAY)
        home = line(overview(db, cycle, today=TODAY), cats["home"])
        assert (home.carry_in, home.available, home.spent) == (D("70.00"), D("170.00"), D("10.00"))


# --------------------------------------------------------------------------- overall (BUD-3)


class TestOverall:
    def test_the_target_compares_with_spending_outside_annual_budgets(self, db, cats, spend):
        make_budget(db, None, "1000.00")
        make_budget(db, cats["groceries"], "400.00")
        make_budget(db, cats["travel"], "6000.00", period=ANNUAL)
        spend(cats["groceries"], "300.00", "2026-10-02")
        spend(cats["dining"], "150.00", "2026-10-02")  # unbudgeted: still counts
        spend(cats["travel"], "900.00", "2026-10-02")  # annual: never in monthly totals
        result = overview(db, OCT, today=TODAY)
        overall = result.overall
        assert overall.has_target and overall.target == overall.budget == D("1000.00")
        assert (overall.spent, overall.remaining, overall.percent_used, overall.status) == (
            D("450.00"),
            D("550.00"),
            D("45.00"),
            "ok",
        )
        assert overall.pace_percent == D("48.39")
        totals = result.totals
        assert (totals.spent, totals.budgeted_spent, totals.unbudgeted_spent) == (
            D("1350.00"),
            D("300.00"),
            D("150.00"),
        )
        assert totals.annual_spent == D("900.00")
        assert result.line_for(cats["travel"].id) is None
        assert [u.category.name for u in result.unbudgeted] == ["Dining & Drinks"]

    def test_without_a_target_the_period_budget_is_the_sum_of_monthly_budgets(
        self, db, cats, spend
    ):
        make_budget(db, cats["groceries"], "400.00")
        make_budget(db, cats["home"], "100.00", rollover=True, effective_from=SEP.start)
        spend(cats["home"], "60.00", "2026-09-05")  # $40 rolls into October
        spend(cats["groceries"], "300.00", "2026-10-02")
        spend(cats["home"], "120.00", "2026-10-02")
        spend(cats["dining"], "75.00", "2026-10-02")
        result = overview(db, OCT, today=TODAY)
        overall = result.overall
        assert overall.has_target is False
        assert (overall.budget, overall.spent, overall.remaining) == (
            D("540.00"),
            D("420.00"),
            D("120.00"),
        )
        assert overall.status == "ok"
        assert result.totals.budgeted == D("540.00")
        assert result.totals.remaining == D("120.00")
        assert result.totals.unbudgeted_spent == D("75.00")

    def test_over_the_target(self, db, cats, spend):
        make_budget(db, None, "100.00")
        spend(cats["dining"], "100.01", "2026-10-02")
        overall = overview(db, OCT, today=TODAY).overall
        assert (overall.status, overall.percent_used, overall.remaining) == (
            "over",
            D("100.01"),
            D("-0.01"),
        )


# --------------------------------------------------------------------------- annual (BUD-5)


class TestAnnual:
    @pytest.fixture
    def travel(self, db, cats, spend):
        make_budget(db, cats["travel"], "6000.00", effective_from=day("2026-01-01"), period=ANNUAL)
        make_budget(db, cats["travel"], None, effective_from=day("2026-01-01"))
        spend(cats["travel"], "1200.00", "2026-02-10")
        spend(cats["travel"], "2400.00", "2026-06-20")
        spend(cats["travel"], "-200.00", "2026-06-25")
        spend(cats["travel"], "500.00", "2026-10-05")
        spend(cats["travel"], "999.00", "2025-12-30")  # last year
        return cats["travel"]

    def test_year_to_date_with_a_pace_line(self, db, travel):
        annual = annual_overview(db, 2026, today=TODAY)
        assert annual.as_of == OCT.end and annual.period == year_period(2026)
        (entry,) = annual.lines
        assert (entry.budget, entry.spent, entry.remaining) == (
            D("6000.00"),
            D("3900.00"),
            D("2100.00"),
        )
        assert (entry.percent_used, entry.status) == (D("65.00"), "ok")
        # Oct 15 is day 288 of 2026: 6000 × 288 / 365.
        assert entry.pace_amount == D("4734.25")
        assert entry.pace_percent == annual.pace_percent == D("78.90")
        assert (annual.budget_total, annual.spent_total, annual.remaining_total) == (
            D("6000.00"),
            D("3900.00"),
            D("2100.00"),
        )

    def test_annual_spending_never_counts_in_monthly_totals(self, db, travel, cats):
        make_budget(db, None, "2000.00")
        result = overview(db, OCT, today=TODAY)
        assert result.line_for(travel.id) is None
        assert result.unbudgeted == ()
        assert result.totals.annual_spent == D("500.00")
        assert result.overall.spent == D("0.00")

    def test_as_of_an_earlier_period(self, db, travel):
        annual = annual_overview(db, 2026, today=TODAY, as_of=day("2026-06-30"))
        (entry,) = annual.lines
        assert entry.spent == D("3400.00")
        assert entry.pace_amount == D("2975.34")  # 6000 × 181 / 365: pace as of Jun 30

    def test_a_past_year_has_no_pace(self, db, travel):
        make_budget(db, travel, "3000.00", effective_from=day("2025-01-01"), period=ANNUAL)
        annual = annual_overview(db, 2025, today=TODAY)
        assert annual.as_of == day("2025-12-31")
        (entry,) = annual.lines
        assert (entry.budget, entry.spent, entry.pace_amount, entry.pace_percent) == (
            D("3000.00"),
            D("999.00"),
            None,
            None,
        )
        assert annual.days_left == 0

    def test_annual_spending_uses_the_statement_basis_in_statement_mode(
        self, db, cats, account, spend
    ):
        make_budget(db, cats["gifts"], "800.00", effective_from=day("2026-01-01"), period=ANNUAL)
        spend(
            cats["gifts"],
            "70.00",
            "2027-01-02",
            authorized_date=day("2026-12-30"),
            posted_date=day("2027-01-02"),
        )
        later = day("2027-01-05")
        assert annual_overview(db, 2026, today=later).lines[0].spent == D("70.00")
        statement_mode(db, account, "2026-09-27", closing_day=27)
        assert annual_overview(db, 2026, today=later).lines[0].spent == D("0.00")

    def test_hidden_categories_and_monthly_budgets_are_left_out(self, db, cats, travel):
        make_budget(db, cats["groceries"], "500.00")
        make_budget(db, cats["gifts"], "800.00", period=ANNUAL)
        cats["gifts"].is_hidden = True
        assert [a.category.name for a in annual_overview(db, 2026, today=TODAY).lines] == ["Travel"]

    def test_a_period_shows_the_years_it_touches(self, db, cats, account, spend):
        """Review finding: the cycle Dec 28 – Jan 27 showed only 2027, so late-December spending
        in annual categories (in no monthly figure) was in no annual view either."""
        travel = cats["travel"]
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period=ANNUAL)
        spend(travel, "1000.00", "2026-06-10")
        spend(travel, "500.00", "2026-12-29")
        statement_mode(db, account, "2026-11-27", "2026-12-27", closing_day=27)
        today = day("2026-12-30")
        cycle = budgets.current_period(db, today=today)
        assert (cycle.start, cycle.end) == (day("2026-12-28"), day("2027-01-27"))
        old, new = budgets.annual_overviews(db, cycle, today=today)
        assert (old.year, old.as_of, new.year, new.as_of) == (
            2026,
            day("2026-12-31"),
            2027,
            day("2027-01-27"),
        )
        (entry,) = old.lines
        assert (entry.spent, entry.remaining) == (D("1500.00"), D("4500.00"))
        assert entry.pace_amount == D("5983.56")  # Dec 30 is day 364: 6000 × 364 / 365
        (entry,) = new.lines  # the 2026 row is still in force
        assert (entry.spent, entry.pace_amount) == (D("0.00"), None)
        # A cycle inside one year, and a calendar month: one overview, as of the period's end.
        (only,) = budgets.annual_overviews(db, budgets.previous_period(db, cycle), today=today)
        assert (only.year, only.as_of, only.spent_total) == (2026, day("2026-12-27"), D("1000.00"))
        (only,) = budgets.annual_overviews(db, month_period(2026, 12), today=today)
        assert (only.year, only.as_of) == (2026, day("2026-12-31"))

    def test_over_an_annual_budget(self, db, cats, spend):
        make_budget(db, cats["gifts"], "100.00", period=ANNUAL)
        spend(cats["gifts"], "150.00", "2026-03-01")
        (entry,) = annual_overview(db, 2026, today=TODAY).lines
        assert (entry.status, entry.percent_used, entry.remaining) == (
            "over",
            D("150.00"),
            D("-50.00"),
        )


# --------------------------------------------------------------------------- suggestions


class TestSuggestions:
    def test_the_mean_of_the_last_three_complete_periods_rounded_up_to_5(self, db, cats, spend):
        for date, amount in (
            ("2026-06-10", "999.00"),  # four periods back: not used
            ("2026-07-10", "600.00"),
            ("2026-08-10", "612.40"),
            ("2026-09-10", "625.00"),
            ("2026-10-10", "999.00"),  # the current period is not complete
        ):
            spend(cats["groceries"], amount, date)
        result = suggestions(db, today=TODAY)
        assert [p.label for p in result.periods] == ["Sep 2026", "Aug 2026", "Jul 2026"]
        assert result.for_category(cats["groceries"].id) == D("615.00")  # 612.47 → 615
        assert suggest(db, cats["groceries"].id, today=TODAY) == D("615.00")

    def test_an_exact_multiple_of_5_stays(self, db, cats, spend):
        for month in (7, 8, 9):
            spend(cats["dining"], "300.00", f"2026-{month:02d}-15")
        assert suggest(db, cats["dining"].id, today=TODAY) == D("300.00")
        assert budgets.round_up_suggestion(D("0.01")) == D("5.00")
        assert budgets.round_up_suggestion(D("-20.00")) == D("0.00")

    def test_fewer_than_three_periods(self, db, cats, spend):
        spend(cats["groceries"], "100.00", "2026-08-01")  # history starts on Aug 1
        spend(cats["groceries"], "201.00", "2026-09-05")
        result = suggestions(db, today=TODAY)
        assert [p.label for p in result.periods] == ["Sep 2026", "Aug 2026"]
        assert result.for_category(cats["groceries"].id) == D("155.00")  # 150.50 → 155

    def test_the_first_partial_period_is_not_averaged(self, db, cats, spend):
        """Review finding: history starting Aug 20 holds 12 days of August, which isn't a
        complete period (averaging it as one gave the demo's $215 for a $3,274 month)."""
        spend(cats["groceries"], "100.00", "2026-08-20")
        spend(cats["groceries"], "201.00", "2026-09-05")
        result = suggestions(db, today=TODAY)
        assert [p.label for p in result.periods] == ["Sep 2026"]
        assert result.for_category(cats["groceries"].id) == D("205.00")  # 201 → 205
        spend(cats["groceries"], "50.00", "2026-09-20")  # history from September only
        assert suggest(db, cats["groceries"].id, today=TODAY) == D("255.00")
        assert suggest(db, cats["groceries"].id, today=TODAY, before=SEP) is None

    def test_no_data(self, db, cats, spend):
        assert suggest(db, cats["groceries"].id, today=TODAY) is None
        assert suggest(db, None, today=TODAY) is None
        spend(cats["groceries"], "50.00", "2026-10-03")  # only the current period
        assert suggest(db, cats["groceries"].id, today=TODAY) is None

    def test_a_category_without_spending_suggests_zero(self, db, cats, spend):
        spend(cats["groceries"], "50.00", "2026-09-01")
        assert suggest(db, cats["home"].id, today=TODAY) == D("0.00")

    def test_periods_before_a_viewed_period(self, db, cats, spend):
        for month, amount in ((5, "100.00"), (6, "200.00"), (7, "300.00"), (8, "900.00")):
            spend(cats["home"], amount, f"2026-{month:02d}-01")
        assert suggest(db, cats["home"].id, today=TODAY, before=AUG) == D("200.00")
        # A future period skips the current, incomplete one.
        result = suggestions(db, today=TODAY, before=NOV)
        assert [p.label for p in result.periods] == ["Sep 2026", "Aug 2026", "Jul 2026"]

    def test_the_overall_suggestion_leaves_out_annual_categories(self, db, cats, spend):
        make_budget(db, cats["travel"], "6000.00", effective_from=day("2026-01-01"), period=ANNUAL)
        for month in (7, 8, 9):
            spend(cats["groceries"], "400.00", f"2026-{month:02d}-15")
            spend(cats["travel"], "1000.00", f"2026-{month:02d}-16")
        assert suggest(db, None, today=TODAY) == D("400.00")

    def test_suggestions_follow_statement_cycles(self, db, cats, account, spend):
        statement_mode(db, account, "2026-08-27", "2026-09-27", closing_day=27)
        spend(cats["groceries"], "10.00", "2026-08-01")  # history from Aug 1
        spend(cats["home"], "90.00", "2026-09-27")  # Aug 28 – Sep 27
        spend(cats["home"], "60.00", "2026-09-28")  # the current cycle: not complete
        result = suggestions(db, today=TODAY)
        assert [p.end for p in result.periods] == [day("2026-09-27")]  # Jul 28 – Aug 27: partial
        assert result.for_category(cats["home"].id) == D("90.00")

    def test_a_flagged_cycle_is_not_averaged(self, db, cats, account, spend):
        """A 61-day cycle (September's statement missing) would count two cycles' spending as
        one: the suggestion averages the cycles around it instead."""
        statement_mode(db, account, "2026-06-27", "2026-07-27", "2026-09-27", closing_day=27)
        spend(cats["home"], "100.00", "2026-06-01")  # history from June
        spend(cats["home"], "100.00", "2026-07-10")  # Jun 28 – Jul 27
        spend(cats["home"], "400.00", "2026-08-10")  # Jul 28 – Sep 27: flagged
        today = day("2026-10-15")
        result = suggestions(db, today=today)
        assert [(p.start, p.end) for p in result.periods] == [
            (day("2026-06-28"), day("2026-07-27"))
        ]
        assert result.for_category(cats["home"].id) == D("100.00")


def test_a_new_category_can_be_budgeted(db):
    pets = make_category(db, "Pets")
    set_budget(db, pets.id, D("120.00"), effective_from=OCT)
    assert line(overview(db, OCT, today=TODAY), pets).budget == D("120.00")
