import datetime as dt

from budgetapp.services.status import connection_status
from tests.factories import make_item, make_sync_run, set_app_settings

NOW = dt.datetime(2026, 10, 15, 16, 0, tzinfo=dt.UTC)


def test_not_linked(db):
    status = connection_status(db, now=NOW)
    assert status.state == "not_linked" and not status.linked and status.primary is None
    assert status.label == "Not linked"


def test_healthy(db):
    make_item(
        db,
        linked_at=NOW - dt.timedelta(days=10),
        last_sync_success_at=NOW - dt.timedelta(hours=2),
        last_consented_at=NOW - dt.timedelta(days=10),
    )
    status = connection_status(db, now=NOW)
    assert status.state == "healthy" and status.label == "Connected"
    assert not status.stale and not status.consent_warning
    assert status.primary.consent_days_left == 355


def test_stale_after_36_hours_whatever_the_status_says(db):
    make_item(
        db, linked_at=NOW - dt.timedelta(days=10), last_sync_success_at=NOW - dt.timedelta(hours=37)
    )
    status = connection_status(db, now=NOW)
    assert status.state == "stale" and status.stale


def test_never_synced_counts_from_linking(db):
    make_item(db, linked_at=NOW - dt.timedelta(hours=2), last_sync_success_at=None)
    assert connection_status(db, now=NOW).state == "healthy"
    db.expire_all()


def test_worst_item_wins(db):
    make_item(db)
    make_item(db, status="needs_reconnect", error_code="ITEM_LOGIN_REQUIRED")
    make_item(db, status="error", error_code="INSTITUTION_DOWN")
    status = connection_status(db)
    assert status.state == "needs_reconnect" and status.needs_reconnect
    assert status.primary.error_code == "ITEM_LOGIN_REQUIRED"
    assert [i.state for i in status.items] == ["needs_reconnect", "error", "healthy"]


def test_consent_from_plaid_or_365_days_after_consenting(db):
    set_app_settings(db, consent_reminder_days=30)
    make_item(db, consent_expires_at=NOW + dt.timedelta(days=20))
    status = connection_status(db, now=NOW)
    assert status.consent_warning and status.primary.consent_days_left == 20
    db.rollback()

    item = make_item(db, consent_expires_at=None, last_consented_at=NOW - dt.timedelta(days=340))
    status = connection_status(db, now=NOW)
    assert status.primary.consent_due_at == item.last_consented_at + dt.timedelta(days=365)
    assert status.primary.consent_days_left == 25 and status.consent_warning


def test_syncing_flag_ignores_old_running_rows(db):
    item = make_item(db)
    make_sync_run(db, item, status="running", started_at=NOW - dt.timedelta(hours=3))
    assert not connection_status(db, now=NOW).syncing
    make_sync_run(db, item, status="running", started_at=NOW - dt.timedelta(minutes=2))
    assert connection_status(db, now=NOW).syncing
