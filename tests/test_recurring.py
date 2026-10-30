"""Recurring charges and subscriptions (INS-3): ``services/recurring.py``."""

from __future__ import annotations

import datetime as dt
import random
import time
from decimal import Decimal

import pytest
from sqlalchemy import insert, select

from budgetapp import periods
from budgetapp.models import RecurringOverride, Transaction
from budgetapp.services import recurring as rec
from budgetapp.services import transactions as txns
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_recurring_override,
    make_split,
    make_transaction,
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
    return make_account(db)


def monthly_dates(first: dt.date, count: int, *, skip: tuple[dt.date, ...] = ()) -> list[dt.date]:
    """``count`` dates one calendar month apart on ``first.day`` (clamped), minus ``skip``."""
    out = []
    for i in range(count):
        year, month = periods.add_months(first.year, first.month, i)
        day = periods.clamp_day(year, month, first.day)
        if day not in skip:
            out.append(day)
    return out


def every(first: dt.date, days: int, count: int) -> list[dt.date]:
    return [first + dt.timedelta(days=days * i) for i in range(count)]


def charge_on(db, card, days, amount="15.49", **kwargs):
    return [make_transaction(db, card, amount=amount, date=day, **kwargs) for day in days]


def found(overview: rec.RecurringOverview) -> dict[str, rec.RecurringSeries]:
    """Every listed series (active, lapsed and ignored) by merchant."""
    return {s.merchant: s for s in (*overview.active, *overview.lapsed, *overview.ignored)}


# --------------------------------------------------------------------------- merchant keys


class TestMerchantKey:
    @pytest.mark.parametrize(
        ("kwargs", "key"),
        [
            ({"merchant_entity_id": " ent-123 ", "merchant_name": "Netflix"}, "ent-123"),
            ({"merchant_entity_id": "  ", "merchant_name": "Netflix"}, "netflix"),
            ({"merchant_name": "Netflix", "name": "NETFLIX.COM 866-579-7172"}, "netflix"),
            ({"name": "NETFLIX.COM 866-579-7172"}, "netflix com"),
            ({"name": "AMAZON MKTPL*JA3YVME"}, "amazon mktpl"),
            ({"name": "TRADER JOE S #236"}, "trader joe s"),
            ({"name": "SQ *BLUE BOTTLE COFFEE 0123"}, "blue bottle coffee"),
            ({"name": "TST* FLOUR + WATER"}, "flour water"),
            ({"name": "  Spotify   USA  "}, "spotify usa"),
            ({"name": "1234"}, "1234"),
        ],
    )
    def test_keys(self, kwargs, key):
        assert rec.merchant_key(**kwargs) == key

    def test_store_numbers_and_suffixes_group_together(self):
        assert rec.merchant_key(name="WALGREENS #1234") == rec.merchant_key(name="WALGREENS #5678")
        assert rec.merchant_key(name="APPLE.COM/BILL*AB12") == rec.merchant_key(
            name="APPLE.COM/BILL*ZZ99"
        )

    @pytest.mark.parametrize(
        ("merchant_name", "name", "text"),
        [
            ("Netflix", "NETFLIX.COM", "Netflix"),
            (None, "SQ *BLUE BOTTLE 0123", "BLUE BOTTLE"),
            (None, "AMAZON MKTPL*JA3YVME", "AMAZON MKTPL"),
            (None, "TRADER JOE S #236", "TRADER JOE S"),
            (None, "7-ELEVEN", "7-ELEVEN"),
        ],
    )
    def test_search_text_for_the_transactions_page(self, merchant_name, name, text):
        assert rec.search_text(merchant_name, name) == text


# --------------------------------------------------------------------------- detection


