import datetime as dt
from decimal import Decimal

import pytest

from budgetapp.periods import Period, month_period
from budgetapp.services import spending
from budgetapp.services.spending import (
    BASIS_POSTED_DATE,
    BASIS_SPEND_DATE,
    basis_for_mode,
    monthly_spending,
    period_spending,
    spending_by_category,
    top_merchants,
)
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_split,
    make_transaction,
    set_app_settings,
)

D = Decimal
OCT = month_period(2026, 10)
NOV = month_period(2026, 11)


@pytest.fixture
def cats(db):
    return {
        n: get_category(db, n)
        for n in ("Groceries", "Dining & Drinks", "Home", "Credits & Rewards")
    }


@pytest.fixture
def account(db):
    return make_account(db)


def test_basis_for_mode():
    assert basis_for_mode("calendar") == BASIS_SPEND_DATE
    assert basis_for_mode("statement") == BASIS_POSTED_DATE
    with pytest.raises(ValueError):
        basis_for_mode("weekly")


class TestPeriodSpending:
    def test_charges_add_refunds_subtract(self, db, cats, account):
        dining = cats["Dining & Drinks"]
        make_transaction(db, account, amount="100.00", category=dining, date="2026-10-03")
        make_transaction(db, account, amount="-30.00", category=dining, date="2026-10-04")
        make_transaction(db, account, amount="12.50", category=cats["Groceries"], date="2026-10-05")
        result = period_spending(db, OCT)
        assert result.amount_for(dining.id) == D("70.00")
        row = result.by_category[dining.id]
        assert (row.charges, row.credits, row.transaction_count) == (D("100.00"), D("-30.00"), 2)
        assert result.total == D("82.50")
        assert result.categories[0].category_id == dining.id  # largest first

    def test_payments_and_credits_never_count(self, db, cats, account):
        make_transaction(
            db, account, amount="-2500.00", category=card_payments(db), date="2026-10-10"
        )
        make_transaction(
            db, account, amount="-25.00", category=cats["Credits & Rewards"], date="2026-10-10"
        )
        make_transaction(db, account, amount="5.00", category=cats["Home"], date="2026-10-10")
        result = period_spending(db, OCT)
        assert result.total == D("5.00")
        assert {r.category_id for r in result.categories} == {cats["Home"].id}

    def test_calendar_mode_uses_the_authorized_date(self, db, cats, account):
        home = cats["Home"]
        make_transaction(
            db,
            account,
            amount="40.00",
            category=home,
            authorized_date="2026-10-31",
            date="2026-11-02",
            posted_date="2026-11-02",
        )
        assert period_spending(db, OCT).amount_for(home.id) == D("40.00")
        assert period_spending(db, NOV).amount_for(home.id) == D("0.00")

    def test_statement_mode_uses_the_posting_date(self, db, cats, account):
        home = cats["Home"]
        set_app_settings(db, budget_period_mode="statement")
        make_transaction(
            db,
            account,
            amount="40.00",
            category=home,
            authorized_date="2026-10-31",
            date="2026-11-02",
            posted_date="2026-11-02",
        )
        cycle = Period(dt.date(2026, 10, 28), dt.date(2026, 11, 27), "statement")
        result = period_spending(db, cycle)
        assert result.basis == BASIS_POSTED_DATE
        assert result.amount_for(home.id) == D("40.00")
        before = Period(dt.date(2026, 9, 28), dt.date(2026, 10, 27), "statement")
        assert period_spending(db, before).total == D("0.00")

    def test_pending_rows_follow_the_setting(self, db, cats, account):
        home = cats["Home"]
        make_transaction(
            db, account, amount="20.00", category=home, date="2026-10-12", pending=True
        )
        make_transaction(db, account, amount="5.00", category=home, date="2026-10-12")
        assert period_spending(db, OCT).total == D("25.00")
        set_app_settings(db, include_pending_in_budgets=False)
        result = period_spending(db, OCT)
        assert result.total == D("5.00") and result.include_pending is False

    def test_pending_rows_in_statement_mode_use_their_date(self, db, cats, account):
        set_app_settings(db, budget_period_mode="statement")
        make_transaction(
            db, account, amount="20.00", category=cats["Home"], date="2026-10-12", pending=True
        )
        assert period_spending(db, OCT).total == D("20.00")

    def test_splits_count_only_when_they_sum(self, db, cats, account):
        groceries, home = cats["Groceries"], cats["Home"]
        good = make_transaction(db, account, amount="100.00", category=home, date="2026-10-03")
        make_split(db, good, groceries, "60.00")
        make_split(db, good, home, "40.00")
        bad = make_transaction(db, account, amount="50.00", category=home, date="2026-10-03")
        make_split(db, bad, groceries, "10.00")
        make_split(db, bad, home, "30.00")  # sums to 40, not 50: ignored
        result = period_spending(db, OCT)
        assert result.amount_for(groceries.id) == D("60.00")
        assert result.amount_for(home.id) == D("90.00")
        assert result.total == D("150.00")

    def test_suspect_duplicates_are_excluded_but_kept_rows_count(self, db, cats):
        manual = make_account(db, manual=True)
        home = cats["Home"]
        make_transaction(
            db,
            manual,
            source="csv",
            amount="70.00",
            category=home,
            date="2026-10-03",
            duplicate_status="suspect",
        )
        make_transaction(
            db,
            manual,
            source="csv",
            amount="8.00",
            category=home,
            date="2026-10-03",
            duplicate_status="kept",
        )
        assert period_spending(db, OCT).total == D("8.00")

    def test_hidden_categories_still_count(self, db, cats, account):
        home = cats["Home"]
        home.is_hidden = True
        make_transaction(db, account, amount="9.99", category=home, date="2026-10-03")
        assert period_spending(db, OCT).amount_for(home.id) == D("9.99")


