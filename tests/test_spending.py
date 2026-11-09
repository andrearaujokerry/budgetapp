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
    make_category,
    make_item,
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


class TestPeriodsSpending:
    """Several periods in one query (a rollover chain): each is exactly its period_spending."""

    def test_each_period_is_its_period_spending(self, db, cats, account):
        home, dining = cats["Home"], cats["Dining & Drinks"]
        for date, amount, category in (
            ("2026-07-31", "1.00", home),
            ("2026-08-01", "10.00", home),  # first day of August
            ("2026-08-31", "20.00", dining),  # last day of August
            ("2026-09-15", "40.00", home),  # in the gap: September isn't asked for
            ("2026-10-01", "80.00", home),
            ("2026-10-02", "-5.00", home),
            ("2026-11-01", "160.00", home),
        ):
            make_transaction(db, account, amount=amount, category=category, date=date)
        aug, oct_ = month_period(2026, 8), OCT
        result = spending.periods_spending(db, [oct_, aug, month_period(2025, 1)])
        assert result == {
            (p.start, p.end): period_spending(db, p) for p in (aug, oct_, month_period(2025, 1))
        }
        assert (result[(aug.start, aug.end)].total, result[(oct_.start, oct_.end)].total) == (
            D("30.00"),
            D("75.00"),
        )
        assert result[(aug.start, aug.end)].categories[0].category_id == dining.id
        assert spending.periods_spending(db, []) == {}

    def test_statement_cycles_use_the_posting_date(self, db, cats, account):
        set_app_settings(db, budget_period_mode="statement")
        make_transaction(
            db,
            account,
            amount="40.00",
            category=cats["Home"],
            authorized_date="2026-10-27",
            date="2026-10-28",
            posted_date="2026-10-28",
        )
        before = Period(dt.date(2026, 9, 28), dt.date(2026, 10, 27), "statement")
        cycle = Period(dt.date(2026, 10, 28), dt.date(2026, 11, 27), "statement")
        result = spending.periods_spending(db, [before, cycle])
        assert (
            result[(before.start, before.end)].total,
            result[(cycle.start, cycle.end)].total,
        ) == (
            D("0.00"),
            D("40.00"),
        )

    def test_overlapping_periods_are_refused(self, db):
        with pytest.raises(ValueError, match="overlap"):
            spending.periods_spending(
                db, [OCT, Period(dt.date(2026, 10, 31), dt.date(2026, 11, 5))]
            )


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


# --------------------------------------------------------------------------- income (INC-1)


def income_category(db, name: str):
    """An income category by name: MONEY's migration seeds Paycheck, Interest and Other Income
    (W3-PLAN C2); without it the test creates the category."""
    try:
        category = get_category(db, name)
    except LookupError:
        category = make_category(db, name, kind="income", color="other")
    assert category.kind == "income"
    return category


def transfers_category(db):
    """A transfer-kind category other than Card Payments (C2's "Transfers")."""
    try:
        category = get_category(db, "Transfers")
    except LookupError:
        category = make_category(db, "Transfers", kind="transfer", color="other")
    assert category.kind == "transfer"
    return category


@pytest.fixture
def checking(db):
    """A checking account on a bank's own Item (C1: type ``depository``)."""
    bank = make_item(db, institution_id="ins_109508", institution_name="First Platypus Bank")
    return make_account(db, bank, name="Plaid Checking", type="depository", subtype="checking")


@pytest.fixture
def income(db):
    return {name: income_category(db, name) for name in ("Paycheck", "Interest", "Other Income")}