class TestCadences:
    def test_monthly(self, db, today, card):
        streaming = get_category(db, "Subscriptions & Streaming")
        charge_on(
            db,
            card,
            monthly_dates(D(2025, 11, 5), 12),
            amount="15.49",
            merchant_name="Netflix",
            name="NETFLIX.COM",
            category=streaming,
        )
        overview = rec.detect(db)
        assert [s.merchant for s in overview.active] == ["Netflix"]
        s = overview.active[0]
        assert (s.key, s.cadence, s.cadence_label) == ("netflix", "monthly", "Monthly")
        assert s.interval_days == 31  # median of 28/30/31-day months
        assert (s.typical_amount, s.last_amount, s.previous_amount) == (
            money("15.49"),
            money("15.49"),
            None,
        )
        assert (s.first_date, s.last_date, s.count) == (D(2025, 11, 5), D(2026, 10, 5), 12)
        assert s.next_expected == D(2026, 11, 5)  # the billing day, not Oct 5 + 31
        assert s.monthly_cost == money("15.49")
        assert s.category.name == "Subscriptions & Streaming"
        assert s.active and not s.late and s.detected and s.status is None
        assert s.search_text == "Netflix"
        assert overview.monthly_total == money("15.49")
        assert overview.yearly_total == money("185.88")
        assert overview.next_up == s

    def test_annual_needs_two_charges(self, db, today, card):
        charge_on(
            db, card, [D(2025, 10, 15), D(2026, 10, 14)], amount="139.00", name="AMAZON PRIME"
        )
        charge_on(db, card, [D(2026, 3, 2)], amount="99.00", name="COSTCO MEMBERSHIP")
        overview = rec.detect(db)
        assert [s.merchant for s in overview.active] == ["AMAZON PRIME"]
        s = overview.active[0]
        assert (s.cadence, s.cadence_label, s.interval_days) == ("annual", "Yearly", 364)
        assert s.next_expected == D(2027, 10, 15)
        assert s.monthly_cost == money("11.58")  # 139 × 30.44 / 365.28 = 139 / 12

    def test_weekly(self, db, today, card):
        charge_on(
            db, card, every(D(2026, 8, 26), 7, 10), amount="59.99", merchant_name="HelloFresh"
        )
        s = rec.detect(db).active[0]
        assert (s.cadence, s.interval_days, s.count) == ("weekly", 7, 10)
        assert s.next_expected == D(2026, 11, 4)
        assert s.monthly_cost == money("260.87")  # 59.99 × 30.44 / 7

    def test_every_two_weeks(self, db, today, card):
        charge_on(db, card, every(D(2026, 7, 10), 14, 8), amount="120.00", name="MERRY MAIDS")
        s = rec.detect(db).active[0]
        assert (s.cadence, s.cadence_label, s.interval_days) == ("biweekly", "Every 2 weeks", 14)
        assert s.next_expected == D(2026, 10, 30)
        assert s.monthly_cost == money("260.91")  # 120 × 30.44 / 14

    def test_quarterly(self, db, today, card):
        days = [D(2025, 11, 1), D(2026, 2, 1), D(2026, 5, 1), D(2026, 8, 1)]
        charge_on(db, card, days, amount="45.00", name="STATE FARM INSURANCE")
        s = rec.detect(db).active[0]
        assert (s.cadence, s.interval_days) == ("quarterly", 92)
        assert s.next_expected == D(2026, 11, 1)
        assert s.monthly_cost == money("15.00")
        assert s.active  # 89 days since the last charge, under 1.5 × 91

    def test_a_skipped_month_is_still_monthly(self, db, today, card):
        days = monthly_dates(D(2025, 11, 1), 12, skip=(D(2026, 3, 1),))
        charge_on(db, card, days, amount="40.00", name="EQUINOX GYM")
        s = rec.detect(db).active[0]
        assert (s.cadence, s.count, s.interval_days) == ("monthly", 11, 31)
        assert s.next_expected == D(2026, 11, 1)

    def test_a_day_of_month_billing_ignores_weekend_shifts(self, db, today, card):
        days = monthly_dates(D(2025, 11, 15), 12)
        days[-1] = D(2026, 10, 17)  # charged two days late once
        charge_on(db, card, days, amount="9.99", name="HULU")
        assert rec.detect(db).active[0].next_expected == D(2026, 11, 15)

    def test_without_a_billing_day_the_median_interval_is_added(self, db, today, card):
        days = [D(2026, 6, 1), D(2026, 7, 1), D(2026, 7, 30), D(2026, 8, 28), D(2026, 9, 27)]
        charge_on(db, card, days, amount="20.00", name="DRIFTING CO")
        s = rec.detect(db).active[0]
        assert s.interval_days == 30
        assert s.next_expected == D(2026, 10, 27)


class TestAmounts:
    def test_a_small_price_change_shows_the_old_price(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 12), 7), amount="10.99", name="SPOTIFY")
        charge_on(db, card, monthly_dates(D(2026, 6, 12), 5), amount="11.99", name="SPOTIFY")
        s = rec.detect(db).active[0]
        assert (s.typical_amount, s.previous_amount, s.price_changed) == (
            money("11.99"),
            money("10.99"),
            True,
        )
        assert s.count == 12 and s.monthly_cost == money("11.99")

    def test_a_large_price_change_is_one_series(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 20), 6), amount="9.99", name="YOUTUBE TV")
        charge_on(db, card, monthly_dates(D(2026, 5, 20), 6), amount="15.99", name="YOUTUBE TV")
        overview = rec.detect(db)
        assert len(overview.active) == 1
        s = overview.active[0]
        assert (s.count, s.typical_amount, s.previous_amount) == (
            12,
            money("15.99"),
            money("9.99"),
        )

    def test_two_price_changes_are_still_one_series(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 3), 4), amount="5.00", name="ODD CO")
        charge_on(db, card, monthly_dates(D(2026, 3, 3), 4), amount="10.00", name="ODD CO")
        charge_on(db, card, monthly_dates(D(2026, 7, 3), 4), amount="20.00", name="ODD CO")
        overview = rec.detect(db)
        assert overview.lapsed == () and len(overview.active) == 1
        s = overview.active[0]
        assert (s.count, s.first_date, s.typical_amount, s.previous_amount) == (
            12,
            D(2025, 11, 3),
            money("20.00"),
            money("10.00"),
        )
        assert (s.cadence, s.next_expected) == ("monthly", D(2026, 11, 3))

    def test_a_price_change_next_to_another_subscription_of_the_merchant(self, db, today, card):
        apple = {"merchant_entity_id": "apple-entity", "merchant_name": "Apple"}
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 6), amount="0.99", **apple)
        charge_on(db, card, monthly_dates(D(2026, 5, 10), 6), amount="2.99", **apple)
        active = sorted(rec.detect(db).active, key=lambda s: s.typical_amount)
        assert [(s.typical_amount, s.previous_amount, s.count) for s in active] == [
            (money("2.99"), money("0.99"), 12),
            (money("10.99"), None, 12),
        ]

    def test_a_level_that_stopped_long_before_the_next_stays_separate(self, db, today, card):
        apple = {"merchant_entity_id": "apple-entity", "merchant_name": "Apple"}
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **apple)
        charge_on(db, card, monthly_dates(D(2025, 10, 10), 4), amount="0.99", **apple)
        charge_on(db, card, monthly_dates(D(2026, 7, 10), 4), amount="2.99", **apple)
        overview = rec.detect(db)
        assert sorted(s.typical_amount for s in overview.active) == [money("2.99"), money("10.99")]
        assert [(s.typical_amount, s.last_date) for s in overview.lapsed] == [
            (money("0.99"), D(2026, 1, 10))
        ]

    def test_a_varying_bill_within_twenty_percent(self, db, today, card):
        amounts = ["84.10", "91.35", "78.20", "99.80", "88.00", "95.55"]
        for day, amount in zip(monthly_dates(D(2026, 5, 9), 6), amounts, strict=True):
            make_transaction(db, card, amount=amount, date=day, name="PG&E WEB ONLINE")
        s = rec.detect(db).active[0]
        assert s.cadence == "monthly" and s.previous_amount is None
        assert s.typical_amount == money("95.55")  # median of the last three

    def test_several_subscriptions_from_one_merchant(self, db, today, card):
        entity = {"merchant_entity_id": "apple-entity", "merchant_name": "Apple"}
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 12), amount="2.99", **entity)
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **entity)
        overview = rec.detect(db)
        assert sorted(s.typical_amount for s in overview.active) == [
            money("2.99"),
            money("10.99"),
        ]
        # Each has its own override key; the merchant's key still applies to both.
        assert {s.key for s in overview.active} == {"apple-entity|2.99", "apple-entity|10.99"}
        assert all(s.partial and s.also == ("apple-entity",) for s in overview.active)
        assert overview.monthly_total == money("13.98")


