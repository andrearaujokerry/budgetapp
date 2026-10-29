"""Statement cycles and reconciliation (INS-4, REL-3, Gate D): ``services/statements.py``."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp.models import Statement
from budgetapp.services import statements as st
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_statement,
    make_transaction,
    set_app_settings,
)

D = dt.date
TODAY = D(2026, 10, 29)


def money(value: str) -> Decimal:
    return Decimal(value)


@pytest.fixture
def today(freeze_today):
    return freeze_today(TODAY)


@pytest.fixture
def card(db):
    return make_account(db, statement_closing_day=27)


def cycle_for(db, account, closing: dt.date) -> st.StatementCycle:
    cycle = st.find_cycle(db, account.id, closing)
    assert cycle is not None, closing
    return cycle


def summary_for(db, account, closing: dt.date) -> st.StatementSummary:
    return st.summary(db, cycle_for(db, account, closing))


# --------------------------------------------------------------------------- membership


class TestMembership:
    def test_posting_date_decides_both_boundaries(self, db, today, card):
        make_statement(db, card, "2026-08-27")
        make_statement(db, card, "2026-09-27")
        on_previous_close = make_transaction(db, card, amount="1.00", date=D(2026, 8, 27))
        first_day = make_transaction(db, card, amount="2.00", date=D(2026, 8, 28))
        on_close = make_transaction(db, card, amount="4.00", date=D(2026, 9, 27))
        after_close = make_transaction(db, card, amount="8.00", date=D(2026, 9, 28))
        lines = st.transactions_in(db, cycle_for(db, card, D(2026, 9, 27)))
        ids = {line.transaction_id for line in lines}
        assert ids == {first_day.id, on_close.id}
        assert on_previous_close.id not in ids and after_close.id not in ids
        assert summary_for(db, card, D(2026, 9, 27)).net_activity == money("6.00")

    def test_authorized_before_the_close_but_posted_after_belongs_to_the_next(
        self, db, today, card
    ):
        make_statement(db, card, "2026-09-27")
        make_transaction(
            db, card, amount="50.00", date=D(2026, 9, 29), authorized_date=D(2026, 9, 26)
        )
        assert summary_for(db, card, D(2026, 9, 27)).net_activity == money("0.00")
        assert summary_for(db, card, D(2026, 10, 27)).net_activity == money("50.00")

    def test_pending_rows_never_count(self, db, today, card):
        make_statement(db, card, "2026-09-27")
        make_transaction(db, card, amount="20.00", date=D(2026, 9, 10))
        make_transaction(db, card, amount="99.00", date=D(2026, 9, 12), pending=True)
        cycle = cycle_for(db, card, D(2026, 9, 27))
        assert st.summary(db, cycle).net_activity == money("20.00")
        assert [line.amount for line in st.transactions_in(db, cycle)] == [money("20.00")]
        assert st.pending_in(db, cycle) == st.PendingInfo(1, money("99.00"))

    def test_suspected_duplicates_never_count(self, db, today, card):
        make_statement(db, card, "2026-09-27")
        make_transaction(db, card, amount="20.00", date=D(2026, 9, 10))
        make_transaction(
            db, card, amount="20.00", date=D(2026, 9, 10), source="csv", duplicate_status="suspect"
        )
        make_transaction(
            db, card, amount="5.00", date=D(2026, 9, 11), source="csv", duplicate_status="kept"
        )
        assert summary_for(db, card, D(2026, 9, 27)).net_activity == money("25.00")

    def test_other_cards_are_never_summed(self, db, today, card):
        other = make_account(db, statement_closing_day=27)
        make_statement(db, card, "2026-09-27")
        make_statement(db, other, "2026-09-27")
        make_transaction(db, card, amount="10.00", date=D(2026, 9, 5))
        make_transaction(db, other, amount="700.00", date=D(2026, 9, 5))
        assert summary_for(db, card, D(2026, 9, 27)).net_activity == money("10.00")
        assert summary_for(db, other, D(2026, 9, 27)).net_activity == money("700.00")
        rows = st.overview(db, card.id)
        assert sum(r.net_activity for r in rows) == money("10.00")


# --------------------------------------------------------------------------- buckets


def bucket_of(db, card, **kwargs) -> str:
    """The bucket of one transaction posted on 2026-09-10 in the cycle closing 2026-09-27."""
    txn = make_transaction(db, card, date=D(2026, 9, 10), **kwargs)
    lines = st.transactions_in(db, cycle_for(db, card, D(2026, 9, 27)))
    return next(line.bucket for line in lines if line.transaction_id == txn.id)


class TestBuckets:
    @pytest.fixture(autouse=True)
    def _statement(self, db, today, card):
        make_statement(db, card, "2026-09-27")

    def test_payment_by_transaction_code(self, db, card):
        assert bucket_of(db, card, amount="-500.00", transaction_code="payment") == st.PAYMENTS

    def test_payment_by_card_payments_category_without_a_code(self, db, card):
        assert bucket_of(db, card, amount="-500.00", category=card_payments(db)) == st.PAYMENTS

    def test_refunds_and_statement_credits_are_credits(self, db, card):
        groceries = get_category(db, "Groceries")
        assert (
            bucket_of(db, card, amount="-12.50", transaction_code="refund", category=groceries)
            == st.CREDITS
        )
        assert bucket_of(db, card, amount="-30.00", name="AMEX OFFER CREDIT") == st.CREDITS

    def test_interest_by_code_by_pfc_and_by_csv_description(self, db, card):
        assert bucket_of(db, card, amount="19.84", transaction_code="interest") == st.INTEREST
        assert (
            bucket_of(
                db,
                card,
                amount="19.84",
                pfc_primary="BANK_FEES",
                pfc_detailed="BANK_FEES_INTEREST_CHARGE",
            )
            == st.INTEREST
        )
        assert (
            bucket_of(
                db,
                card,
                amount="19.84",
                source="csv",
                name="INTEREST CHARGE ON PURCHASES",
                external_category="Fees & Adjustments-Fees & Adjustments",
            )
            == st.INTEREST
        )

    def test_fees_by_code_by_pfc_and_by_csv_category(self, db, card):
        assert bucket_of(db, card, amount="95.00", transaction_code="membership fee") == st.FEES
        assert bucket_of(db, card, amount="1.42", transaction_code="bank charge") == st.FEES
        assert bucket_of(db, card, amount="39.00", transaction_code="late fee") == st.FEES
        assert (
            bucket_of(
                db,
                card,
                amount="95.00",
                pfc_primary="BANK_FEES",
                pfc_detailed="BANK_FEES_OTHER_BANK_FEES",
            )
            == st.FEES
        )
        assert (
            bucket_of(
                db,
                card,
                amount="95.00",
                source="csv",
                name="ANNUAL MEMBERSHIP FEE",
                external_category="Fees & Adjustments-Fees & Adjustments",
            )
            == st.FEES
        )

    def test_sign_comes_first(self, db, card):
        # A fee reversal is a credit; a returned payment (positive, in Card Payments) a charge.
        assert bucket_of(db, card, amount="-95.00", transaction_code="membership fee") == st.CREDITS
        assert (
            bucket_of(
                db, card, amount="-5.00", source="csv", external_category="Fees & Adjustments"
            )
            == st.CREDITS
        )
        assert (
            bucket_of(
                db, card, amount="500.00", category=card_payments(db), name="PAYMENT RETURNED"
            )
            == st.NEW_CHARGES
        )

    def test_everything_else_is_a_new_charge(self, db, card):
        assert bucket_of(db, card, amount="42.00", transaction_code="purchase") == st.NEW_CHARGES
        assert bucket_of(db, card, amount="42.00") == st.NEW_CHARGES
        assert (
            bucket_of(db, card, amount="42.00", source="csv", external_category="Restaurant")
            == st.NEW_CHARGES
        )

    def test_summary_totals_per_bucket(self, db, card):
        make_transaction(
            db, card, amount="-1530.00", date=D(2026, 9, 5), transaction_code="payment"
        )
        make_transaction(db, card, amount="-30.00", date=D(2026, 9, 6), transaction_code="refund")
        make_transaction(db, card, amount="1387.84", date=D(2026, 9, 7))
        make_transaction(db, card, amount="95.00", date=D(2026, 9, 8), transaction_code="late fee")
        summary = summary_for(db, card, D(2026, 9, 27))
        assert (summary.payments, summary.credits) == (money("-1530.00"), money("-30.00"))
        assert (summary.new_charges, summary.fees, summary.interest) == (
            money("1387.84"),
            money("95.00"),
            money("0.00"),
        )
        assert summary.payments_credits == money("-1560.00")
        assert summary.net_activity == money("-77.16")
        assert summary.count(st.PAYMENTS) == 1 and summary.transaction_count == 4

    def test_interest_dated_on_the_closing_date_belongs_to_that_statement(self, db, card):
        make_transaction(db, card, amount="19.84", date=D(2026, 9, 27), transaction_code="interest")
        assert summary_for(db, card, D(2026, 9, 27)).interest == money("19.84")
        assert summary_for(db, card, D(2026, 10, 27)).interest == money("0.00")

    def test_groups_follow_the_statement_sections(self, db, card):
        make_transaction(db, card, amount="10.00", date=D(2026, 9, 3))
        make_transaction(db, card, amount="-10.00", date=D(2026, 9, 4), transaction_code="payment")
        make_transaction(db, card, amount="1.00", date=D(2026, 9, 5), transaction_code="interest")
        lines = st.transactions_in(db, cycle_for(db, card, D(2026, 9, 27)))
        groups = st.group_by_bucket(lines)
        assert [g.label for g in groups] == ["Payments", "New Charges", "Interest Charged"]
        assert [g.total for g in groups] == [money("-10.00"), money("10.00"), money("1.00")]


# --------------------------------------------------------------------------- cycles


class TestCycles:
    def test_closing_day_31_across_february_in_a_leap_year(self, db, freeze_today):
        freeze_today(D(2024, 4, 10))
        card = make_account(db, statement_closing_day=31)
        make_transaction(db, card, amount="1.00", date=D(2024, 1, 5))
        closings = sorted(c.closing_date for c in st.cycles(db, card.id))
        assert closings == [D(2024, 1, 31), D(2024, 2, 29), D(2024, 3, 31), D(2024, 4, 30)]
        feb = st.find_cycle(db, card.id, D(2024, 2, 29))
        assert (feb.start, feb.days, feb.generated, feb.flagged) == (D(2024, 2, 1), 29, True, False)

    def test_closing_day_31_across_february_in_a_common_year(self, db, freeze_today):
        freeze_today(D(2025, 3, 15))
        card = make_account(db, statement_closing_day=31)
        make_transaction(db, card, amount="1.00", date=D(2025, 1, 20))
        closings = sorted(c.closing_date for c in st.cycles(db, card.id))
        assert closings == [D(2025, 1, 31), D(2025, 2, 28), D(2025, 3, 31)]
        assert st.find_cycle(db, card.id, D(2025, 3, 31)).start == D(2025, 3, 1)

    def test_newest_first_through_the_open_cycle(self, db, today, card):
        make_transaction(db, card, amount="1.00", date=D(2026, 7, 2))
        found = st.cycles(db, card.id)
        assert [c.closing_date for c in found] == [
            D(2026, 11, 27),
            D(2026, 10, 27),
            D(2026, 9, 27),
            D(2026, 8, 27),
            D(2026, 7, 27),
        ]
        assert [c.status for c in found] == [st.OPEN, st.SETTLING, st.FINAL, st.FINAL, st.FINAL]
        assert all(c.generated for c in found)
        assert (found[0].start, found[-1].start) == (D(2026, 10, 28), D(2026, 6, 28))

    def test_stored_dates_win_and_generation_stays_outside_them(self, db, today, card):
        make_statement(db, card, "2026-08-28", closing_date_source="liabilities")
        make_statement(db, card, "2026-09-26", closing_date_source="liabilities")
        make_transaction(db, card, amount="1.00", date=D(2026, 7, 1))
        found = {c.closing_date: c for c in st.cycles(db, card.id)}
        assert D(2026, 8, 27) not in found and D(2026, 9, 27) not in found  # no phantoms
        assert found[D(2026, 9, 26)].start == D(2026, 8, 29)
        assert not found[D(2026, 9, 26)].generated
        assert found[D(2026, 10, 27)].generated and found[D(2026, 10, 27)].start == D(2026, 9, 27)

    def test_a_missing_statement_shows_as_a_flagged_long_cycle(self, db, today, card):
        make_statement(db, card, "2026-07-27")
        make_statement(db, card, "2026-09-27")
        cycle = st.find_cycle(db, card.id, D(2026, 9, 27))
        assert cycle.start == D(2026, 7, 28) and cycle.days == 62 and cycle.flagged
        assert st.find_cycle(db, card.id, D(2026, 8, 27)) is None

    def test_the_earliest_statement_may_override_its_start(self, db, today, card):
        make_statement(db, card, "2026-09-27", period_start_override=D(2026, 8, 25))
        make_statement(db, card, "2026-10-27")
        first = st.find_cycle(db, card.id, D(2026, 9, 27))
        assert first.start == D(2026, 8, 25) and first.is_earliest_statement
        assert first.period_start_override == D(2026, 8, 25)
        before = st.find_cycle(db, card.id, D(2026, 8, 24))
        assert before is not None and before.generated
        later = st.find_cycle(db, card.id, D(2026, 10, 27))
        assert later.period_start_override is None and not later.can_set_period_start

    def test_without_dates_or_a_closing_day_there_are_no_cycles(self, db, today):
        card = make_account(db)
        make_transaction(db, card, amount="1.00", date=D(2026, 9, 1))
        assert st.cycles(db, card.id) == []
        assert st.overview(db, card.id) == []
        assert st.find_cycle(db, card.id, D(2026, 9, 27)) is None

    def test_a_stored_date_alone_is_enough(self, db, today):
        card = make_account(db)
        make_statement(db, card, "2026-09-26")
        closings = [c.closing_date for c in st.cycles(db, card.id)]
        assert closings == [D(2026, 11, 26), D(2026, 10, 26), D(2026, 9, 26)]

    def test_unknown_account(self, db, today):
        with pytest.raises(LookupError):
            st.cycles(db, 999_999)

    def test_a_user_date_overrides_a_nearby_liabilities_snapshot(self, db, today, card):
        user = make_statement(db, card, "2026-09-25", closing_date_source="user")
        make_statement(
            db,
            card,
            "2026-09-30",
            closing_date_source="liabilities",
            plaid_issue_date=D(2026, 9, 30),
            reported_new_balance=money("1234.56"),
            reported_new_balance_source="liabilities",
        )
        found = {c.closing_date: c for c in st.cycles(db, card.id)}
        assert D(2026, 9, 30) not in found
        cycle = found[D(2026, 9, 25)]
        assert cycle.statement_id == user.id and cycle.closing_date_source == "user"
        assert cycle.plaid_issue_date == D(2026, 9, 30)
        assert cycle.reported.new_balance == money("1234.56")
        assert cycle.reported.new_balance_source == "liabilities"
        assert found[D(2026, 10, 27)].start == D(2026, 9, 26)

    def test_correcting_a_snapshot_date_makes_it_the_users(self, db, today, card):
        row = make_statement(
            db,
            card,
            "2026-09-28",
            closing_date_source="liabilities",
            plaid_issue_date=D(2026, 9, 28),
        )
        st.set_closing_date(db, card.id, D(2026, 9, 28), D(2026, 9, 27))
        assert (row.closing_date, row.closing_date_source) == (D(2026, 9, 27), "user")
        assert row.plaid_issue_date == D(2026, 9, 28)  # the snapshot key stays
        cycle = st.find_cycle(db, card.id, D(2026, 9, 27))
        assert cycle.statement_id == row.id and cycle.closing_date_source == "user"


class TestStatus:
    @pytest.mark.parametrize(
        ("today", "settle_days", "status"),
        [
            (D(2026, 9, 26), 3, st.OPEN),
            (D(2026, 9, 27), 3, st.OPEN),  # the closing day itself is still open
            (D(2026, 9, 28), 3, st.SETTLING),
            (D(2026, 9, 30), 3, st.SETTLING),
            (D(2026, 10, 1), 3, st.FINAL),
            (D(2026, 9, 28), 0, st.FINAL),
        ],
    )
    def test_open_settling_final(self, db, freeze_today, card, today, settle_days, status):
        freeze_today(today)
        set_app_settings(db, statement_settle_days=settle_days)
        make_statement(db, card, "2026-09-27")
        cycle = st.find_cycle(db, card.id, D(2026, 9, 27))
        assert cycle.status == status
        assert cycle.final_on == D(2026, 9, 27) + dt.timedelta(days=settle_days + 1)

    def test_no_cycle_after_the_open_one(self, db, today, card):
        assert st.find_cycle(db, card.id, D(2026, 11, 27)) is not None
        assert st.find_cycle(db, card.id, D(2026, 12, 27)) is None


# --------------------------------------------------------------------------- Gate D


def two_statements(db, card, *, new_balance: str, previous_new: str = "1000.00"):
    make_statement(
        db,
        card,
        "2026-08-27",
        reported_new_balance=money(previous_new),
        reported_new_balance_source="liabilities",
    )
    return make_statement(
        db,
        card,
        "2026-09-27",
        reported_new_balance=money(new_balance),
        reported_new_balance_source="liabilities",
    )


class TestGateD:
    @pytest.fixture(autouse=True)
    def _activity(self, db, today, card):
        make_transaction(
            db, card, amount="-1000.00", date=D(2026, 9, 21), transaction_code="payment"
        )
        make_transaction(db, card, amount="1387.84", date=D(2026, 9, 2))
        make_transaction(db, card, amount="19.84", date=D(2026, 9, 27), transaction_code="interest")

    def test_passes_to_the_cent(self, db, card):
        two_statements(db, card, new_balance="1407.68")
        summary = summary_for(db, card, D(2026, 9, 27))
        assert summary.net_activity == money("407.68")
        check = summary.check
        assert check.passed and not check.failed and not check.provisional
        assert (check.previous_balance, check.previous_source) == (
            money("1000.00"),
            "previous_statement",
        )
        assert check.expected_activity == money("407.68") and check.difference == money("0.00")
        assert summary.previous_balance == money("1000.00")
        assert summary.new_balance == money("1407.68")

    def test_fails_by_one_cent(self, db, card):
        two_statements(db, card, new_balance="1407.69")
        check = summary_for(db, card, D(2026, 9, 27)).check
        assert check.failed and check.state == st.CHECK_FAIL
        assert check.difference == money("-0.01")  # the app has a cent less
        assert check.expected_activity == money("407.69")

    def test_uses_this_statements_previous_balance_when_the_previous_one_is_unknown(self, db, card):
        make_statement(
            db,
            card,
            "2026-09-27",
            reported_previous_balance=money("1000.00"),
            reported_new_balance=money("1407.68"),
        )
        summary = summary_for(db, card, D(2026, 9, 27))
        assert summary.check.passed and summary.check.previous_source == "this_statement"
        assert summary.previous_balance is None  # the app has no previous statement
        assert summary.balance_basis == "this_statement"
        assert summary.new_balance == money("1407.68")

    def test_needs_a_new_balance_and_a_previous_balance(self, db, card):
        make_statement(db, card, "2026-09-27")
        check = summary_for(db, card, D(2026, 9, 27)).check
        assert check.state == st.CHECK_UNAVAILABLE and check.missing == "new_balance"
        db.execute(Statement.__table__.update().values(reported_new_balance=money("1407.68")))
        db.expire_all()
        check = summary_for(db, card, D(2026, 9, 27)).check
        assert check.state == st.CHECK_UNAVAILABLE and check.missing == "previous_balance"

    def test_an_open_period_is_not_checked(self, db, card):
        make_statement(db, card, "2026-10-27")
        assert summary_for(db, card, D(2026, 11, 27)).check.state == st.CHECK_OPEN

    def test_a_settling_statement_is_provisional(self, db, card, freeze_today):
        freeze_today(D(2026, 9, 29))
        two_statements(db, card, new_balance="1407.68")
        check = summary_for(db, card, D(2026, 9, 27)).check
        assert check.passed and check.provisional

    def test_reported_differences_per_line(self, db, card):
        make_statement(
            db,
            card,
            "2026-08-27",
            reported_new_balance=money("1000.00"),
        )
        make_statement(
            db,
            card,
            "2026-09-27",
            reported_previous_balance=money("1000.00"),
            reported_payments_credits=money("1000.00"),
            reported_new_charges=money("1387.85"),
            reported_fees=money("0.00"),
            reported_interest=money("19.84"),
            reported_new_balance=money("1407.69"),
        )
        summary = summary_for(db, card, D(2026, 9, 27))
        lines = {f.key: f for f in summary.fields}
        assert [f.label for f in summary.fields] == [
            "Previous Balance",
            "Payments/Credits",
            "New Charges",
            "Fees",
            "Interest Charged",
            "New Balance",
        ]
        assert lines["previous_balance"].matches
        payments = lines["payments_credits"]
        assert (payments.app, payments.reported, payments.matches) == (
            money("-1000.00"),
            money("-1000.00"),
            True,
        )
        assert lines["new_charges"].difference == money("-0.01")
        assert lines["fees"].matches and lines["interest"].matches
        assert lines["new_balance"].difference == money("-0.01")
        assert summary.check.difference == money("-0.01")
        assert summary.cycle.reported.identity_difference == money("0.00")

    def test_a_typo_in_the_reported_values_is_visible(self, db, card):
        make_statement(
            db,
            card,
            "2026-09-27",
            reported_previous_balance=money("1000.00"),
            reported_payments_credits=money("1000.00"),
            reported_new_charges=money("1387.84"),
            reported_fees=money("0.00"),
            reported_interest=money("19.84"),
            reported_new_balance=money("1470.68"),
        )
        cycle = st.find_cycle(db, card.id, D(2026, 9, 27))
        assert cycle.reported.identity_difference == money("63.00")

    def test_overview_matches_summary_for_every_cycle(self, db, card):
        two_statements(db, card, new_balance="1407.69")
        make_transaction(db, card, amount="5.00", date=D(2026, 10, 3))
        for row in st.overview(db, card.id):
            single = st.summary(db, row.cycle)
            assert row == single


# --------------------------------------------------------------------------- boundaries


class TestNearBoundary:
    def test_rows_within_three_days_either_side_with_their_statement(self, db, today, card):
        make_statement(db, card, "2026-08-27")
        make_statement(db, card, "2026-09-27")
        dates = [D(2026, 8, 24), D(2026, 8, 25), D(2026, 8, 27), D(2026, 8, 30), D(2026, 8, 31)]
        for day in dates:
            make_transaction(db, card, amount="1.00", date=day)
        make_transaction(db, card, amount="2.00", date=D(2026, 9, 25))
        make_transaction(db, card, amount="3.00", date=D(2026, 9, 28))
        make_transaction(db, card, amount="4.00", date=D(2026, 9, 26), pending=True)
        start, end = st.near_boundary(db, cycle_for(db, card, D(2026, 9, 27)))
        assert start.closing_date == D(2026, 8, 27) and start.edge == "start"
        assert [(line.posted_date, line.side) for line in start.lines] == [
            (D(2026, 8, 25), "previous"),
            (D(2026, 8, 27), "previous"),
            (D(2026, 8, 30), "this"),
        ]
        assert [(line.amount, line.side) for line in end.lines] == [
            (money("2.00"), "this"),
            (money("3.00"), "next"),
            (money("4.00"), "pending"),
        ]
        assert end.lines[-1].posted_date is None and end.lines[-1].pending

    def test_days_must_be_positive(self, db, today, card):
        make_statement(db, card, "2026-09-27")
        with pytest.raises(ValueError):
            st.near_boundary(db, cycle_for(db, card, D(2026, 9, 27)), days=0)


# --------------------------------------------------------------------------- changes


class TestRecordReported:
    def test_values_as_printed_on_the_statement(self, db, today, card):
        make_statement(db, card, "2026-09-27")
        row = st.record_reported(
            db,
            card.id,
            D(2026, 9, 27),
            previous_balance="$2,749.22",
            payments_credits="−$1,530.00",
            new_charges="+1,387.84",
            fees="0",
            interest="19.84",
            new_balance="2,626.90",
            notes="  Typed from the PDF  ",
        )
        assert row.reported_previous_balance == money("2749.22")
        assert row.reported_payments_credits == money("1530.00")  # stored positive
        assert row.reported_new_charges == money("1387.84")
        assert (row.reported_fees, row.reported_interest) == (money("0.00"), money("19.84"))
        assert (row.reported_new_balance, row.reported_new_balance_source) == (
            money("2626.90"),
            "user",
        )
        assert row.notes == "Typed from the PDF"
        assert st.find_cycle(db, card.id, D(2026, 9, 27)).reported.identity_difference == 0

    def test_payments_without_the_minus_and_credit_balances(self, db, today, card):
        make_statement(db, card, "2026-09-27")
        row = st.record_reported(
            db,
            card.id,
            D(2026, 9, 27),
            previous_balance="12.34 CR",
            payments_credits="1530",
            new_balance="$5.00CR",
        )
        assert row.reported_payments_credits == money("1530.00")
        assert row.reported_previous_balance == money("-12.34")
        assert row.reported_new_balance == money("-5.00")

    def test_blank_clears_and_an_unchanged_snapshot_balance_keeps_its_source(self, db, today, card):
        row = make_statement(
            db,
            card,
            "2026-09-27",
            closing_date_source="liabilities",
            reported_new_balance=money("100.00"),
            reported_new_balance_source="liabilities",
            reported_fees=money("95.00"),
        )
        st.record_reported(db, card.id, D(2026, 9, 27), new_balance="100.00", fees="")
        assert row.reported_new_balance_source == "liabilities"
        assert row.reported_fees is None
        st.record_reported(db, card.id, D(2026, 9, 27), new_balance="")
        assert (row.reported_new_balance, row.reported_new_balance_source) == (None, None)

    def test_errors_name_every_bad_field_and_change_nothing(self, db, today, card):
        row = make_statement(db, card, "2026-09-27", reported_fees=money("1.00"))
        with pytest.raises(st.StatementError) as err:
            st.record_reported(
                db,
                card.id,
                D(2026, 9, 27),
                fees="ten",
                interest="1.234",
                new_balance="1e3",
                notes="x" * (st.MAX_NOTES_LENGTH + 1),
            )
        assert set(err.value.errors) == {"fees", "interest", "new_balance", "notes"}
        assert "two decimal places" in err.value.errors["interest"]
        assert row.reported_fees == money("1.00")

    def test_a_generated_cycle_gets_a_user_statement(self, db, today, card):
        assert db.scalars(select(Statement)).all() == []
        row = st.record_reported(db, card.id, D(2026, 9, 27), new_balance="10.00")
        assert (row.closing_date, row.closing_date_source) == (D(2026, 9, 27), "user")
        assert st.find_cycle(db, card.id, D(2026, 9, 27)).statement_id == row.id

    def test_unknown_cycle(self, db, today, card):
        with pytest.raises(LookupError):
            st.record_reported(db, card.id, D(2026, 9, 15), new_balance="1")
        with pytest.raises(LookupError):
            st.record_reported(db, 999_999, D(2026, 9, 27), new_balance="1")


class TestClosingDates:
    def test_moving_a_closing_date(self, db, today, card):
        make_statement(db, card, "2026-08-27")
        row = make_statement(db, card, "2026-09-27", closing_date_source="liabilities")
        make_statement(db, card, "2026-10-27")
        st.set_closing_date(db, card.id, D(2026, 9, 27), "2026-09-25")
        assert (row.closing_date, row.closing_date_source) == (D(2026, 9, 25), "user")
        assert st.find_cycle(db, card.id, D(2026, 10, 27)).start == D(2026, 9, 26)

    @pytest.mark.parametrize(
        ("new", "message"),
        [
            ("2026-08-27", "already closes"),
            ("2026-10-20", "edit that one instead"),
            ("2026-11-15", "between the statements"),
            ("27/09/2026", "Enter a date"),
        ],
    )
    def test_refused_moves(self, db, today, card, new, message):
        make_statement(db, card, "2026-08-27")
        row = make_statement(db, card, "2026-09-27")
        make_statement(db, card, "2026-10-27")
        with pytest.raises(st.StatementError) as err:
            st.set_closing_date(db, card.id, D(2026, 9, 27), new)
        assert message in err.value.errors["closing_date"]
        assert row.closing_date == D(2026, 9, 27)

    def test_not_before_the_period_start(self, db, today, card):
        make_statement(db, card, "2026-09-27", period_start_override=D(2026, 9, 1))
        with pytest.raises(st.StatementError) as err:
            st.set_closing_date(db, card.id, D(2026, 9, 27), "2026-08-30")
        assert "period start" in err.value.errors["closing_date"]

    def test_period_start_only_for_the_earliest_statement(self, db, today, card):
        first = make_statement(db, card, "2026-08-27")
        make_statement(db, card, "2026-09-27")
        st.set_period_start(db, card.id, D(2026, 8, 27), "2026-07-30")
        assert first.period_start_override == D(2026, 7, 30)
        assert st.find_cycle(db, card.id, D(2026, 8, 27)).start == D(2026, 7, 30)
        with pytest.raises(st.StatementError) as err:
            st.set_period_start(db, card.id, D(2026, 9, 27), "2026-08-28")
        assert "earliest" in err.value.errors["period_start"]
        with pytest.raises(st.StatementError) as err:
            st.set_period_start(db, card.id, D(2026, 8, 27), "2026-08-28")
        assert "on or before" in err.value.errors["period_start"]
        st.set_period_start(db, card.id, D(2026, 8, 27), None)
        assert first.period_start_override is None

    def test_save_statement_validates_everything_first(self, db, today, card):
        row = make_statement(db, card, "2026-09-27")
        make_statement(db, card, "2026-10-27")
        with pytest.raises(st.StatementError) as err:
            st.save_statement(
                db,
                card.id,
                D(2026, 9, 27),
                {"closing_date": "2026-10-25", "fees": "abc", "new_balance": "12.00"},
            )
        assert set(err.value.errors) == {"closing_date", "fees"}
        assert row.reported_new_balance is None and row.closing_date == D(2026, 9, 27)
        saved = st.save_statement(
            db, card.id, D(2026, 9, 27), {"closing_date": "2026-09-26", "new_balance": "12.00"}
        )
        assert saved is row
        assert (row.closing_date, row.reported_new_balance) == (D(2026, 9, 26), money("12.00"))


class TestAddAndCheck:
    def test_add_a_statement(self, db, today, card):
        row = st.add_statement(db, card.id, "2026-09-26")
        assert (row.closing_date, row.closing_date_source) == (D(2026, 9, 26), "user")
        assert st.find_cycle(db, card.id, D(2026, 9, 26)).statement_id == row.id

    @pytest.mark.parametrize(
        ("day", "message"),
        [
            ("2026-09-27", "already closes"),
            ("2026-09-30", "edit that one instead"),
            ("2027-03-01", "too far in the future"),
            ("", "Enter a date"),
        ],
    )
    def test_refused_additions(self, db, today, card, day, message):
        make_statement(db, card, "2026-09-27")
        with pytest.raises(st.StatementError) as err:
            st.add_statement(db, card.id, day)
        assert message in err.value.errors["closing_date"]

    def test_mark_checked_and_unchecked(self, db, freeze_today, card):
        now = dt.datetime(2026, 10, 29, 18, 0, tzinfo=dt.UTC)
        freeze_today(now=now)
        row = make_statement(db, card, "2026-09-27")
        st.mark_checked(db, card.id, D(2026, 9, 27))
        assert row.checked_at == now
        assert st.find_cycle(db, card.id, D(2026, 9, 27)).checked_at == now
        st.mark_checked(db, card.id, D(2026, 9, 27), checked=False)
        assert row.checked_at is None


class TestAccounts:
    def test_linked_cards_first_and_the_budget_card_by_default(self, db):
        manual = make_account(db, manual=True)
        linked = make_account(db)
        hidden = make_account(db, is_hidden=True)
        options = st.accounts(db)
        assert [o.id for o in options] == [linked.id, manual.id, hidden.id]
        assert st.default_account(db, options).id == linked.id
        set_app_settings(db, budget_cycle_account_id=manual.id)
        assert st.default_account(db, options).id == manual.id
        assert st.default_account(db, []) is None

    def test_detail_bundles_the_page(self, db, today, card):
        make_statement(db, card, "2026-08-27")
        make_statement(db, card, "2026-09-27")
        make_transaction(db, card, amount="3.00", date=D(2026, 9, 3))
        detail = st.detail(db, card.id, D(2026, 9, 27))
        assert detail.cycle.closing_date == D(2026, 9, 27)
        assert detail.later.closing_date == D(2026, 10, 27)
        assert detail.earlier.closing_date == D(2026, 8, 27)
        assert [g.bucket for g in detail.groups] == [st.NEW_CHARGES]
        assert len(detail.boundaries) == 2
        assert st.detail(db, card.id, D(2026, 9, 20)) is None
        assert st.detail(db, 999_999, D(2026, 9, 27)) is None