class TestIncome:
    """INC-1: money in, booked to income-kind categories from any account, as a positive
    number, on the same date basis, pending setting and suspect rule as spending (C4)."""

    def test_money_in_from_any_account_is_positive_income(
        self, db, cats, account, checking, income
    ):
        paycheck = income["Paycheck"]
        make_transaction(db, checking, amount="-2400.00", category=paycheck, date="2026-10-09")
        make_transaction(db, checking, amount="-2400.00", category=paycheck, date="2026-10-23")
        make_transaction(
            db, checking, amount="-3.17", category=income["Interest"], date="2026-10-31"
        )
        credits = cats["Credits & Rewards"]
        make_transaction(db, account, amount="-25.00", category=credits, date="2026-10-12")
        result = spending.period_income(db, OCT)
        assert [(r.name, r.amount) for r in result.categories] == [
            ("Paycheck", D("4800.00")),
            ("Credits & Rewards", D("25.00")),  # Amex statement credits: their own line
            ("Interest", D("3.17")),
        ]
        assert result.total == D("4828.17")
        assert result.amount_for(paycheck.id) == D("4800.00")
        assert result.amount_for(cats["Home"].id) == D("0.00")
        assert result.largest.name == "Paycheck"
        row = result.categories[0]
        assert (row.received, row.taken_back, row.transaction_count) == (D("4800.00"), 0, 2)
        assert (result.basis, result.include_pending) == (BASIS_SPEND_DATE, True)

    def test_spending_transfers_and_payments_are_not_income(
        self, db, cats, account, checking, income
    ):
        make_transaction(db, account, amount="-2500.00", category=card_payments(db),
                         date="2026-10-10")  # fmt: skip
        make_transaction(db, checking, amount="2500.00", category=card_payments(db),
                         date="2026-10-10")  # the same payment, seen from checking  # fmt: skip
        make_transaction(db, checking, amount="500.00", category=transfers_category(db),
                         date="2026-10-15")  # to savings  # fmt: skip
        make_transaction(db, account, amount="-30.00", category=cats["Groceries"],
                         date="2026-10-04")  # a refund: less spending, not income  # fmt: skip
        result = spending.period_income(db, OCT)
        assert result.categories == () and result.total == D("0.00")
        assert result.largest is None
        # …and income never changes what the spending functions return.
        make_transaction(db, checking, amount="-2400.00", category=income["Paycheck"],
                         date="2026-10-09")  # fmt: skip
        assert period_spending(db, OCT).total == D("-30.00")
        assert {r.kind for r in period_spending(db, OCT).categories} == {"expense"}

    def test_a_reversal_reduces_income(self, db, cats, account):
        credits = cats["Credits & Rewards"]
        make_transaction(db, account, amount="-100.00", category=credits, date="2026-10-02")
        make_transaction(db, account, amount="40.00", category=credits, date="2026-10-20")
        (row,) = spending.period_income(db, OCT).categories
        assert (row.amount, row.received, row.taken_back) == (D("60.00"), D("100.00"), D("40.00"))

    def test_income_taken_back_entirely_is_negative_and_not_the_largest(self, db, cats, account):
        make_transaction(db, account, amount="15.00", category=cats["Credits & Rewards"],
                         date="2026-10-02")  # a clawed-back credit from last month  # fmt: skip
        result = spending.period_income(db, OCT)
        assert result.total == D("-15.00") and result.largest is None

    def test_calendar_mode_uses_the_authorized_date_statement_mode_the_posting_date(
        self, db, checking, income
    ):
        paycheck = income["Paycheck"]
        make_transaction(db, checking, amount="-900.00", category=paycheck,
                         authorized_date="2026-10-31", date="2026-11-02",
                         posted_date="2026-11-02")  # fmt: skip
        assert spending.period_income(db, OCT).total == D("900.00")
        assert spending.period_income(db, NOV).total == D("0.00")
        set_app_settings(db, budget_period_mode="statement")
        cycle = Period(dt.date(2026, 10, 28), dt.date(2026, 11, 27), "statement")
        before = Period(dt.date(2026, 9, 28), dt.date(2026, 10, 27), "statement")
        assert spending.period_income(db, cycle).total == D("900.00")
        assert spending.period_income(db, cycle).basis == BASIS_POSTED_DATE
        assert spending.period_income(db, before).total == D("0.00")

    def test_pending_rows_follow_the_budget_setting(self, db, checking, income):
        paycheck = income["Paycheck"]
        make_transaction(db, checking, amount="-1000.00", category=paycheck, date="2026-10-12",
                         pending=True)  # fmt: skip
        make_transaction(db, checking, amount="-5.00", category=paycheck, date="2026-10-12")
        assert spending.period_income(db, OCT).total == D("1005.00")
        set_app_settings(db, include_pending_in_budgets=False)
        result = spending.period_income(db, OCT)
        assert result.total == D("5.00") and result.include_pending is False
        months = spending.monthly_income(db, OCT.start, OCT.end)
        assert months[0].total == D("5.00")
        assert spending.monthly_income(db, OCT.start, OCT.end, include_pending=True)[0].total == (
            D("1005.00")
        )

    def test_suspect_duplicates_are_excluded(self, db, cats):
        manual = make_account(db, manual=True)
        credits = cats["Credits & Rewards"]
        make_transaction(db, manual, source="csv", amount="-70.00", category=credits,
                         date="2026-10-03", duplicate_status="suspect")  # fmt: skip
        make_transaction(db, manual, source="csv", amount="-8.00", category=credits,
                         date="2026-10-03", duplicate_status="kept")  # fmt: skip
        assert spending.period_income(db, OCT).total == D("8.00")

    def test_split_parts_in_income_categories_count(self, db, checking, income):
        deposit = make_transaction(db, checking, amount="-1000.00", category=income["Paycheck"],
                                   date="2026-10-15")  # fmt: skip
        make_split(db, deposit, income["Paycheck"], "-800.00")
        make_split(db, deposit, income["Other Income"], "-200.00")
        result = spending.period_income(db, OCT)
        assert result.amount_for(income["Paycheck"].id) == D("800.00")
        assert result.amount_for(income["Other Income"].id) == D("200.00")
        broken = make_transaction(db, checking, amount="-50.00", category=income["Interest"],
                                  date="2026-10-16")  # fmt: skip
        make_split(db, broken, income["Paycheck"], "-10.00")  # doesn't add up: ignored
        assert spending.period_income(db, OCT).amount_for(income["Interest"].id) == D("50.00")

    def test_hidden_categories_and_accounts_still_count(self, db, cats, account):
        credits = cats["Credits & Rewards"]
        account.is_hidden = True
        make_transaction(db, account, amount="-9.99", category=credits, date="2026-10-03")
        assert spending.period_income(db, OCT).total == D("9.99")

    def test_periods_income_is_each_periods_income(self, db, checking, income):
        paycheck = income["Paycheck"]
        for date, amount in (("2026-08-01", "-10.00"), ("2026-08-31", "-20.00"),
                             ("2026-09-15", "-40.00"), ("2026-10-01", "-80.00")):  # fmt: skip
            make_transaction(db, checking, amount=amount, category=paycheck, date=date)
        aug = month_period(2026, 8)
        result = spending.periods_income(db, [OCT, aug])
        assert result == {(p.start, p.end): spending.period_income(db, p) for p in (aug, OCT)}
        assert result[(aug.start, aug.end)].total == D("30.00")
        assert spending.periods_income(db, []) == {}
        with pytest.raises(ValueError, match="overlap"):
            spending.periods_income(db, [OCT, Period(dt.date(2026, 10, 31), dt.date(2026, 11, 5))])

    def test_monthly_income_uses_calendar_months_by_spend_date(self, db, cats, checking, income):
        set_app_settings(db, budget_period_mode="statement")  # trends ignore the budget mode
        paycheck = income["Paycheck"]
        make_transaction(db, checking, amount="-10.00", category=paycheck,
                         authorized_date="2026-08-31", date="2026-09-02")  # fmt: skip
        make_transaction(db, checking, amount="-30.00", category=paycheck, date="2026-10-15")
        make_transaction(db, checking, amount="45.00", category=cats["Groceries"],
                         date="2026-10-16")  # spending: not income  # fmt: skip
        months = spending.monthly_income(db, dt.date(2026, 8, 1), dt.date(2026, 11, 30))
        assert [m.month.label for m in months] == ["Aug 2026", "Sep 2026", "Oct 2026", "Nov 2026"]
        assert [m.total for m in months] == [D("10.00"), D("0.00"), D("30.00"), D("0.00")]
        assert months[2].amount_for(paycheck.id) == D("30.00")
        assert months[1].categories == ()
        assert spending.monthly_income(db, dt.date(2026, 9, 1), dt.date(2026, 8, 1)) == []

    def test_income_by_category(self, db, cats, account, checking, income):
        make_transaction(db, checking, amount="-3.00", category=income["Interest"],
                         date="2026-10-31")  # fmt: skip
        make_transaction(db, account, amount="-25.00", category=cats["Credits & Rewards"],
                         date="2026-10-10")  # fmt: skip
        rows = spending.income_by_category(
            db, OCT.start, OCT.end, basis=BASIS_SPEND_DATE, include_pending=True
        )
        assert [(r.name, r.amount) for r in rows] == [
            ("Credits & Rewards", D("25.00")),
            ("Interest", D("3.00")),
        ]