class TestNotRecurring:
    def test_irregular_visits_at_a_steady_amount(self, db, today, card):
        days = [D(2026, 1, 3), D(2026, 1, 6), D(2026, 1, 23), D(2026, 3, 4), D(2026, 3, 12)]
        days += [D(2026, 4, 3), D(2026, 6, 2)]
        charge_on(db, card, days, amount="25.00", name="TARTINE BAKERY")
        assert rec.detect(db).is_empty

    def test_a_habit_whose_median_fits_is_not_a_subscription(self, db, today, card):
        day, days = D(2026, 1, 5), [D(2026, 1, 5)]
        for gap in (12, 13, 11, 25, 5, 12, 3, 30, 14):  # median 12, but only 5 of 9 in band
            day += dt.timedelta(days=gap)
            days.append(day)
        charge_on(db, card, days, amount="14.50", name="SWEETGREEN")
        assert rec.detect(db).is_empty

    def test_weekly_groceries_of_any_amount(self, db, today, card):
        amounts = ["40.00", "150.00", "75.00", "90.00", "30.00", "120.00", "60.00", "45.00"]
        for day, amount in zip(every(D(2026, 8, 30), 7, 8), amounts, strict=True):
            make_transaction(db, card, amount=amount, date=day, name="SAFEWAY #1711")
        assert rec.detect(db).is_empty

    def test_two_monthly_charges_are_not_enough(self, db, today, card):
        charge_on(db, card, [D(2026, 9, 3), D(2026, 10, 3)], amount="5.00", name="PATREON")
        assert rec.detect(db).is_empty

    def test_only_posted_positive_expense_charges_count(self, db, today, card):
        days = monthly_dates(D(2026, 6, 8), 4)
        charge_on(db, card, days[:2], amount="12.00", name="PENDING CO")
        charge_on(db, card, days[2:], amount="12.00", name="PENDING CO", pending=True)
        charge_on(db, card, days, amount="-50.00", name="AMEX OFFER CREDIT")
        charge_on(
            db, card, days, amount="500.00", name="RETURNED PAYMENT", category=card_payments(db)
        )
        rewards = get_category(db, "Credits & Rewards")
        charge_on(db, card, days, amount="3.00", name="REWARD ADJUSTMENT", category=rewards)
        assert rec.detect(db).is_empty

    def test_refunds_and_suspected_duplicates_leave_the_series_alone(self, db, today, card):
        days = monthly_dates(D(2026, 5, 5), 6)
        charge_on(db, card, days, amount="15.49", merchant_name="Netflix")
        make_transaction(db, card, amount="-15.49", date=D(2026, 8, 6), merchant_name="Netflix")
        charge_on(
            db,
            card,
            days[:3],
            amount="15.49",
            merchant_name="Netflix",
            source="csv",
            duplicate_status="suspect",
        )
        s = rec.detect(db).active[0]
        assert s.count == 6 and s.typical_amount == money("15.49")

    def test_monthly_charges_older_than_400_days_are_not_looked_at(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2024, 11, 3), 10), amount="9.00", name="OLD MONTHLY")
        overview = rec.detect(db)
        assert overview.since == TODAY - dt.timedelta(days=400)
        assert overview.yearly_since == TODAY - dt.timedelta(days=3 * 365)
        assert overview.is_empty

    def test_a_split_charge_counts_its_expense_parts(self, db, today, card):
        fitness = get_category(db, "Health & Fitness")
        home = get_category(db, "Home")
        rewards = get_category(db, "Credits & Rewards")
        for day in monthly_dates(D(2026, 6, 2), 5):
            txn = make_transaction(db, card, amount="100.00", date=day, name="CLUB MEMBERSHIP")
            make_split(db, txn, fitness, "60.00")
            make_split(db, txn, home, "30.00")
            make_split(db, txn, rewards, "10.00")
        s = rec.detect(db).active[0]
        assert s.typical_amount == money("90.00")
        assert s.category.name == "Health & Fitness"


