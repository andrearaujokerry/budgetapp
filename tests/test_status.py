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


# --------------------------------------------------------------------------- wave 3 (BANK-6)


def test_the_year_estimate_is_for_american_express_only(db):
    """BANK-6: other banks use Plaid's consent_expiration_time or have no due date at all."""
    set_app_settings(db, consent_reminder_days=30)
    old = NOW - dt.timedelta(days=400)
    bank = make_item(db, institution_id="ins_3", institution_name="TD Bank", last_consented_at=old)
    (state,) = connection_status(db, now=NOW).items
    assert state.item_id == bank.id and state.institution_id == "ins_3"
    assert state.consent_due_at is None and state.consent_days_left is None
    assert not state.consent_warning and not state.consent_expired
    assert not state.consent_estimated
    status = connection_status(db, now=NOW)
    assert not status.consent_warning and status.consent_item is None
    assert not status.consent_expired

    bank.consent_expires_at = NOW + dt.timedelta(days=12)  # Plaid sent a date: it counts
    db.flush()
    (state,) = connection_status(db, now=NOW).items
    assert (state.consent_days_left, state.consent_warning) == (12, True)
    assert not state.consent_estimated


def test_an_amex_item_saved_without_an_institution_id_keeps_the_estimate(db):
    item = make_item(
        db,
        institution_id=None,
        institution_name="American Express",
        last_consented_at=NOW - dt.timedelta(days=340),
    )
    (state,) = connection_status(db, now=NOW).items
    assert state.consent_due_at == item.last_consented_at + dt.timedelta(days=365)
    assert state.consent_estimated and state.consent_warning
    nameless = make_item(db, institution_id=None, institution_name=None)
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[nameless.id].consent_due_at is None
    assert by_id[nameless.id].name == "A bank connection"


def test_the_banners_name_each_institution_and_the_first_consent_due(db):
    set_app_settings(db, consent_reminder_days=30)
    make_item(db, institution_name="American Express", consent_expires_at=NOW + dt.timedelta(9))
    td = make_item(
        db,
        institution_id="ins_14",
        institution_name="TD Bank",
        consent_expires_at=NOW + dt.timedelta(days=3),
    )
    make_item(
        db,
        institution_id="ins_5",
        institution_name="Citibank",
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
    )
    make_item(db, institution_id="ins_6", institution_name="Chase", status="needs_reconnect")
    status = connection_status(db, now=NOW)
    assert status.reconnect_names == ("Citibank", "Chase")
    assert status.consent_item.item_id == td.id


def test_liabilities_apply_only_to_connections_with_a_card(db):
    """BANK-7: a connection with only bank accounts has no statement details to share."""
    from tests.factories import make_account

    card_item = make_item(db)
    make_account(db, card_item)
    bank_item = make_item(db, institution_id="ins_14", institution_name="TD Bank")
    make_account(db, bank_item, type="depository", subtype="checking", name="Checking")
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[card_item.id].has_cards and not by_id[bank_item.id].has_cards