@pytest.mark.parametrize("mode", ["calendar", "statement"])
@pytest.mark.parametrize("include_pending", [True, False])
def test_income_spending_and_transfers_add_up_to_every_row(
    db, cats, account, checking, income, mode, include_pending
):
    """The cross-check (W3-PLAN §3 INCOME): in a period, spending − income + transfers is the
    plain sum of every counted row's amount (Plaid's sign), on the period's basis and pending
    setting, with splits, refunds, payments, transfers and suspect rows in the mix."""
    set_app_settings(db, budget_period_mode=mode, include_pending_in_budgets=include_pending)
    period = (
        OCT if mode == "calendar" else Period(dt.date(2026, 9, 28), dt.date(2026, 10, 27), mode)
    )
    groceries, home = cats["Groceries"], cats["Home"]
    rows = [
        make_transaction(db, account, amount="82.40", category=groceries, date="2026-10-03"),
        make_transaction(db, account, amount="-12.40", category=groceries, date="2026-10-04"),
        make_transaction(
            db, account, amount="-25.00", category=cats["Credits & Rewards"], date="2026-10-05"
        ),  # fmt: skip
        make_transaction(
            db, account, amount="-1200.00", category=card_payments(db), date="2026-10-06"
        ),  # fmt: skip
        make_transaction(
            db, checking, amount="1200.00", category=card_payments(db), date="2026-10-06"
        ),  # fmt: skip
        make_transaction(
            db, checking, amount="-2400.00", category=income["Paycheck"], date="2026-10-09"
        ),  # fmt: skip
        make_transaction(
            db, checking, amount="300.00", category=transfers_category(db), date="2026-10-11"
        ),  # fmt: skip
        make_transaction(db, checking, amount="1850.00", category=home, date="2026-10-01"),
        make_transaction(
            db, checking, amount="60.00", category=home, date="2026-10-19", pending=True
        ),  # fmt: skip
        make_transaction(
            db,
            checking,
            amount="-4.10",
            category=income["Interest"],
            date="2026-10-27",
            authorized_date="2026-10-26",
        ),  # fmt: skip
        make_transaction(
            db,
            account,
            amount="45.00",
            category=home,
            date="2026-10-28",
            authorized_date="2026-10-27",
        ),  # the cycle's last day by purchase  # fmt: skip
    ]
    split = make_transaction(db, account, amount="100.00", category=home, date="2026-10-12")
    make_split(db, split, groceries, "70.00")
    make_split(db, split, cats["Credits & Rewards"], "30.00")  # odd, but it must still add up
    rows.append(split)
    make_transaction(db, account, amount="999.00", category=home, date="2026-10-13",
                     source="csv", duplicate_status="suspect")  # never counted  # fmt: skip

    spent = period_spending(db, period).total
    earned = spending.period_income(db, period).total
    basis = basis_for_mode(mode)
    transfers = sum(
        r.amount
        for r in spending_by_category(
            db, period.start, period.end, basis=basis, include_pending=include_pending,
            kinds=("transfer",),
        )
    )  # fmt: skip

    def counted(t) -> bool:
        if t.pending and not include_pending:
            return False
        day = (t.authorized_date or t.date) if mode == "calendar" else (t.posted_date or t.date)
        return period.start <= day <= period.end

    raw = sum((t.amount for t in rows if counted(t)), D("0"))
    assert spent - earned + transfers == raw
    assert earned > 0 and spent > 0