class TestActivity:
    def test_lapsed_after_one_and_a_half_intervals(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 15), 9), amount="7.99", name="HULU")
        charge_on(db, card, monthly_dates(D(2025, 12, 1), 6), amount="30.00", name="CLASSPASS")
        overview = rec.detect(db)
        assert overview.active == ()
        assert [s.merchant for s in overview.lapsed] == ["HULU", "CLASSPASS"]  # latest first
        assert overview.monthly_total == money("0.00")
        assert overview.next_up is None

    def test_late_while_still_active(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 12, 20), 10), amount="80.00", name="COMCAST")
        s = rec.detect(db).active[0]
        assert s.next_expected == D(2026, 10, 20)
        assert s.active and s.late

    def test_active_sorted_by_next_expected_and_totals(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 25), 12), amount="20.00", name="LATER CO")
        charge_on(db, card, monthly_dates(D(2025, 11, 2), 12), amount="10.00", name="SOONER CO")
        charge_on(db, card, every(D(2026, 8, 26), 7, 10), amount="7.00", name="WEEKLY CO")
        overview = rec.detect(db)
        assert [(s.merchant, s.next_expected) for s in overview.active] == [
            ("SOONER CO", D(2026, 11, 2)),
            ("WEEKLY CO", D(2026, 11, 4)),
            ("LATER CO", D(2026, 11, 25)),
        ]
        assert overview.monthly_total == money("60.44")  # 10 + 20 + 7 × 30.44 / 7
        assert overview.next_up.merchant == "SOONER CO"

    def test_empty(self, db, today):
        overview = rec.detect(db)
        assert overview.is_empty and overview.monthly_total == money("0.00")
        assert overview.today == TODAY


# --------------------------------------------------------------------------- overrides


class TestOverrides:
    def test_ignored_moves_a_series_out_of_the_lists_and_the_total(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 5), 12), merchant_name="Netflix")
        charge_on(db, card, monthly_dates(D(2025, 11, 9), 12), amount="11.99", name="SPOTIFY")
        make_recurring_override(db, "netflix", "ignored")
        overview = rec.detect(db)
        assert [s.merchant for s in overview.active] == ["SPOTIFY"]
        assert [s.merchant for s in overview.ignored] == ["Netflix"]
        assert overview.ignored[0].is_ignored
        assert overview.monthly_total == money("11.99")

    def test_confirmed_keeps_a_merchant_detection_rejects(self, db, today, card):
        charge_on(db, card, [D(2026, 9, 3), D(2026, 10, 3)], amount="5.00", name="PATREON")
        make_recurring_override(db, "patreon", "confirmed")
        s = rec.detect(db).active[0]
        assert (s.merchant, s.is_confirmed, s.detected) == ("PATREON", True, False)
        assert (s.cadence, s.monthly_cost, s.next_expected) == (
            "monthly",
            money("5.00"),
            D(2026, 11, 3),
        )

    def test_a_confirmed_single_charge_has_no_cadence(self, db, today, card):
        charge_on(db, card, [D(2026, 10, 1)], amount="5.00", name="ONE OFF")
        make_recurring_override(db, "one off", "confirmed")
        s = rec.detect(db).active[0]
        assert (s.cadence, s.cadence_label, s.next_expected, s.monthly_cost) == (
            None,
            "Irregular",
            None,
            None,
        )
        assert rec.detect(db).monthly_total == money("0.00")

    def test_confirmed_on_a_detected_series_changes_nothing_but_the_status(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 5), 12), merchant_name="Netflix")
        make_recurring_override(db, "netflix", "confirmed")
        s = rec.detect(db).active[0]
        assert s.is_confirmed and s.detected and s.cadence == "monthly"

    def test_set_override_creates_updates_and_resets(self, db, today):
        row = rec.set_override(db, " netflix ", rec.IGNORED)
        assert (row.merchant_key, row.status) == ("netflix", "ignored")
        again = rec.set_override(db, "netflix", rec.CONFIRMED)
        assert again.id == row.id and again.status == "confirmed"
        assert rec.set_override(db, "netflix", None) is None
        assert db.scalars(select(RecurringOverride)).all() == []
        assert rec.set_override(db, "never-set", None) is None

    @pytest.mark.parametrize(
        ("key", "status"),
        [("", rec.IGNORED), ("   ", rec.IGNORED), ("x" * 301, rec.IGNORED), ("ok", "hidden")],
    )
    def test_set_override_refuses_bad_input(self, db, key, status):
        with pytest.raises(ValueError):
            rec.set_override(db, key, status)


# --------------------------------------------------------------------------- review fixes


def everything(overview: rec.RecurringOverview) -> list[rec.RecurringSeries]:
    return [*overview.active, *overview.lapsed, *overview.ignored]


def amounts_on(db, card, days, amounts, **kwargs):
    for day, amount in zip(days, amounts, strict=True):
        make_transaction(db, card, amount=amount, date=day, **kwargs)


