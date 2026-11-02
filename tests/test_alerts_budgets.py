"""ALR-1 budget alerts: ``alerts.evaluate_budgets`` (ARCHITECTURE §11, §6 "Budget math").

Spending comes from the Budgets page's own numbers (``services.budgets`` over
``spending.period_spending``). Each test commits the session (a savepoint release in tests), which
is when notifications go out, and reads ``notifier.sent``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from budgetapp import cli, periods
from budgetapp.commands import recategorize as recategorize_command
from budgetapp.models import AlertEvent, Statement
from budgetapp.services import alerts, budgets, csv_import
from tests.factories import (
    get_category,
    make_account,
    make_alert_event,
    make_budget,
    make_rule,
    make_statement,
    make_transaction,
    set_app_settings,
)

TODAY = dt.date(2026, 11, 12)
NOV = "2026-11-01"


@pytest.fixture(autouse=True)
def today(freeze_today):
    return freeze_today(TODAY)


@pytest.fixture
def card(db):
    return make_account(db)


@pytest.fixture
def dining(db):
    return get_category(db, "Dining & Drinks")


@pytest.fixture
def travel(db):
    return get_category(db, "Travel")


def spend(db, card, category, amount, *, day=TODAY, **kwargs):
    return make_transaction(db, card, amount=amount, date=day, category=category, **kwargs)


def evaluate(db, notifier, **kwargs):
    results = alerts.evaluate_budgets(db, notifier, **kwargs)
    db.commit()
    return results


def keys(db) -> list[str]:
    db.expire_all()
    return sorted(db.scalars(select(AlertEvent.dedupe_key).where(AlertEvent.kind == "budget")))


def event(db, key: str) -> AlertEvent:
    db.expire_all()
    return db.scalars(select(AlertEvent).where(AlertEvent.dedupe_key == key)).one()


# --------------------------------------------------------------------------- crossing


def test_nothing_below_the_first_threshold(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "319.99")
    assert evaluate(db, notifier) == []
    assert keys(db) == [] and notifier.sent == []


def test_80_percent_notifies_once_after_commit(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "320.00")  # exactly 80 %: "reaches" counts
    results = alerts.evaluate_budgets(db, notifier)
    assert [r.dedupe_key for r in results] == [f"budget:{dining.id}:{NOV}:80"]
    assert results[0].queued and notifier.sent == []  # sent when the caller commits
    db.commit()
    (sent,) = notifier.sent
    assert sent.title == "Dining & Drinks: 80% of budget used"
    assert sent.message == "$320.00 of $400.00 spent in November, $80.00 left, 19 days to go."
    stored = event(db, f"budget:{dining.id}:{NOV}:80")
    assert stored.category_id == dining.id and stored.delivered and stored.kind == "budget"
    assert evaluate(db, notifier) == [] and len(notifier.sent) == 1


def test_100_percent_notifies_again_and_says_over_budget(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "350.00")
    evaluate(db, notifier)
    spend(db, card, dining, "60.00")
    evaluate(db, notifier)
    assert notifier.titles == [
        "Dining & Drinks: 80% of budget used",
        "Dining & Drinks: over budget",
    ]
    assert notifier.sent[1].message == (
        "$410.00 of $400.00 spent in November, $10.00 over, 19 days to go."
    )
    assert keys(db) == [f"budget:{dining.id}:{NOV}:100", f"budget:{dining.id}:{NOV}:80"]


def test_exactly_the_budget_is_used_up(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "400.00")
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: budget used up"]
    assert notifier.sent[0].message == "$400.00 of $400.00 spent in November, 19 days to go."


def test_crossing_several_thresholds_at_once_sends_only_the_highest(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "450.00")
    results = evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: over budget"]
    assert [(r.dedupe_key, r.inserted, r.queued) for r in results] == [
        (f"budget:{dining.id}:{NOV}:80", True, False),
        (f"budget:{dining.id}:{NOV}:100", True, True),
    ]
    assert evaluate(db, notifier) == [] and len(notifier.sent) == 1


def test_refunds_count_against_spending(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "350.00")
    spend(db, card, dining, "-40.00")  # a refund in an expense category subtracts (TXN-3)
    assert evaluate(db, notifier) == []


def test_other_categories_and_kinds_never_count(db, notifier, card, dining, travel):
    make_budget(db, dining, "100.00")
    spend(db, card, travel, "500.00")
    spend(db, card, get_category(db, "Card Payments"), "-900.00")
    assert evaluate(db, notifier) == []


# --------------------------------------------------------------------------- re-arming


def test_falling_back_below_a_threshold_clears_it_so_it_fires_again(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "250.00")
    hold = spend(db, card, dining, "100.00", pending=True)  # a hotel hold, counted (default)
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:{NOV}:80"]
    db.delete(hold)  # the hold dropped
    db.flush()
    assert evaluate(db, notifier) == []
    assert keys(db) == []
    spend(db, card, dining, "100.00")  # the real charge posts
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"] * 2


def test_dropping_below_100_keeps_80_recorded(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    spend(db, card, dining, "350.00")
    extra = spend(db, card, dining, "100.00", pending=True)
    evaluate(db, notifier)
    db.delete(extra)
    db.flush()
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:{NOV}:80"]
    spend(db, card, dining, "100.00")
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: over budget", "Dining & Drinks: over budget"]


def test_a_bigger_budget_re_arms_and_a_smaller_one_fires(db, notifier, card, dining):
    budget = make_budget(db, dining, "400.00")
    spend(db, card, dining, "330.00")
    evaluate(db, notifier)
    budget.amount = Decimal("500.00")
    db.flush()
    evaluate(db, notifier)
    assert keys(db) == []
    budget.amount = Decimal("300.00")
    db.flush()
    evaluate(db, notifier)
    assert notifier.titles == [
        "Dining & Drinks: 80% of budget used",
        "Dining & Drinks: over budget",
    ]


def test_a_new_period_fires_again(db, notifier, card, dining, freeze_today):
    make_budget(db, dining, "100.00")
    spend(db, card, dining, "85.00", day=dt.date(2026, 10, 20))
    freeze_today(dt.date(2026, 10, 25))
    evaluate(db, notifier)
    freeze_today(TODAY)
    spend(db, card, dining, "90.00")
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:2026-10-01:80", f"budget:{dining.id}:{NOV}:80"]
    assert len(notifier.sent) == 2 and "in November" in notifier.sent[1].message


def test_a_lower_threshold_added_later_is_recorded_silently(db, notifier, card, dining):
    make_budget(db, dining, "400.00")
    set_app_settings(db, alert_thresholds=[100])
    spend(db, card, dining, "420.00")
    evaluate(db, notifier)
    set_app_settings(db, alert_thresholds=[50, 100])
    results = evaluate(db, notifier)
    assert [(r.dedupe_key, r.queued) for r in results] == [(f"budget:{dining.id}:{NOV}:50", False)]
    assert notifier.titles == ["Dining & Drinks: over budget"]


# --------------------------------------------------------------------------- settings


def test_turned_off_means_nothing_and_nothing_is_cleared(db, notifier, card, dining):
    make_budget(db, dining, "100.00")
    make_alert_event(db, kind="budget", dedupe_key=f"budget:{dining.id}:{NOV}:80")
    set_app_settings(db, budget_alerts_enabled=False)
    spend(db, card, dining, "500.00")
    assert evaluate(db, notifier) == []
    assert keys(db) == [f"budget:{dining.id}:{NOV}:80"] and notifier.sent == []


def test_custom_thresholds(db, notifier, card, dining):
    make_budget(db, dining, "200.00")
    set_app_settings(db, alert_thresholds=[50, 90, 150])
    spend(db, card, dining, "100.00")
    evaluate(db, notifier)
    spend(db, card, dining, "200.00")  # 150 %
    evaluate(db, notifier)
    assert notifier.titles == [
        "Dining & Drinks: 50% of budget used",
        "Dining & Drinks: 150% of budget used",
    ]
    assert notifier.sent[1].message == (
        "$300.00 of $200.00 spent in November, $100.00 over, 19 days to go."
    )
    assert keys(db) == [f"budget:{dining.id}:{NOV}:{t}" for t in (150, 50, 90)]


def test_a_threshold_list_without_100_says_over_budget_when_it_is(db, notifier, card, dining):
    make_budget(db, dining, "200.00")
    set_app_settings(db, alert_thresholds=[90])
    spend(db, card, dining, "250.00")
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: over budget"]


def test_empty_thresholds_mean_no_budget_alerts(db, notifier, card, dining):
    make_budget(db, dining, "10.00")
    set_app_settings(db, alert_thresholds=[])
    spend(db, card, dining, "50.00")
    assert evaluate(db, notifier) == []


def test_pending_charges_follow_the_budgets_setting(db, notifier, card, dining):
    make_budget(db, dining, "100.00")
    spend(db, card, dining, "90.00", pending=True)
    set_app_settings(db, include_pending_in_budgets=False)
    assert evaluate(db, notifier) == []
    set_app_settings(db, include_pending_in_budgets=True)
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]


def test_hidden_categories_are_left_out(db, notifier, card, dining):
    make_budget(db, dining, "100.00")
    dining.is_hidden = True
    db.flush()
    spend(db, card, dining, "500.00")
    assert evaluate(db, notifier) == []


# --------------------------------------------------------------------------- unusual budgets


def test_zero_available_fires_on_the_first_spending(db, notifier, card, dining):
    make_budget(db, dining, "0.00")
    assert evaluate(db, notifier) == []
    spend(db, card, dining, "-5.00")  # a refund alone is not spending
    assert evaluate(db, notifier) == []
    spend(db, card, dining, "12.00")
    evaluate(db, notifier)
    assert notifier.titles == ["Dining & Drinks: over budget"]
    assert notifier.sent[0].message == (
        "$7.00 spent in November against a budget of $0.00, 19 days to go."
    )


def test_a_negative_carry_in_fires_on_the_first_spending(db, notifier, card, dining):
    make_budget(db, dining, "100.00", rollover=True, effective_from=dt.date(2026, 10, 1))
    spend(db, card, dining, "250.00", day=dt.date(2026, 10, 10))  # October: $150 over
    spend(db, card, dining, "5.00")
    evaluate(db, notifier)
    line = budgets.overview(db, budgets.current_period(db)).line_for(dining.id)
    assert line.available == Decimal("-50.00")
    assert notifier.titles == ["Dining & Drinks: over budget"]
    assert notifier.sent[0].message == (
        "$5.00 spent in November, with nothing left after last period's overspending, "
        "19 days to go."
    )


def test_a_positive_carry_in_raises_the_bar(db, notifier, card, dining):
    make_budget(db, dining, "100.00", rollover=True, effective_from=dt.date(2026, 10, 1))
    spend(db, card, dining, "20.00", day=dt.date(2026, 10, 10))  # $80 carried in: $180 available
    spend(db, card, dining, "140.00")
    assert evaluate(db, notifier) == []
    spend(db, card, dining, "5.00")
    evaluate(db, notifier)
    assert notifier.sent[0].message == (
        "$145.00 of $180.00 spent in November, $35.00 left, 19 days to go."
    )


def test_the_overall_target(db, notifier, card, dining, travel):
    make_budget(db, None, "1000.00")
    spend(db, card, dining, "500.00")
    spend(db, card, travel, "350.00")
    evaluate(db, notifier)
    assert keys(db) == [f"budget:overall:{NOV}:80"]
    assert notifier.titles == ["Overall budget: 80% used"]
    assert notifier.sent[0].message == (
        "$850.00 of $1,000.00 spent in November, $150.00 left, 19 days to go."
    )
    spend(db, card, travel, "200.00")
    evaluate(db, notifier)
    assert notifier.titles[-1] == "Over your overall budget"
    db.expire_all()
    assert event(db, f"budget:overall:{NOV}:100").category_id is None


def test_without_a_target_the_sum_of_budgets_does_not_alert(db, notifier, card, dining, travel):
    make_budget(db, dining, "100.00")
    make_budget(db, travel, "1000.00")
    spend(db, card, dining, "50.00")
    spend(db, card, travel, "800.00")
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{travel.id}:{NOV}:80"]


def test_annual_budgets_use_the_year(db, notifier, card, dining, travel):
    make_budget(db, travel, "5000.00", period="annual")
    spend(db, card, travel, "3000.00", day=dt.date(2026, 3, 2))
    spend(db, card, travel, "1100.00", day=dt.date(2026, 11, 2))
    make_budget(db, None, "500.00")  # the overall target ignores annual categories
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{travel.id}:2026:80"]
    assert notifier.titles == ["Travel: 80% of the 2026 budget used"]
    assert notifier.sent[0].message == "$4,100.00 of $5,000.00 spent so far in 2026, $900.00 left."
    spend(db, card, travel, "1000.00")
    evaluate(db, notifier)
    assert notifier.titles[-1] == "Travel: over the 2026 budget"


def test_annual_and_monthly_keys_never_collide(db, notifier, card, dining, freeze_today):
    annual = make_budget(db, dining, "100.00", period="annual")
    freeze_today(dt.date(2026, 1, 20))
    spend(db, card, dining, "90.00", day=dt.date(2026, 1, 5))
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:2026:80"]
    annual.amount = None  # switched to a monthly budget in January (BUD: one type at a time)
    make_budget(db, dining, "100.00", effective_from=dt.date(2026, 1, 1))
    db.flush()
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:2026-01-01:80", f"budget:{dining.id}:2026:80"]
    assert notifier.titles == [
        "Dining & Drinks: 80% of the 2026 budget used",
        "Dining & Drinks: 80% of budget used",
    ]


def test_budget_key_format():
    month = periods.month_of(TODAY)
    year = periods.year_of(TODAY)
    assert alerts.budget_key(7, month, 80) == "budget:7:2026-11-01:80"
    assert alerts.budget_key(None, month, 100) == "budget:overall:2026-11-01:100"
    assert alerts.budget_key(7, year, 80) == "budget:7:2026:80"


# --------------------------------------------------------------------------- statement cycles


@pytest.fixture
def statement_mode(db, card):
    card.statement_closing_day = 27
    make_statement(db, card, "2026-10-27")
    set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=card.id)
    return card


def test_statement_cycles_key_on_the_cycle_start(db, notifier, statement_mode, dining):
    card = statement_mode
    make_budget(db, dining, "100.00", effective_from=dt.date(2026, 10, 1))
    spend(db, card, dining, "85.00", day=dt.date(2026, 11, 2))
    spend(db, card, dining, "50.00", day=dt.date(2026, 10, 20))  # the previous cycle
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:2026-10-28:80"]
    assert notifier.sent[0].message == (
        "$85.00 of $100.00 spent this cycle (it ends Nov 27), $15.00 left, 16 days to go."
    )


def test_a_moved_closing_date_does_not_fire_the_cycle_again(db, notifier, statement_mode, dining):
    card = statement_mode
    make_budget(db, dining, "100.00", effective_from=dt.date(2026, 10, 1))
    charge = spend(db, card, dining, "85.00", day=dt.date(2026, 11, 2))
    evaluate(db, notifier)
    # The statement really closed two days earlier: the cycle now starts on Oct 26.
    db.scalars(select(Statement)).one().closing_date = dt.date(2026, 10, 25)
    db.flush()
    assert budgets.current_period(db).start == dt.date(2026, 10, 26)
    assert evaluate(db, notifier) == [] and len(notifier.sent) == 1
    # Spending falls below 80 %: the event kept under the old start is cleared all the same.
    charge.amount = Decimal("10.00")
    db.flush()
    evaluate(db, notifier)
    assert keys(db) == []


def test_the_previous_cycles_events_are_not_this_cycles(db, notifier, statement_mode, dining):
    card = statement_mode
    make_budget(db, dining, "100.00", effective_from=dt.date(2026, 9, 1))
    make_alert_event(db, kind="budget", dedupe_key=f"budget:{dining.id}:2026-09-28:80")
    spend(db, card, dining, "85.00", day=dt.date(2026, 11, 2))
    evaluate(db, notifier)
    assert keys(db) == [f"budget:{dining.id}:2026-09-28:80", f"budget:{dining.id}:2026-10-28:80"]
    assert len(notifier.sent) == 1


def test_a_flagged_cycle_says_so(db, notifier, card, dining):
    card.statement_closing_day = 27
    make_statement(db, card, "2026-09-27")
    make_statement(db, card, "2026-11-27")  # October's statement is missing: a 61-day cycle
    set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=card.id)
    make_budget(db, dining, "100.00", effective_from=dt.date(2026, 9, 1))
    period = budgets.current_period(db)
    assert period.flagged and period.days == 61
    spend(db, card, dining, "90.00", day=dt.date(2026, 10, 3))
    evaluate(db, notifier)
    assert notifier.sent[0].message == (
        "$90.00 of $100.00 spent this cycle (it ends Nov 27), $10.00 left, 16 days to go. "
        "This cycle is 61 days long, so check the Statements page for a missing or wrong "
        "closing date."
    )


# --------------------------------------------------------------------------- never breaks


def test_an_error_is_logged_and_the_callers_work_still_commits(
    db, notifier, card, dining, monkeypatch, caplog
):
    make_budget(db, dining, "100.00")
    kept = spend(db, card, dining, "95.00")

    def broken(*_args, **_kwargs):
        raise RuntimeError("bug in a budget")

    monkeypatch.setattr(budgets, "overview", broken)
    assert alerts.evaluate_budgets(db, notifier) == []
    db.commit()
    db.expire_all()
    assert db.get(type(kept), kept.id) is not None
    assert "budget alerts failed" in caplog.text and notifier.sent == []


def test_a_failing_notifier_is_recorded_not_raised(db, card, dining):
    from budgetapp.services.notify import RecordingNotifier

    failing = RecordingNotifier(fail_with="osascript exited 1")
    make_budget(db, dining, "100.00")
    spend(db, card, dining, "95.00")
    evaluate(db, failing)
    stored = event(db, f"budget:{dining.id}:{NOV}:80")
    assert not stored.delivered and stored.error == "osascript exited 1"


# --------------------------------------------------------------------------- CSV imports

CSV_HEADER = (
    "Date,Description,Amount,Extended Details,Appears On Your Statement As,Address,"
    "City/State,Zip Code,Country,Reference,Category,Card Member\n"
)


def csv_file(*rows: tuple[str, str, str, str]) -> bytes:
    """An extended Amex download with restaurant rows ``(date, name, amount, reference)``."""
    body = "".join(
        f'{day},{name},{amount},"{name}",{name},,,,UNITED STATES,{reference},'
        "Restaurant-Restaurant,A B\n"
        for day, name, amount, reference in rows
    )
    return (CSV_HEADER + body).encode()


def test_a_csv_import_that_crosses_a_budget_alerts_after_commit(db, notifier, dining):
    make_budget(db, dining, "100.00")
    data = csv_file(
        ("11/03/26", "TARTINE", "60.00", "'320263070000000001'"),
        ("11/05/26", "ZUNI CAFE", "30.00", "'320263090000000002'"),
    )
    preview = csv_import.preview(db, data, "nov.csv", None)
    outcome = csv_import.commit(db, preview.token, notifier=notifier)
    assert outcome.inserted_count == 2 and notifier.sent == []
    db.commit()
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]


def test_a_dry_or_empty_import_evaluates_nothing(db, notifier, dining, monkeypatch):
    from budgetapp.services.events import ImportOutcome

    calls = []
    monkeypatch.setattr(alerts, "evaluate_budgets", lambda *a, **k: calls.append(1) or [])
    assert alerts.evaluate_after_import(db, notifier, ImportOutcome(1, "a.csv")) == []
    dry = ImportOutcome(1, "a.csv", inserted_count=3, dry_run=True)
    assert alerts.evaluate_after_import(db, notifier, dry) == []
    paired = ImportOutcome(1, "a.csv", matched_plaid_count=1)
    alerts.evaluate_after_import(db, notifier, paired)
    assert calls == [1]


# --------------------------------------------------------------------------- ./run recategorize


def test_the_recategorize_command_evaluates_budgets(db, session_factory, notifier, monkeypatch):
    """``./run recategorize`` (CAT-4) moves past charges: budget alerts go out on its commit."""
    command = recategorize_command
    card = make_account(db)
    dining = get_category(db, "Dining & Drinks")
    make_budget(db, dining, "100.00")
    make_rule(db, dining, merchant_pattern="TARTINE")
    make_transaction(db, card, amount="95.00", date=TODAY, name="TARTINE BAKERY")
    db.commit()
    lines: list[str] = []
    command.recategorize(session_factory, dry_run=True, out=lines.append, notifier=notifier)
    assert notifier.sent == [] and lines[2].strip() == "1 would change category"
    command.recategorize(session_factory, out=lines.append, notifier=notifier)
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]
    # The command line hands the environment's notifier over (a no-op one under pytest).
    monkeypatch.setattr(command, "open_database", lambda settings: (session_factory, lambda: None))
    db.execute(delete(AlertEvent))
    db.commit()
    assert cli.main(["recategorize"]) == 0
    db.expire_all()
    assert db.scalars(select(AlertEvent.dedupe_key)).all() == [f"budget:{dining.id}:{NOV}:80"]
