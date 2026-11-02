"""Alerts after a sync: ALR-2 sync problems, SYNC-6 consent, ALR-3 large charges (ARCHITECTURE §11).

The first part calls ``alerts.evaluate_after_sync`` with hand-made outcomes (the Item row set up as
the sync engine leaves it); the second runs the real sync engine against ``FakePlaidGateway`` and
the ``./run sync --unattended`` command, so the wiring is covered end to end. Notifications go out
when the session commits; tests read ``notifier.sent``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.commands import sync as sync_cmd
from budgetapp.models import AlertEvent, PlaidItem
from budgetapp.services import alerts, sync
from budgetapp.services.events import SyncOutcome
from budgetapp.services.notify import RecordingNotifier
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_item,
    make_transaction,
    set_app_settings,
)

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def at_now(freeze_today):
    freeze_today(now=NOW)


def at(moment: dt.datetime) -> None:
    clock.set_override(now=moment)


def today() -> dt.date:
    return clock.today()


@pytest.fixture
def item(db):
    return make_item(db, last_sync_success_at=NOW - 2 * HOUR, linked_at=NOW - 30 * DAY)


def fail(item: PlaidItem, code: str, *, reconnect: bool = False, **kwargs: Any) -> SyncOutcome:
    """The Item and outcome as the sync engine leaves them after a failed run."""
    item.status = "needs_reconnect" if reconnect else "error"
    item.error_code = code
    return SyncOutcome(
        item.id,
        "schedule",
        "error",
        item_status=item.status,
        error_code=code,
        **kwargs,
    )


def succeed(item: PlaidItem, **kwargs: Any) -> SyncOutcome:
    item.status = "healthy"
    item.error_code = None
    item.last_sync_success_at = clock.now()
    kwargs.setdefault("status", "success")
    return SyncOutcome(item.id, "schedule", item_status="healthy", **kwargs)


def evaluate(db, notifier, outcome: SyncOutcome) -> list[alerts.AlertResult]:
    db.flush()
    results = alerts.evaluate_after_sync(db, notifier, outcome)
    db.commit()
    return results


def keys(db, kind: str | None = None) -> list[str]:
    db.expire_all()
    query = select(AlertEvent.dedupe_key).order_by(AlertEvent.id)
    if kind is not None:
        query = query.where(AlertEvent.kind == kind)
    return list(db.scalars(query))


def event(db, key: str) -> AlertEvent:
    db.expire_all()
    return db.scalars(select(AlertEvent).where(AlertEvent.dedupe_key == key)).one()


def local_date(moment: dt.datetime) -> dt.date:
    return moment.astimezone().date()


def long_date(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


# --------------------------------------------------------------------------- ALR-2 failures


def test_a_failed_sync_alerts_once_per_code_and_day(db, notifier, item):
    evaluate(db, notifier, fail(item, "INSTITUTION_NOT_RESPONDING"))
    (sent,) = notifier.sent
    assert sent.title == "American Express sync failed"
    assert sent.message == (
        "Amex isn't answering Plaid right now. The next sync will try again. "
        "Code: INSTITUTION_NOT_RESPONDING."
    )
    key = f"sync-error:{item.id}:INSTITUTION_NOT_RESPONDING:{today().isoformat()}"
    assert keys(db) == [key]
    assert event(db, key).item_id == item.id and event(db, key).kind == "sync_error"
    at(NOW + 6 * HOUR)
    evaluate(db, notifier, fail(item, "INSTITUTION_NOT_RESPONDING"))
    assert len(notifier.sent) == 1
    evaluate(db, notifier, fail(item, "INTERNAL_SERVER_ERROR"))  # another code the same day
    at(NOW + DAY)
    evaluate(db, notifier, fail(item, "INSTITUTION_NOT_RESPONDING"))  # the next day
    assert len(notifier.sent) == 3


def test_internal_failures_alert_with_plain_words(db, notifier, item):
    evaluate(db, notifier, fail(item, "INTERNAL", internal_error=True))
    assert notifier.sent[0].message == (
        "Something went wrong in the app during the sync. The details are in the sync log. "
        "Code: INTERNAL."
    )


def test_an_unknown_code_uses_plaids_display_message(db, notifier, item):
    evaluate(db, notifier, fail(item, "SOMETHING_NEW", display_message="Please try later."))
    evaluate(db, notifier, fail(item, "OTHER_NEW"))
    assert [n.message for n in notifier.sent] == [
        "Please try later. Code: SOMETHING_NEW.",
        "Plaid reported an error. Code: OTHER_NEW.",
    ]


def test_skipped_runs_and_vanished_items_raise_nothing(db, notifier, item):
    skipped = SyncOutcome(item.id, "schedule", "skipped")
    assert evaluate(db, notifier, skipped) == []
    gone = SyncOutcome(item.id + 1000, "schedule", "error", error_code="X")
    assert evaluate(db, notifier, gone) == []
    assert notifier.sent == []


# --------------------------------------------------------------------------- ALR-2 reconnect


def test_reconnect_alerts_once_per_occurrence(db, notifier, item):
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    (sent,) = notifier.sent
    assert sent.title == "Reconnect American Express"
    assert sent.message == (
        "Amex needs you to sign in again through Plaid (the consent expired or was revoked). "
        "Reconnect fixes it and keeps this connection. Open the Connection page and click "
        "Reconnect."
    )
    assert keys(db) == [f"reconnect:{item.id}:open"]
    at(NOW + DAY)  # still broken the next day: no daily nagging, and no "sync failed" either
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert len(notifier.sent) == 1
    first = event(db, f"reconnect:{item.id}:open")

    at(NOW + DAY + 2 * HOUR)  # reconnected: a sync succeeds
    evaluate(db, notifier, succeed(item))
    assert keys(db) == [f"reconnect:{item.id}:{first.id}"]
    at(NOW + 9 * DAY)
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == ["Reconnect American Express"] * 2  # the stale data is in it
    assert keys(db, "reconnect") == [f"reconnect:{item.id}:{first.id}", f"reconnect:{item.id}:open"]
    assert "The last successful sync was 7 days ago" in notifier.sent[1].message


def test_an_item_marked_for_reconnect_after_a_successful_sync_alerts(db, notifier, item):
    """``/item/get`` (a best-effort step after the ledger is updated) can find the Item broken."""
    outcome = succeed(item)
    item.status, item.error_code = "needs_reconnect", "ITEM_LOGIN_REQUIRED"
    item.error_display_message = None
    evaluate(db, notifier, outcome)
    assert notifier.titles == ["Reconnect American Express"]
    evaluate(db, notifier, outcome)
    assert len(notifier.sent) == 1


def test_a_transient_error_does_not_end_the_reconnect_occurrence(db, notifier, item):
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    evaluate(db, notifier, fail(item, "INSTITUTION_DOWN"))
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == ["Reconnect American Express", "American Express sync failed"]


def test_consent_revoked_and_locked_items_say_so(db, notifier, item):
    evaluate(db, notifier, fail(item, "ITEM_LOCKED", reconnect=True))
    assert notifier.sent[0].message.startswith("Amex didn't accept the sign-in.")


# --------------------------------------------------------------------------- ALR-2 stale


def test_stale_data_alerts_once_a_day(db, notifier):
    linked = make_item(db, last_sync_success_at=None, linked_at=NOW - 40 * HOUR)
    not_ready = SyncOutcome(linked.id, "schedule", "not_ready", item_status="healthy")
    evaluate(db, notifier, not_ready)
    (sent,) = notifier.sent
    assert sent.title == "American Express data is out of date"
    assert sent.message == (
        "No transactions have arrived since the card was linked 40 hours ago. Check the "
        "Connection page; Sync now tries again."
    )
    assert keys(db) == [f"stale:{linked.id}:{today().isoformat()}"]
    evaluate(db, notifier, not_ready)
    at(NOW + DAY)
    evaluate(db, notifier, not_ready)
    assert len(notifier.sent) == 2


def test_fresh_data_is_not_stale(db, notifier):
    linked = make_item(db, last_sync_success_at=None, linked_at=NOW - 30 * HOUR)
    evaluate(db, notifier, SyncOutcome(linked.id, "schedule", "not_ready"))
    assert notifier.sent == []


def test_a_failure_on_stale_data_sends_one_notification(db, notifier):
    old = make_item(db, last_sync_success_at=NOW - 3 * DAY)
    evaluate(db, notifier, fail(old, "INSTITUTION_DOWN"))
    (sent,) = notifier.sent
    last = (NOW - 3 * DAY).astimezone()
    when = f"{last:%b} {last.day} at {last.strftime('%I:%M %p').lstrip('0')}"
    assert sent.message == (
        "Amex isn't answering Plaid right now. The next sync will try again. Code: "
        f"INSTITUTION_DOWN. The last successful sync was 3 days ago ({when})."
    )
    stale = event(db, f"stale:{old.id}:{today().isoformat()}")
    assert not stale.delivered and stale.error is None  # recorded, folded into the failure


def test_stale_while_waiting_for_a_reconnect_reminds_daily(db, notifier):
    broken = make_item(db, last_sync_success_at=NOW - 20 * HOUR)
    evaluate(db, notifier, fail(broken, "ITEM_LOGIN_REQUIRED", reconnect=True))
    at(NOW + DAY)
    evaluate(db, notifier, fail(broken, "ITEM_LOGIN_REQUIRED", reconnect=True))
    at(NOW + 2 * DAY)
    evaluate(db, notifier, fail(broken, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == [
        "Reconnect American Express",
        "American Express data is out of date",
        "American Express data is out of date",
    ]
    assert notifier.sent[1].message.startswith("The last successful sync was 44 hours ago")
    assert notifier.sent[1].message.endswith("Reconnect on the Connection page to catch up.")


def test_sync_alerts_can_be_turned_off(db, notifier):
    set_app_settings(db, sync_alerts_enabled=False)
    old = make_item(db, last_sync_success_at=NOW - 3 * DAY, consent_expires_at=NOW + 5 * DAY)
    evaluate(db, notifier, fail(old, "INSTITUTION_DOWN"))
    evaluate(db, notifier, fail(old, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == ["Renew your American Express connection"]  # SYNC-6 stays on
    assert keys(db, "consent") and not keys(db, "sync_error") and not keys(db, "stale")


# --------------------------------------------------------------------------- SYNC-6 consent


def test_consent_reminder_once_per_due_date(db, notifier, item):
    item.consent_expires_at = NOW + 25 * DAY
    evaluate(db, notifier, succeed(item))
    due = local_date(NOW + 25 * DAY)
    (sent,) = notifier.sent
    assert sent.title == "Renew your American Express connection"
    assert sent.message == (
        f"Plaid's access to American Express ends on {long_date(due)} (25 days left). Renew it "
        "on the Connection page so syncing keeps working; it takes a minute and keeps this "
        "connection."
    )
    assert keys(db) == [f"consent:{item.id}:{due.isoformat()}"]
    at(NOW + 10 * DAY)
    evaluate(db, notifier, succeed(item))
    assert len(notifier.sent) == 1
    item.consent_expires_at = NOW + 375 * DAY  # renewed through update mode
    evaluate(db, notifier, succeed(item))
    assert len(notifier.sent) == 1
    at(NOW + 350 * DAY)
    evaluate(db, notifier, succeed(item))
    assert len(notifier.sent) == 2
    assert keys(db, "consent")[-1] == f"consent:{item.id}:{local_date(NOW + 375 * DAY)}"


def test_consent_outside_the_reminder_window(db, notifier, item):
    item.consent_expires_at = NOW + 31 * DAY
    evaluate(db, notifier, succeed(item))
    assert notifier.sent == []
    set_app_settings(db, consent_reminder_days=45)
    evaluate(db, notifier, succeed(item))
    assert notifier.titles == ["Renew your American Express connection"]


def test_consent_falls_back_to_a_year_after_the_last_consent(db, notifier):
    consented = NOW - 340 * DAY
    linked = make_item(db, last_consented_at=consented, linked_at=consented)
    evaluate(db, notifier, succeed(linked))
    due = local_date(consented + 365 * DAY)
    assert keys(db) == [f"consent:{linked.id}:{due.isoformat()}"]


@pytest.mark.parametrize(
    ("offset", "words"),
    [
        (DAY, "ends tomorrow, {due}"),
        (dt.timedelta(minutes=5), "ends today"),
        (-2 * DAY, "ended on {due}"),
    ],
)
def test_consent_wording_close_to_and_after_the_date(db, notifier, item, offset, words):
    item.consent_expires_at = NOW + offset
    evaluate(db, notifier, fail(item, "INSTITUTION_DOWN"))  # reminded after a failure too
    due = long_date(local_date(NOW + offset))
    reminder = next(n for n in notifier.sent if n.title.startswith("Renew"))
    assert f"Plaid's access to American Express {words.format(due=due)}." in reminder.message


# --------------------------------------------------------------------------- ALR-3


@pytest.fixture
def card(db, item):
    return make_account(db, item)


@pytest.fixture
def threshold(db):
    set_app_settings(db, large_charge_threshold=Decimal("500.00"))
    return Decimal("500.00")


def charge(db, card, amount, *, day: dt.date | None = None, **kwargs: Any):
    return make_transaction(db, card, amount=amount, date=day or today(), **kwargs)


def new_charges(item, *rows, **kwargs: Any) -> SyncOutcome:
    kwargs.setdefault("sync_run_id", 7)
    return succeed(item, inserted_transaction_ids=tuple(r.id for r in rows), **kwargs)


def test_a_large_new_charge_alerts_once(db, notifier, item, card, threshold):
    big = charge(db, card, "2500.00", merchant_name="Apple", name="APPLE STORE #12")
    evaluate(db, notifier, new_charges(item, big))
    (sent,) = notifier.sent
    assert sent.title == "$2,500.00 at Apple"
    assert sent.message == (
        f"A charge on {today():%b} {today().day} is over your $500.00 large-charge alert."
    )
    stored = event(db, f"large-charge:{big.plaid_transaction_id}")
    assert (stored.kind, stored.transaction_id, stored.item_id) == (
        "large_charge",
        big.id,
        item.id,
    )
    evaluate(db, notifier, new_charges(item, big))
    assert len(notifier.sent) == 1


def test_only_charges_over_the_amount(db, notifier, item, card, threshold):
    rows = [
        charge(db, card, "500.00"),  # not over it
        charge(db, card, "499.99"),
        charge(db, card, "-900.00", name="REFUND"),
        charge(db, card, "500.01", name="JUST OVER", merchant_name=None),
    ]
    evaluate(db, notifier, new_charges(item, *rows))
    assert notifier.titles == ["$500.01 at JUST OVER"]


def test_a_pending_charge_says_so(db, notifier, item, card, threshold):
    hold = charge(db, card, "800.00", pending=True, merchant_name="Hilton")
    evaluate(db, notifier, new_charges(item, hold))
    assert notifier.sent[0].message.startswith("A pending charge on")


def test_only_charges_dated_in_the_last_7_days(db, notifier, item, card, threshold):
    week = charge(db, card, "900.00", day=today() - dt.timedelta(days=7), merchant_name="Week")
    older = charge(db, card, "900.00", day=today() - dt.timedelta(days=8), merchant_name="Old")
    evaluate(db, notifier, new_charges(item, week, older))
    assert notifier.titles == ["$900.00 at Week"]


@pytest.mark.parametrize(
    "outcome_kwargs",
    [
        {"initial_history_load": True},
        {"status": "not_ready"},
    ],
)
def test_no_large_charge_alerts_on_history_loads_or_without_data(
    db, notifier, item, card, threshold, outcome_kwargs
):
    big = charge(db, card, "2500.00")
    evaluate(db, notifier, new_charges(item, big, **outcome_kwargs))
    assert keys(db, "large_charge") == []


def test_no_large_charge_alerts_after_a_failure_or_when_off(db, notifier, item, card):
    big = charge(db, card, "2500.00")
    evaluate(db, notifier, new_charges(item, big))  # no threshold set
    failed = fail(item, "INSTITUTION_DOWN", inserted_transaction_ids=(big.id,))
    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    evaluate(db, notifier, failed)
    assert keys(db, "large_charge") == []


def test_csv_rows_never_raise_large_charge_alerts(db, notifier, item, card, threshold):
    row = charge(db, card, "2500.00", source="csv")
    evaluate(db, notifier, new_charges(item, row))
    assert keys(db, "large_charge") == []


def test_a_posting_never_alerts_again(db, notifier, item, card, threshold):
    """Safety net: a posted row naming a pending row shares that row's key."""
    hold = charge(db, card, "800.00", pending=True, merchant_name="Hilton")
    evaluate(db, notifier, new_charges(item, hold))
    posted = charge(db, card, "812.50", pending_transaction_id=hold.plaid_transaction_id)
    evaluate(db, notifier, new_charges(item, posted))
    both = charge(db, card, "900.00", pending=True, merchant_name="Delta")
    twin = charge(db, card, "900.00", pending_transaction_id=both.plaid_transaction_id)
    evaluate(db, notifier, new_charges(item, both, twin))
    assert notifier.titles == ["$800.00 at Hilton", "$900.00 at Delta"]