class TestYearly:
    """Review finding: a yearly charge was listed only for ~35 days after each renewal."""

    def test_listed_before_its_renewal(self, db, today, card):
        merchant = {"merchant_name": "One Medical", "merchant_entity_id": "ent-onemed"}
        charge_on(db, card, [D(2024, 12, 2), D(2025, 12, 2)], amount="199.00", **merchant)
        s = rec.detect(db).active[0]
        assert (s.merchant, s.cadence, s.next_expected) == ("One Medical", "annual", D(2026, 12, 2))
        assert s.monthly_cost == money("16.58")  # 199 × 30.44 / 365.28
        assert not s.late

    def test_listed_the_rest_of_the_year(self, db, today, card):
        merchant = {"merchant_name": "MasterClass", "merchant_entity_id": "ent-mc"}
        charge_on(db, card, [D(2025, 9, 20), D(2026, 9, 20)], amount="120.00", **merchant)
        assert [s.merchant for s in rec.detect(db).active] == ["MasterClass"]

    def test_the_latest_renewal_is_the_price(self, db, today, card):
        days = [D(2025, 7, 9), D(2026, 7, 9)]
        amounts_on(db, card, days, ["174.00", "186.00"], merchant_name="Lemonade")
        s = rec.detect(db).active[0]
        assert (s.typical_amount, s.previous_amount, s.monthly_cost) == (
            money("186.00"),
            money("174.00"),
            money("15.50"),
        )

    def test_late_then_lapsed_once_45_days_overdue(self, db, freeze_today, card):
        charge_on(db, card, [D(2024, 9, 1), D(2025, 9, 1)], amount="99.00", name="YEARLY CO")
        freeze_today(D(2026, 10, 16))  # due Sep 1, 2026
        s = rec.detect(db).active[0]
        assert s.late and s.next_expected == D(2026, 9, 1)
        freeze_today(D(2026, 10, 17))
        overview = rec.detect(db)
        assert overview.active == () and [s.merchant for s in overview.lapsed] == ["YEARLY CO"]

    def test_two_renewals_far_apart_in_amount_are_not_yearly(self, db, today, card):
        amounts_on(db, card, [D(2025, 6, 1), D(2026, 6, 1)], ["150.00", "190.00"], name="VET")
        assert rec.detect(db).is_empty

    def test_a_renewal_from_the_csv_era_joins_the_plaid_one(self, db, today, card):
        # The CSV row has no merchant id or name; both carry the statement descriptor.
        descriptor = "CA DMV REGISTRATION SACRAMENTO CA"
        make_transaction(
            db,
            card,
            amount="198.00",
            date=D(2025, 4, 17),
            source="csv",
            name="CA DMV REGISTRATION       SACRAMENTO CA",
            original_description=descriptor,
        )
        make_transaction(
            db,
            card,
            amount="212.00",
            date=D(2026, 4, 17),
            name="CA DMV REGISTRATION",
            merchant_name="California DMV",
            merchant_entity_id="ent-dmv",
            original_description=descriptor,
        )
        s = rec.detect(db).active[0]
        assert (s.merchant, s.cadence, s.count, s.typical_amount) == (
            "California DMV",
            "annual",
            2,
            money("212.00"),
        )
        assert s.keys == (
            "ent-dmv",
            rec.merchant_key(name="CA DMV REGISTRATION       SACRAMENTO CA"),
        )
        # The Transactions search the page links to finds both eras' charges.
        assert s.search_text == descriptor
        listed = txns.search(db, txns.TransactionFilter.from_params({"q": s.search_text}))
        assert listed.total_count == 2

    def test_a_place_visited_in_both_eras_is_not_yearly(self, db, today, card):
        descriptor = "SFMOMA SAN FRANCISCO CA"
        make_transaction(
            db,
            card,
            amount="33.24",
            date=D(2025, 1, 2),
            source="csv",
            name="SFMOMA                    SAN FRANCISCO CA",
            original_description=descriptor,
        )
        for day, amount in ((D(2025, 5, 28), "36.34"), (D(2026, 5, 19), "33.28")):
            make_transaction(
                db,
                card,
                amount=amount,
                date=day,
                name="SFMOMA",
                merchant_name="SFMOMA",
                merchant_entity_id="ent-sfmoma",
                original_description=descriptor,
            )
        assert rec.detect(db).is_empty

    def test_daily_coffee_is_not_a_yearly_charge(self, db, today, card):
        # Two of ~280 purchases a year apart happen to cost exactly the same.
        coffee = {"merchant_name": "Blue Bottle Coffee", "merchant_entity_id": "ent-bb"}
        rng = random.Random(7)
        day = D(2025, 10, 1)
        while day <= TODAY:
            cents = rng.randint(590, 1210)
            if rng.random() < 0.7 and cents != 674:
                make_transaction(db, card, amount=Decimal(cents) / 100, date=day, **coffee)
            day += dt.timedelta(days=1)
        charge_on(db, card, [D(2025, 10, 30), D(2026, 10, 20)], amount="6.74", **coffee)
        assert rec.detect(db).is_empty


