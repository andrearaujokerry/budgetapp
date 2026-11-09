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
from sqlalchemy import select, text

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.commands import sync as sync_cmd
from budgetapp.db import LOCK_NS_SYNC
from budgetapp.models import AlertEvent, PlaidItem
from budgetapp.services import alerts, sync
from budgetapp.services.events import PendingRaise, SyncOutcome
from budgetapp.services.notify import NotifyResult, RecordingNotifier
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
        "American Express isn't answering Plaid right now. The next sync will try again. "
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
        "American Express needs you to sign in again through Plaid (the consent expired or was "
        "revoked). Reconnect fixes it and keeps this connection. Open the Connection page and "
        "click Reconnect."
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
    assert notifier.sent[0].message.startswith("American Express didn't accept the sign-in.")


# --------------------------------------------------------------------------- ALR-2 stale


def test_stale_data_alerts_once_a_day(db, notifier):
    linked = make_item(db, last_sync_success_at=None, linked_at=NOW - 40 * HOUR)
    not_ready = SyncOutcome(linked.id, "schedule", "not_ready", item_status="healthy")
    evaluate(db, notifier, not_ready)
    (sent,) = notifier.sent
    assert sent.title == "American Express data is out of date"
    assert sent.message == (
        "No transactions have arrived since American Express was linked 40 hours ago. Check the "
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
        "American Express isn't answering Plaid right now. The next sync will try again. Code: "
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


def test_no_consent_reminder_while_a_reconnect_is_needed(db, notifier, item):
    item.consent_expires_at = NOW - DAY  # expired: Plaid now answers ITEM_LOGIN_REQUIRED
    evaluate(db, notifier, fail(item, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == ["Reconnect American Express"] and keys(db, "consent") == []
    item.consent_expires_at = NOW + 20 * DAY  # a reconnect that did not move the date
    evaluate(db, notifier, succeed(item))
    assert notifier.titles[-1] == "Renew your American Express connection"


def test_a_bank_connection_names_its_bank_and_has_no_estimated_consent(db, notifier):
    """BANK-6/BANK-7: connection alerts name the institution; only American Express gets the
    12-month consent estimate, so a bank without a date from Plaid never gets a reminder."""
    bank = make_item(
        db,
        institution_id="ins_14",
        institution_name="TD Bank",
        last_sync_success_at=NOW - 2 * HOUR,
        linked_at=NOW - 400 * DAY,
        last_consented_at=NOW - 400 * DAY,
    )
    evaluate(db, notifier, fail(bank, "ITEM_LOGIN_REQUIRED", reconnect=True))
    assert notifier.titles == ["Reconnect TD Bank"]
    assert notifier.sent[0].message.startswith("TD Bank needs you to sign in again through Plaid")
    evaluate(db, notifier, succeed(bank))
    assert keys(db, "consent") == [] and notifier.titles == ["Reconnect TD Bank"]
    bank.consent_expires_at = NOW + 10 * DAY  # Plaid sends a date: it counts
    evaluate(db, notifier, succeed(bank))
    assert notifier.titles[-1] == "Renew your TD Bank connection"


def test_a_connection_without_a_name_stays_neutral(db, notifier):
    nameless = make_item(
        db, institution_name=None, institution_id=None, last_sync_success_at=NOW - 2 * HOUR
    )
    evaluate(db, notifier, fail(nameless, "INSTITUTION_DOWN"))
    assert notifier.titles == ["Your bank sync failed"]
    assert notifier.sent[0].message.startswith("The bank isn't answering Plaid right now.")


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


def test_a_catch_up_sync_counts_the_7_days_back_from_the_previous_sync(
    db, notifier, item, card, threshold
):
    """Final review reqs-3: after a break of over a week, the charges of the break's first days
    were never announced."""
    previous = NOW - 12 * DAY
    since = local_date(previous) - dt.timedelta(days=7)
    rows = [
        charge(db, card, "900.00", day=since, merchant_name="First"),
        charge(db, card, "900.00", day=since - dt.timedelta(days=1), merchant_name="Before"),
    ]
    evaluate(db, notifier, new_charges(item, *rows, previous_success_at=previous))
    assert notifier.titles == ["$900.00 at First"]


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


def raised(item, row, pending_id: str, before: str, **kwargs: Any) -> SyncOutcome:
    """A sync that stored ``row`` for more than the ``before`` its pending charge had."""
    kwargs.setdefault("sync_run_id", 8)
    return succeed(
        item, pending_raises=(PendingRaise(row.id, pending_id, Decimal(before)),), **kwargs
    )


def test_a_charge_that_goes_over_the_amount_when_it_posts_alerts_once(
    db, notifier, item, card, threshold
):
    """A tip or hotel incidentals added at posting: the pending charge was under the amount."""
    posted = charge(db, card, "560.00", merchant_name="Hilton", pending_transaction_id="hold-1")
    evaluate(db, notifier, raised(item, posted, "hold-1", "450.00"))
    (sent,) = notifier.sent
    assert sent.title == "$560.00 at Hilton"
    assert sent.message == (
        f"A charge on {today():%b} {today().day} posted for more than the pending $450.00 and is "
        "over your $500.00 large-charge alert."
    )
    assert keys(db, "large_charge") == ["large-charge:hold-1"]
    assert event(db, "large-charge:hold-1").transaction_id == posted.id
    evaluate(db, notifier, raised(item, posted, "hold-1", "450.00"))
    assert len(notifier.sent) == 1


def test_a_pending_charge_raised_in_place_says_so(db, notifier, item, card, threshold):
    hold = charge(db, card, "640.00", pending=True, merchant_name="Hertz")
    evaluate(db, notifier, raised(item, hold, hold.plaid_transaction_id, "300.00"))
    assert notifier.sent[0].message == (
        f"A pending charge on {today():%b} {today().day} went up from $300.00 and is over your "
        "$500.00 large-charge alert."
    )
    assert keys(db, "large_charge") == [f"large-charge:{hold.plaid_transaction_id}"]


def test_raised_charges_alert_only_when_they_first_go_over_the_amount(
    db, notifier, item, card, threshold
):
    was_over = charge(db, card, "700.00", pending_transaction_id="p-over")
    still_under = charge(db, card, "500.00", pending_transaction_id="p-under")
    old = charge(db, card, "800.00", day=today() - dt.timedelta(days=8), pending_transaction_id="p")
    csv_row = charge(db, card, "900.00", source="csv")
    outcome = succeed(
        item,
        pending_raises=(
            PendingRaise(was_over.id, "p-over", Decimal("520.00")),  # it was over while pending
            PendingRaise(still_under.id, "p-under", Decimal("450.00")),
            PendingRaise(old.id, "p", Decimal("400.00")),
            PendingRaise(csv_row.id, "p-csv", Decimal("400.00")),
        ),
    )
    evaluate(db, notifier, outcome)
    posted = charge(db, card, "800.00", pending_transaction_id="p-new")
    evaluate(db, notifier, raised(item, posted, "p-new", "400.00", initial_history_load=True))
    evaluate(db, notifier, raised(item, posted, "p-new", "400.00", status="not_ready"))
    assert keys(db, "large_charge") == [] and notifier.sent == []


def test_a_raised_charge_that_already_alerted_while_pending_stays_silent(
    db, notifier, item, card, threshold
):
    hold = charge(db, card, "600.00", pending=True, merchant_name="Hilton")
    evaluate(db, notifier, new_charges(item, hold))
    set_app_settings(db, large_charge_threshold=Decimal("650.00"))  # the amount was raised since
    posted = charge(db, card, "700.00", pending_transaction_id=hold.plaid_transaction_id)
    evaluate(db, notifier, raised(item, posted, hold.plaid_transaction_id, "600.00"))
    assert notifier.titles == ["$600.00 at Hilton"]


def test_new_and_raised_charges_share_the_summary(db, notifier, item, card, threshold):
    rows = [charge(db, card, f"{600 + i}.00", merchant_name=f"Shop {i}") for i in range(3)]
    posted = charge(db, card, "650.00", merchant_name="Tip", pending_transaction_id="p-tip")
    outcome = succeed(
        item,
        sync_run_id=9,
        inserted_transaction_ids=tuple(r.id for r in rows),
        pending_raises=(PendingRaise(posted.id, "p-tip", Decimal("480.00")),),
    )
    evaluate(db, notifier, outcome)
    assert notifier.titles == ["4 new charges over $500.00"]
    assert "large-charge:p-tip" in keys(db, "large_charge")


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


def test_the_engine_says_how_to_fix_rejected_plaid_keys(linked, fake_plaid, notifier):
    """Final review ops-4: the notification said only "Plaid reported an error"."""
    linked.sync()
    error = fake_plaid.plaid_error("INVALID_API_KEYS", error_type="INVALID_INPUT")
    fake_plaid.queue_error("transactions_sync", error)
    assert linked.sync().status == "error"
    (sent,) = notifier.sent
    assert "./run setup --keys-only" in sent.message
    assert sent.message.endswith("Code: INVALID_API_KEYS.")


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


@pytest.mark.parametrize("break_kind", ["mac off", "reconnect"])
def test_the_engine_announces_a_large_charge_from_a_catch_up_sync(
    db, linked, fake_plaid, notifier, break_kind
):
    """Final review reqs-3: the catch-up sync after a break of over a week (the Mac off, or a
    reconnect fixed late) stored the break's large charges silently."""
    set_app_settings(db, large_charge_threshold=Decimal("500.00"))
    db.commit()
    at(NOW - 12 * DAY)
    assert linked.sync().initial_history_load
    assert not linked.sync().initial_history_load
    if break_kind == "reconnect":
        at(NOW - 10 * DAY)
        fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
        assert linked.sync().needs_reconnect
        fake_plaid.set_item_error(linked.fake_item, None)
    at(NOW)
    early = (NOW - 9 * DAY).date()
    fake_plaid.add_transaction(linked.fake_item, amount="2400.00", date=early, merchant_name="Ikea")
    recent = (NOW - 3 * DAY).date()
    fake_plaid.add_transaction(linked.fake_item, amount="700.00", date=recent, merchant_name="Rei")
    assert linked.sync().status == "success"
    assert [t for t in notifier.titles if " at " in t] == ["$2,400.00 at Ikea", "$700.00 at Rei"]


def test_a_charge_dated_over_a_week_ago_stays_silent_after_a_recent_sync(
    db, linked, fake_plaid, notifier
):
    """The window still keeps a charge Plaid delivers late out of the alerts."""
    set_app_settings(db, large_charge_threshold=Decimal("500.00"))
    db.commit()
    at(NOW - 2 * HOUR)
    linked.sync()
    linked.sync()
    at(NOW)
    late = (NOW - 9 * DAY).date()
    fake_plaid.add_transaction(linked.fake_item, amount="2400.00", date=late, merchant_name="Ikea")
    outcome = linked.sync()
    assert (outcome.added, notifier.sent) == (1, [])


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


@pytest.mark.parametrize(("link", "posted_amount"), [(True, "115.00"), (False, "105.00")])
def test_the_engine_alerts_a_charge_that_first_goes_over_the_amount_when_it_posts(
    db, linked, fake_plaid, notifier, link, posted_amount
):
    """Pending $90 under a $100 alert, posted higher with the tip: alerted once, at posting
    (``link=False``: Plaid didn't name the pending row; the app matched it)."""
    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    db.commit()
    linked.sync()  # the history load
    hold = fake_plaid.add_pending(
        linked.fake_item, amount="90.00", date=today(), name="TARTINE", merchant_name="Tartine"
    )
    linked.sync()
    assert notifier.sent == []
    fake_plaid.post_pending(
        linked.fake_item, hold["transaction_id"], amount=posted_amount, link=link
    )
    outcome = linked.sync()
    assert outcome.inserted_transaction_ids == ()  # the posting continues a known charge
    assert notifier.titles == [f"${posted_amount} at Tartine"]
    assert "posted for more than the pending $90.00" in notifier.sent[0].message
    assert keys(db, "large_charge") == [f"large-charge:{hold['transaction_id']}"]
    linked.sync()
    assert len(notifier.sent) == 1


def test_the_engine_alerts_a_pending_charge_plaid_raised_in_place(db, linked, fake_plaid, notifier):
    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    db.commit()
    linked.sync()
    hold = fake_plaid.add_pending(
        linked.fake_item, amount="90.00", date=today(), name="HERTZ", merchant_name="Hertz"
    )
    linked.sync()
    fake_plaid.modify_transaction(linked.fake_item, hold["transaction_id"], amount="140.00")
    linked.sync()
    fake_plaid.post_pending(linked.fake_item, hold["transaction_id"], amount="150.00")
    linked.sync()
    assert notifier.titles == ["$140.00 at Hertz"]
    assert "went up from $90.00" in notifier.sent[0].message


def test_the_engine_stays_silent_when_a_charge_over_the_amount_on_the_history_load_posts(
    db, linked, fake_plaid, notifier
):
    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    db.commit()
    hold = fake_plaid.add_pending(linked.fake_item, amount="150.00", date=today(), name="DELTA")
    assert linked.sync().initial_history_load
    fake_plaid.post_pending(linked.fake_item, hold["transaction_id"], amount="170.00")
    linked.sync()
    assert notifier.sent == []


class LockProbe:
    """A notifier that records, for each notification, whether the Item's sync lock is held."""

    def __init__(self, engine, item_id: int) -> None:
        self.engine, self.item_id = engine, item_id
        self.titles: list[str] = []
        self.held: list[int] = []

    def notify(self, title: str, message: str, *, subtitle: str = "") -> NotifyResult:
        with self.engine.connect() as conn:
            held = conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND classid = :ns AND objid = :item"
                ),
                {"ns": LOCK_NS_SYNC, "item": self.item_id},
            ).scalar_one()
        self.titles.append(title)
        self.held.append(held)
        return NotifyResult(True)


@pytest.mark.parametrize("case", ["reconnect", "internal", "large charge"])
def test_notifications_go_out_after_the_sync_lock_is_released(db, linked, fake_plaid, engine, case):
    """ARCHITECTURE §11: no notification while a lock is held (osascript can take seconds, and a
    sync of the Item started meanwhile would be recorded as skipped)."""
    set_app_settings(db, large_charge_threshold=Decimal("100.00"))
    db.commit()
    linked.sync()
    probe = LockProbe(engine, linked.item.id)
    if case == "reconnect":
        fake_plaid.set_item_error(linked.fake_item, "ITEM_LOGIN_REQUIRED")
        linked.sync(notifier=probe)
        assert probe.titles == ["Reconnect American Express"]
    elif case == "internal":  # the alert still goes out when the exception is re-raised
        fake_plaid.queue_error("transactions_sync", RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            linked.sync(notifier=probe)
        assert probe.titles == ["American Express sync failed"]
    else:
        fake_plaid.add_transaction(linked.fake_item, amount="250.00", date=today(), name="BIG")
        linked.sync(notifier=probe)
        assert len(probe.titles) == 1
    assert probe.held == [0]
