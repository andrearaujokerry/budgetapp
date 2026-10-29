"""Recurring charges and subscriptions (INS-3): ``services/recurring.py``."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import periods
from budgetapp.models import RecurringOverride
from budgetapp.services import recurring as rec
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
        charge_on(db, card, monthly_dates(D(2025, 11, 10), 3), amount="0.99", **apple)
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
        assert {s.key for s in overview.active} == {"apple-entity"}
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

    def test_charges_older_than_400_days_are_not_looked_at(self, db, today, card):
        charge_on(db, card, [D(2025, 9, 20), D(2026, 9, 20)], amount="99.00", name="OLD YEARLY")
        overview = rec.detect(db)
        assert overview.since == TODAY - dt.timedelta(days=400)
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