class TestAcrossKeys:
    """Review finding: CSV-era and Plaid-era charges of one subscription had different keys."""

    @pytest.fixture
    def netflix(self, db, freeze_today, card):
        freeze_today(D(2026, 5, 20))
        streaming = get_category(db, "Subscriptions & Streaming")
        charge_on(
            db,
            card,
            monthly_dates(D(2025, 6, 12), 6),  # CSV era: Jun-Nov 2025
            amount="17.99",
            source="csv",
            name="NETFLIX.COM               LOS GATOS CA",
            original_description="NETFLIX.COM LOS GATOS CA",
            category=streaming,
        )
        charge_on(
            db,
            card,
            monthly_dates(D(2025, 12, 12), 6),  # Plaid era: Dec 2025 - May 2026
            amount="17.99",
            name="NETFLIX.COM",
            merchant_name="Netflix",
            merchant_entity_id="ent-netflix",
            original_description="NETFLIX.COM LOS GATOS CA",
            category=streaming,
        )
        return rec.merchant_key(name="NETFLIX.COM               LOS GATOS CA")

    def test_one_subscription_listed_once(self, db, card, netflix):
        overview = rec.detect(db)
        assert [(s.merchant, s.count) for s in everything(overview)] == [("Netflix", 12)]
        s = overview.active[0]
        assert (s.key, s.keys, s.first_date) == (
            "ent-netflix",
            ("ent-netflix", netflix),
            D(2025, 6, 12),
        )
        assert overview.monthly_total == money("17.99")

    def test_a_choice_made_before_linking_still_applies(self, db, card, netflix):
        make_recurring_override(db, netflix, "ignored")
        overview = rec.detect(db)
        assert overview.active == () and overview.monthly_total == money("0.00")
        assert [s.merchant for s in overview.ignored] == ["Netflix"]
        assert overview.orphans == ()

    def test_ignore_and_restore_write_every_key(self, db, card, netflix):
        s = rec.detect(db).active[0]
        rec.set_series_override(db, s.keys, rec.IGNORED, also=s.also)
        rows = {r.merchant_key: r.status for r in db.scalars(select(RecurringOverride))}
        assert rows == {"ent-netflix": "ignored", netflix: "ignored"}
        rec.set_series_override(db, s.keys, None, also=s.also)
        assert db.scalars(select(RecurringOverride)).all() == []

    def test_a_seasonal_bill_joins_across_the_cutover(self, db, freeze_today, card):
        freeze_today(D(2025, 8, 20))
        descriptor = "PG&E WEB ONLINE 800-743-5000 CA"
        amounts_on(  # winter in the CSV era
            db,
            card,
            monthly_dates(D(2024, 11, 14), 6),
            ["65.64", "71.20", "69.90", "66.79", "62.27", "60.15"],
            source="csv",
            name="PG&E WEB ONLINE           800-743-5000 CA",
            original_description=descriptor,
        )
        amounts_on(  # summer from Plaid: 25% lower
            db,
            card,
            monthly_dates(D(2025, 5, 14), 3),
            ["48.05", "44.38", "45.23"],
            name="PG&E WEB ONLINE",
            merchant_name="PG&E",
            merchant_entity_id="ent-pge",
            original_description=descriptor,
        )
        overview = rec.detect(db)
        assert [(s.merchant, s.count) for s in everything(overview)] == [("PG&E", 9)]

    def test_unrelated_merchants_are_not_joined(self, db, freeze_today, card):
        freeze_today(D(2026, 5, 20))
        charge_on(db, card, monthly_dates(D(2025, 6, 3), 6), amount="7.99", name="HULU 877")
        charge_on(db, card, monthly_dates(D(2025, 12, 3), 6), amount="7.99", name="DISNEY PLUS")
        overview = rec.detect(db)
        assert [s.merchant for s in overview.active] == ["DISNEY PLUS"]
        assert [s.merchant for s in overview.lapsed] == ["HULU 877"]


class TestNotASubscription:
    """Review finding: frequent purchases were listed as subscriptions."""

    def test_grocery_runs_with_one_odd_amount(self, db, today, card):
        groceries = get_category(db, "Groceries")
        days = [D(2025, 11, 15), D(2025, 12, 16), D(2026, 1, 12), D(2026, 2, 10)]
        days += [D(2026, 3, 13), D(2026, 4, 10)]
        amounts = ["238.11", "198.63", "174.22", "149.95", "171.00", "179.11"]
        amounts_on(db, card, days, amounts, name="COSTCO WHSE #0144", category=groceries)
        assert rec.detect(db).is_empty

    def test_three_purchases_with_a_first_odd_one(self, db, today, card):
        days = [D(2026, 3, 15), D(2026, 6, 7), D(2026, 8, 29)]
        amounts_on(db, card, days, ["40.01", "52.75", "51.06"], merchant_name="Sephora")
        assert rec.detect(db).is_empty

    def test_filling_the_tank_about_monthly(self, db, today, card):
        # Every ~33 days, drifting through the month, amounts within 20% of each other.
        days = every(D(2026, 3, 1), 33, 8)
        amounts = ["54.56", "48.26", "62.88", "51.10", "58.40", "49.95", "60.12", "55.30"]
        amounts_on(db, card, days, amounts, merchant_name="Chevron")
        assert rec.detect(db).is_empty
        # The same amounts on a fixed day of the month are a bill.
        amounts_on(db, card, monthly_dates(D(2026, 3, 18), 8), amounts, merchant_name="Telco")
        assert [s.merchant for s in rec.detect(db).active] == ["Telco"]

    def test_three_equal_purchases_a_month_apart_among_others(self, db, today, card):
        bakery = {"merchant_name": "Arizmendi Bakery"}
        charge_on(db, card, [D(2026, 7, 15), D(2026, 8, 14), D(2026, 9, 13)], "5.69", **bakery)
        other = [D(2026, 7, 2), D(2026, 7, 21), D(2026, 8, 5), D(2026, 9, 1), D(2026, 9, 25)]
        amounts_on(db, card, other, ["4.25", "12.40", "8.75", "3.50", "15.10"], **bakery)
        assert rec.detect(db).is_empty

    def test_a_price_change_that_just_happened_is_still_one(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 9), 11), amount="10.99", name="YOUTUBE")
        charge_on(db, card, [D(2026, 10, 9)], amount="15.99", name="YOUTUBE")
        s = rec.detect(db).active[0]
        assert (s.typical_amount, s.previous_amount, s.count) == (
            money("15.99"),
            money("10.99"),
            12,
        )