def test_three_new_charges_notify_one_by_one(db, notifier, item, card, threshold):
    rows = [charge(db, card, f"{600 + i}.00", merchant_name=f"Shop {i}") for i in range(3)]
    evaluate(db, notifier, new_charges(item, *rows))
    assert notifier.titles == ["$600.00 at Shop 0", "$601.00 at Shop 1", "$602.00 at Shop 2"]


def test_more_new_charges_are_summed_up_in_one_notification(db, notifier, item, card, threshold):
    rows = [charge(db, card, f"{600 + i}.00", merchant_name=f"Shop {i}") for i in range(5)]
    old = charge(db, card, "700.00", merchant_name="Seen")
    evaluate(db, notifier, new_charges(item, old))
    results = evaluate(db, notifier, new_charges(item, old, *rows, sync_run_id=42))
    assert notifier.titles == ["$700.00 at Seen", "5 new charges over $500.00"]
    assert notifier.sent[1].message == (
        "Shop 0 $600.00, Shop 1 $601.00, Shop 2 $602.00 and 2 more. They are on the "
        "Transactions page."
    )
    assert [r.queued for r in results] == [False] * 5 + [True]
    assert len(keys(db, "large_charge")) == 7 and f"large-charges:{item.id}:42" in keys(db)


# --------------------------------------------------------------------------- isolation