def test_spending_by_category_kinds(db, cats, account):
    make_transaction(
        db, account, amount="-25.00", category=cats["Credits & Rewards"], date="2026-10-10"
    )
    rows = spending_by_category(
        db, OCT.start, OCT.end, basis=BASIS_SPEND_DATE, include_pending=True, kinds=("income",)
    )
    assert [(r.name, r.amount) for r in rows] == [("Credits & Rewards", D("-25.00"))]


def test_monthly_spending_uses_calendar_months_by_spend_date(db, cats, account):
    set_app_settings(db, budget_period_mode="statement")  # trends ignore the budget mode
    home = cats["Home"]
    make_transaction(
        db, account, amount="10.00", category=home, authorized_date="2026-08-31", date="2026-09-02"
    )
    make_transaction(db, account, amount="30.00", category=home, date="2026-10-15")
    months = monthly_spending(db, dt.date(2026, 8, 1), dt.date(2026, 11, 30))
    assert [m.month.label for m in months] == ["Aug 2026", "Sep 2026", "Oct 2026", "Nov 2026"]
    assert [m.total for m in months] == [D("10.00"), D("0.00"), D("30.00"), D("0.00")]
    assert months[2].amount_for(home.id) == D("30.00")
    assert monthly_spending(db, dt.date(2026, 9, 1), dt.date(2026, 8, 1)) == []


def test_top_merchants(db, cats, account):
    dining = cats["Dining & Drinks"]
    for amount in ("20.00", "15.00"):
        make_transaction(
            db, account, amount=amount, category=dining, merchant_name="Cafe", date="2026-10-02"
        )
    make_transaction(
        db, account, amount="50.00", category=dining, merchant_name="Bistro", date="2026-10-02"
    )
    make_transaction(
        db, account, amount="-5.00", category=dining, merchant_name="Refundia", date="2026-10-02"
    )
    make_transaction(
        db,
        account,
        amount="8.00",
        category=dining,
        merchant_name=None,
        name="FOOD TRUCK",
        date="2026-10-02",
    )
    rows = top_merchants(
        db, OCT.start, OCT.end, basis=BASIS_SPEND_DATE, include_pending=True, limit=3
    )
    assert [(r.merchant, r.amount, r.transaction_count) for r in rows] == [
        ("Bistro", D("50.00"), 1),
        ("Cafe", D("35.00"), 2),
        ("FOOD TRUCK", D("8.00"), 1),
    ]
    assert spending.period_top_merchants(db, OCT, limit=1)[0].merchant == "Bistro"