class TestVariableBills:
    """Review finding: a seasonal utility bill dropped out for half the year."""

    def test_a_seasonal_utility_bill_is_monthly(self, db, freeze_today, card):
        freeze_today(D(2026, 5, 20))
        utilities = get_category(db, "Utilities & Bills")
        amounts = ["54.34", "48.05", "44.38", "45.23", "49.89", "61.29", "62.71", "71.42"]
        amounts += ["66.79", "68.83", "66.09", "54.51", "50.53"]
        amounts_on(
            db,
            card,
            monthly_dates(D(2025, 5, 14), 13),
            amounts,
            merchant_name="PG&E",
            category=utilities,
        )
        s = rec.detect(db).active[0]
        assert (s.merchant, s.cadence, s.variable, s.previous_amount) == (
            "PG&E",
            "monthly",
            True,
            None,
        )
        assert s.typical_amount == money("54.51")  # median of the last three
        assert s.next_expected == D(2026, 6, 14)

    def test_a_varying_amount_on_no_fixed_day_is_not_a_bill(self, db, today, card):
        days = [D(2026, 1, 3), D(2026, 2, 6), D(2026, 3, 4), D(2026, 4, 9), D(2026, 5, 6)]
        days += [D(2026, 6, 10), D(2026, 7, 8), D(2026, 8, 12)]
        amounts = ["44.38", "71.42", "48.05", "68.83", "45.23", "66.09", "49.89", "61.29"]
        amounts_on(db, card, days, amounts, name="BILL CO")
        assert rec.detect(db).is_empty


class TestNextDate:
    """Review finding: wrong next date / Late badge in common edge cases."""

    def test_a_charge_that_slipped_into_the_next_month(self, db, freeze_today, card):
        freeze_today(D(2026, 10, 15))
        days = [D(2026, 4, 30), D(2026, 5, 30), D(2026, 6, 30), D(2026, 7, 30), D(2026, 8, 30)]
        charge_on(db, card, [*days, D(2026, 10, 1)], amount="9.99", merchant_name="Hulu")
        assert rec.detect(db).active[0].next_expected == D(2026, 10, 30)

    def test_after_the_billing_day_moved(self, db, freeze_today, card):
        freeze_today(D(2026, 10, 10))
        days = [*monthly_dates(D(2026, 4, 20), 5), D(2026, 10, 5)]
        charge_on(db, card, days, amount="15.00", merchant_name="Gym")
        assert rec.detect(db).active[0].next_expected == D(2026, 11, 5)

    def test_a_pending_renewal_is_not_late(self, db, freeze_today, card):
        freeze_today(D(2026, 10, 7))
        netflix = {"merchant_name": "Netflix", "name": "NETFLIX.COM"}
        charge_on(db, card, monthly_dates(D(2025, 11, 5), 11), amount="15.49", **netflix)
        assert rec.detect(db).active[0].late  # the Oct 5 charge hasn't shown up at all
        make_transaction(db, card, amount="15.49", date=D(2026, 10, 5), pending=True, **netflix)
        s = rec.detect(db).active[0]
        assert (s.late, s.next_expected, s.pending_date) == (False, D(2026, 11, 5), D(2026, 10, 5))
        assert (s.count, s.last_date) == (11, D(2026, 9, 5))  # detection counts posted ones

    def test_a_pending_charge_of_another_amount_is_not_the_renewal(self, db, freeze_today, card):
        freeze_today(D(2026, 10, 7))
        netflix = {"merchant_name": "Netflix", "name": "NETFLIX.COM"}
        charge_on(db, card, monthly_dates(D(2025, 11, 5), 11), amount="15.49", **netflix)
        make_transaction(db, card, amount="3.99", date=D(2026, 10, 6), pending=True, **netflix)
        s = rec.detect(db).active[0]
        assert s.late and s.pending_date is None


class TestOneMerchantSeveralSeries:
    """Review findings: overrides hid all of a merchant's series; two subscriptions half a
    month apart passed as one every-two-weeks charge; price levels were joined crosswise."""

    apple = {"merchant_entity_id": "ent-apple", "merchant_name": "Apple"}

    def test_ignoring_one_keeps_the_other(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 12), amount="0.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **self.apple)
        cheap = next(s for s in rec.detect(db).active if s.typical_amount == money("0.99"))
        rec.set_series_override(db, cheap.keys, rec.IGNORED, also=cheap.also)
        overview = rec.detect(db)
        assert [s.typical_amount for s in overview.active] == [money("10.99")]
        assert [s.typical_amount for s in overview.ignored] == [money("0.99")]
        assert overview.monthly_total == money("10.99")

    def test_a_choice_for_the_whole_merchant_still_covers_every_series(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 12), amount="0.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **self.apple)
        make_recurring_override(db, "ent-apple", "ignored")
        overview = rec.detect(db)
        assert overview.active == () and len(overview.ignored) == 2
        first = overview.ignored[0]
        rec.set_series_override(db, first.keys, None, also=first.also)  # Restore clears it
        assert len(rec.detect(db).active) == 2

    def test_a_series_choice_survives_a_price_change(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 6), amount="0.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 12), amount="10.99", **self.apple)
        make_recurring_override(db, "ent-apple|0.99", "ignored")  # ignored at the old price
        charge_on(db, card, monthly_dates(D(2026, 5, 10), 6), amount="2.99", **self.apple)
        overview = rec.detect(db)
        assert [(s.typical_amount, s.previous_amount) for s in overview.ignored] == [
            (money("2.99"), money("0.99"))
        ]
        assert overview.ignored[0].keys == ("ent-apple|2.99",)

    def test_two_monthly_subscriptions_half_a_month_apart(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 12), amount="10.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 25), 12), amount="9.99", **self.apple)
        overview = rec.detect(db)
        got = sorted((s.cadence, s.typical_amount, s.monthly_cost) for s in overview.active)
        assert got == [
            ("monthly", money("9.99"), money("9.99")),
            ("monthly", money("10.99"), money("10.99")),
        ]
        assert overview.monthly_total == money("20.98")

    def test_price_levels_of_two_subscriptions_are_not_crossed(self, db, today, card):
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 6), amount="0.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2026, 5, 10), 6), amount="2.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2025, 11, 18), 5), amount="9.99", **self.apple)
        charge_on(db, card, monthly_dates(D(2026, 4, 18), 7), amount="10.99", **self.apple)
        got = sorted((s.typical_amount, s.previous_amount) for s in rec.detect(db).active)
        assert got == [(money("2.99"), money("0.99")), (money("10.99"), money("9.99"))]

    def test_a_weekly_service_stays_weekly(self, db, today, card):
        charge_on(
            db, card, every(D(2026, 8, 26), 7, 10), amount="59.99", merchant_name="HelloFresh"
        )
        assert [s.cadence for s in rec.detect(db).active] == ["weekly"]