def test_one_broken_evaluation_does_not_stop_the_others(
    db, notifier, item, card, threshold, monkeypatch, caplog
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(alerts, "_consent", broken)
    item.consent_expires_at = NOW + 5 * DAY
    big = charge(db, card, "2500.00", merchant_name="Apple")
    evaluate(db, notifier, new_charges(item, big))
    assert notifier.titles == ["$2,500.00 at Apple"]
    assert "the consent reminder failed" in caplog.text


def test_budgets_are_evaluated_after_successful_runs_only(db, notifier, item, monkeypatch):
    calls: list[dt.date | None] = []
    monkeypatch.setattr(
        alerts, "evaluate_budgets", lambda s, n, *, today=None: calls.append(today) or []
    )
    evaluate(db, notifier, succeed(item))
    evaluate(db, notifier, SyncOutcome(item.id, "schedule", "not_ready"))
    evaluate(db, notifier, fail(item, "INSTITUTION_DOWN"))
    assert calls == [today(), today()]


# --------------------------------------------------------------------------- the real engine


class Linked:
    """A fake Plaid Item linked in the test database (as in tests/test_sync.py)."""

    def __init__(self, db, session_factory, fake, store, settings, notifier) -> None:
        self.db, self.session_factory, self.fake = db, session_factory, fake
        self.store, self.settings, self.notifier = store, settings, notifier
        self.fake_item = fake.add_item(item_id="item-amex")
        app_secrets.store_access_token(
            settings, "item-amex", self.fake_item.access_token, store=store
        )
        self.item = make_item(db, plaid_item_id="item-amex", last_sync_success_at=None)

    def sync(self, notifier=None) -> SyncOutcome:
        outcome = sync.sync_item(
            self.session_factory,
            self.fake,
            self.store,
            self.settings,
            self.item.id,
            trigger="schedule",
            notifier=notifier or self.notifier,
            sleep=lambda _s: None,
        )
        self.db.expire_all()
        return outcome


@pytest.fixture
def linked(db, session_factory, fake_plaid, secrets, settings, notifier) -> Linked:
    return Linked(db, session_factory, fake_plaid, secrets, settings, notifier)


def test_the_engine_alerts_a_reconnect_once_until_it_is_fixed(linked, fake_plaid, notifier):
    linked.sync()
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    assert linked.sync().needs_reconnect
    linked.sync()
    assert notifier.titles == ["Reconnect American Express"]
    fake_plaid.set_item_error(linked.fake_item, None)
    assert linked.sync().status == "success"
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    linked.sync()
    assert notifier.titles == ["Reconnect American Express"] * 2


def test_the_engine_alerts_a_failure_once_a_day(linked, fake_plaid, notifier):
    linked.sync()
    for _ in range(2):
        fake_plaid.queue_error(
            "transactions_sync", fake_plaid.plaid_error("INSTITUTION_NOT_RESPONDING")
        )
        assert linked.sync().status == "error"
    assert notifier.titles == ["American Express sync failed"]


def test_the_engine_alerts_a_large_charge_once_and_not_at_posting(db, linked, fake_plaid, notifier):
    set_app_settings(db, large_charge_threshold=Decimal("1000.00"))
    db.commit()
    recent = today() - dt.timedelta(days=1)
    fake_plaid.add_transaction(linked.fake_item, amount="3000.00", date=recent, name="OLD TV")
    assert linked.sync().initial_history_load  # two years of backfill never notify
    pending = fake_plaid.add_pending(
        linked.fake_item, amount="1800.00", date=recent, name="DELTA AIR", merchant_name="Delta"
    )
    linked.sync()
    fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], amount="1850.00")
    assert linked.sync().added == 1
    assert notifier.titles == ["$1,800.00 at Delta"]


