from sqlalchemy import select

from budgetapp.models import AlertEvent
from budgetapp.services import alerts
from budgetapp.services.events import ImportOutcome, SyncOutcome
from tests.factories import get_category, make_item, make_transaction


def _record(db, notifier, key="budget:1:2026-10-01:80", **kwargs):
    values = {
        "kind": "budget",
        "dedupe_key": key,
        "title": "Dining at 80%",
        "message": "You've used $320 of $400.",
    }
    values.update(kwargs)
    return alerts.record_and_notify(db, notifier, **values)


def _event(db, alert_id):
    db.expire_all()
    return db.get(AlertEvent, alert_id)


def test_records_and_notifies_once(db, notifier):
    first = _record(db, notifier)
    second = _record(db, notifier, message="a different message")
    assert first.inserted and first.queued and first.alert_id is not None
    assert not second.inserted and second.alert_id is None and not second.queued
    assert notifier.sent == []  # nothing goes out before the caller commits
    db.commit()
    assert notifier.titles == ["Dining at 80%"]
    event = db.scalars(select(AlertEvent).where(AlertEvent.dedupe_key == first.dedupe_key)).one()
    db.refresh(event)
    assert event.delivered and event.delivered_at is not None and event.error is None
    db.commit()  # a second commit sends nothing new
    assert len(notifier.sent) == 1


def test_a_rolled_back_alert_is_never_sent_and_can_fire_later(session_factory, notifier):
    first = session_factory()
    _record(first, notifier, key="stale:1:2026-10-28")
    first.rollback()  # e.g. a later alert in the same best-effort transaction failed
    first.close()
    assert notifier.sent == []
    second = session_factory()
    assert _record(second, notifier, key="stale:1:2026-10-28").inserted
    second.commit()
    second.close()
    assert len(notifier.sent) == 1


def test_an_alert_inside_a_rolled_back_savepoint_is_not_sent(db, notifier):
    kept = _record(db, notifier, key="kept")
    savepoint = db.begin_nested()
    _record(db, notifier, key="dropped")
    savepoint.rollback()
    db.commit()
    assert notifier.titles == ["Dining at 80%"]
    assert _event(db, kept.alert_id).delivered


def test_links_rows(db, notifier):
    item = make_item(db)
    txn = make_transaction(db)
    category = get_category(db, "Travel")
    result = _record(
        db, notifier, key="large:1", transaction_id=txn.id, item_id=item.id, category_id=category.id
    )
    event = db.get(AlertEvent, result.alert_id)
    assert (event.transaction_id, event.item_id, event.category_id) == (
        txn.id,
        item.id,
        category.id,
    )


def test_failed_delivery_is_recorded(db, notifier):
    notifier.fail_with = "osascript exited 1"
    result = _record(db, notifier)
    db.commit()
    event = _event(db, result.alert_id)
    assert event.delivered is False and event.delivered_at is None
    assert event.error == "osascript exited 1"


def test_a_crashing_notifier_never_breaks_the_caller(db):
    class Broken:
        def notify(self, *args, **kwargs):
            raise RuntimeError("boom")

    result = _record(db, Broken())
    db.commit()  # does not raise
    event = _event(db, result.alert_id)
    assert result.inserted and not event.delivered and "RuntimeError" in event.error


def test_clear_allows_firing_again(db, notifier):
    _record(db, notifier)
    db.commit()
    assert alerts.clear_alert(db, "budget:1:2026-10-01:80") is True
    assert alerts.clear_alert(db, "budget:1:2026-10-01:80") is False
    assert _record(db, notifier).inserted
    db.commit()
    assert len(notifier.sent) == 2


def test_record_without_notifying(db, notifier):
    result = _record(db, notifier, notify=False)
    db.commit()
    assert result.inserted and not result.queued and notifier.sent == []


def test_deleting_the_transaction_keeps_the_alert(db, notifier):
    txn = make_transaction(db)
    result = _record(db, notifier, key="large:2", transaction_id=txn.id)
    db.delete(txn)
    db.flush()
    db.expire_all()
    assert db.get(AlertEvent, result.alert_id).transaction_id is None


def test_evaluation_stubs_return_nothing(db, notifier):
    outcome = SyncOutcome(item_id=1, trigger="manual", status="success")
    assert alerts.evaluate_after_sync(db, notifier, outcome) == []
    assert (
        alerts.evaluate_after_import(db, notifier, ImportOutcome(account_id=1, filename="a.csv"))
        == []
    )
    assert alerts.evaluate_budgets(db, notifier) == []


def test_outcome_helpers():
    ok = SyncOutcome(item_id=1, trigger="schedule", status="not_ready")
    assert ok.ok and not ok.changed
    failed = SyncOutcome(
        item_id=1, trigger="schedule", status="error", item_status="needs_reconnect", added=1
    )
    assert not failed.ok and failed.needs_reconnect and failed.changed
    assert ImportOutcome(account_id=1, filename="x", inserted_count=2).changed