class TestOrphanChoices:
    """Review finding: a choice whose merchant has no series could not be seen or undone."""

    def test_listed_with_the_merchants_name(self, db, today, card):
        clipper = {"merchant_name": "Clipper", "merchant_entity_id": "ent-clipper"}
        days = [D(2026, 6, 1), D(2026, 6, 24), D(2026, 7, 13), D(2026, 8, 5), D(2026, 8, 27)]
        charge_on(db, card, days, amount="40.00", **clipper)
        make_recurring_override(db, "ent-clipper", "ignored")
        make_recurring_override(db, "ent-gone|9.99", "confirmed")
        make_transaction(
            db,
            card,
            amount="9.99",
            date=D(2022, 1, 5),
            merchant_name="Gone Co",
            merchant_entity_id="ent-gone",
        )
        overview = rec.detect(db)
        assert not overview.is_empty
        assert [(o.name, o.status, o.amount) for o in overview.orphans] == [
            ("Clipper", "ignored", None),
            ("Gone Co", "confirmed", money("9.99")),
        ]
        rec.set_series_override(db, ["ent-clipper"], None)
        assert [o.key for o in rec.detect(db).orphans] == ["ent-gone|9.99"]

    def test_unknown_merchants_show_their_key(self, db, today):
        make_recurring_override(db, "old shop", "ignored")
        assert [(o.key, o.name) for o in rec.detect(db).orphans] == [("old shop", "old shop")]

    @pytest.mark.parametrize(
        ("keys", "also", "status"),
        [([], [], rec.IGNORED), (["ok"], [], "hidden"), (["x" * 301], [], None)],
    )
    def test_series_choice_refuses_bad_input(self, db, keys, also, status):
        with pytest.raises(ValueError):
            rec.set_series_override(db, keys, status, also=also)
        assert db.scalars(select(RecurringOverride)).all() == []

    def test_too_many_keys_are_refused(self, db):
        with pytest.raises(ValueError):
            rec.set_series_override(db, [f"k{i}" for i in range(30)], rec.IGNORED)


class TestScale:
    def test_ten_years_of_data_stay_fast(self, db, today, card):
        rng_state = 1
        cat = get_category(db, "Shopping").id
        rows, n = [], 0
        day = TODAY - dt.timedelta(days=3650)
        while day <= TODAY:
            for _ in range(15):
                n += 1
                rng_state = (rng_state * 1103515245 + 12345) % 2**31
                shop = rng_state % 400
                rows.append(
                    {
                        "account_id": card.id,
                        "source": "plaid",
                        "pending": False,
                        "date": day,
                        "posted_date": day,
                        "name": f"SHOP {shop}",
                        "merchant_name": f"Shop {shop}",
                        "merchant_entity_id": f"ent-{shop}",
                        "amount": Decimal(300 + rng_state % 8700) / 100,
                        "iso_currency_code": "USD",
                        "category_id": cat,
                        "category_source": "default",
                        "plaid_transaction_id": f"perf-{n}",
                    }
                )
            if day.day == 5:
                for bill in range(12):
                    n += 1
                    rows.append(
                        {
                            "account_id": card.id,
                            "source": "plaid",
                            "pending": False,
                            "date": day,
                            "posted_date": day,
                            "name": f"BILL {bill}",
                            "merchant_name": f"Bill {bill}",
                            "merchant_entity_id": f"bill-{bill}",
                            "amount": Decimal("9.99") + bill,
                            "iso_currency_code": "USD",
                            "category_id": cat,
                            "category_source": "default",
                            "plaid_transaction_id": f"perf-{n}",
                        }
                    )
            day += dt.timedelta(days=1)
        for i in range(0, len(rows), 5000):
            db.execute(insert(Transaction), rows[i : i + 5000])
        db.flush()
        started = time.perf_counter()
        overview = rec.detect(db)
        assert time.perf_counter() - started < 5.0  # ~0.1 s; generous for a busy machine
        assert {s.merchant for s in overview.active} >= {f"Bill {b}" for b in range(12)}