def test_the_engine_alerts_a_budget_crossed_by_a_sync(db, linked, fake_plaid, notifier):
    dining = get_category(db, "Dining & Drinks")
    make_budget(db, dining, "100.00")
    db.commit()
    linked.sync()
    fake_plaid.add_transaction(
        linked.fake_item,
        amount="95.00",
        date=today(),
        name="TARTINE",
        pfc_primary="FOOD_AND_DRINK",
        pfc_detailed="FOOD_AND_DRINK_RESTAURANT",
    )
    linked.sync()
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]


def test_a_broken_notifier_never_breaks_a_sync(db, linked, fake_plaid, caplog):
    class Broken:
        def notify(self, *_args, **_kwargs):
            raise OSError("osascript is gone")

    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    db.commit()
    linked.sync()
    fake_plaid.add_transaction(linked.fake_item, amount="250.00", date=today(), name="BIG")
    outcome = linked.sync(notifier=Broken())
    assert outcome.status == "success" and outcome.added == 1
    stored = db.scalars(select(AlertEvent).where(AlertEvent.kind == "large_charge")).one()
    assert not stored.delivered and "OSError: osascript is gone" in stored.error
    assert "was not sent" in caplog.text


def test_an_unattended_sync_sends_its_alerts(db, linked, fake_plaid, notifier, monkeypatch):
    """The launchd agent's run: ``./run sync --if-due --unattended``."""
    monkeypatch.setattr(app_secrets, "enable_unattended_mode", lambda: True)
    linked.sync()
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    rt = sync_cmd.Runtime(
        linked.settings,
        session_factory=linked.session_factory,
        gateway=fake_plaid,
        store=linked.store,
        notifier=notifier,
        sleep=lambda _s: None,
        out=lambda _line: None,
    )
    args = type("Args", (), {"if_due": True, "min_age_hours": 0.0, "unattended": True})()
    args.item = None
    assert sync_cmd.sync_command(args, rt) == 1
    assert notifier.titles == ["Reconnect American Express"]


def test_a_recording_notifier_failure_is_recorded_on_the_event(db, linked, fake_plaid):
    failing = RecordingNotifier(fail_with="osascript exited 1: not allowed")
    linked.sync()
    fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
    linked.sync(notifier=failing)
    stored = db.scalars(select(AlertEvent).where(AlertEvent.kind == "reconnect")).one()
    assert stored.error == "osascript exited 1: not allowed" and not stored.delivered
