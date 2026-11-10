import datetime as dt

from budgetapp.services.status import connection_status
from tests.bank_factories import make_bank_account
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
    bank = make_item(
        db, institution_id="ins_109511", institution_name="Tartan Bank", last_consented_at=old
    )
    (state,) = connection_status(db, now=NOW).items
    assert state.item_id == bank.id and state.institution_id == "ins_109511"
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
    tartan = make_item(
        db,
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        consent_expires_at=NOW + dt.timedelta(days=3),
    )
    make_item(
        db,
        institution_id="ins_135858",
        institution_name="Windowpane Bank",
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
    )
    make_item(
        db,
        institution_id="ins_109512",
        institution_name="Houndstooth Bank",
        status="needs_reconnect",
    )
    status = connection_status(db, now=NOW)
    assert status.reconnect_names == ("Windowpane Bank", "Houndstooth Bank")
    assert status.consent_item.item_id == tartan.id


def test_liabilities_apply_only_to_connections_with_a_card(db):
    """BANK-7: a connection with only bank accounts has no statement details to share."""
    from tests.factories import make_account

    card_item = make_item(db)
    make_account(db, card_item)
    bank_item = make_item(db, institution_id="ins_109511", institution_name="Tartan Bank")
    make_bank_account(db, bank_item, name="Checking")
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[card_item.id].has_cards and not by_id[bank_item.id].has_cards


# ------------------------------------------------------------------ W3-BANK review fixes (UX)


def test_another_banks_reconnect_never_hides_a_consent_that_is_due(db):
    """UX review (major): a reconnect at Tartan Bank renews nothing at American Express, so Amex's
    due consent stays in consent_items. A connection that itself needs reconnecting is left out
    (its Reconnect renews its consent too). Several due consents come soonest first."""
    set_app_settings(db, consent_reminder_days=30)
    amex = make_item(db, consent_expires_at=NOW + dt.timedelta(days=5))
    make_item(
        db,
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
        consent_expires_at=NOW - dt.timedelta(days=1),  # renewed by its own reconnect
    )
    windowpane = make_item(
        db,
        institution_id="ins_135858",
        institution_name="Windowpane Bank",
        consent_expires_at=NOW - dt.timedelta(days=2),
    )
    status = connection_status(db, now=NOW)
    assert [i.item_id for i in status.consent_items] == [windowpane.id, amex.id]
    assert status.consent_item.item_id == windowpane.id
    assert status.needs_reconnect and status.syncable


def test_syncable_means_some_connection_can_sync(db):
    """UX review: one broken bank never takes Sync now away from the others; with none that can
    sync (each needs reconnecting or its consent expired) the action is the repair."""
    make_item(db, status="needs_reconnect", error_code="ITEM_LOGIN_REQUIRED")
    assert not connection_status(db, now=NOW).syncable
    make_item(
        db,
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        consent_expires_at=NOW - dt.timedelta(days=1),
    )
    assert not connection_status(db, now=NOW).syncable
    make_item(db, institution_id="ins_135858", institution_name="Windowpane Bank")
    assert connection_status(db, now=NOW).syncable


def test_two_logins_at_one_bank_get_labels_from_their_first_account(db):
    """UX review: BANK-3 allows a second login at a bank already linked; every place that names
    a connection tells them apart. A name of its own keeps no label."""
    from tests.factories import make_account

    mine = make_item(db, institution_id="ins_109512", institution_name="Houndstooth Bank")
    make_account(db, mine, name="Travel Rewards", mask="1234")
    theirs = make_item(db, institution_id="ins_109512", institution_name="HOUNDSTOOTH BANK")
    make_account(db, theirs, name="Cash Rewards", mask="5678")
    tartan = make_item(db, institution_id="ins_109511", institution_name="Tartan Bank")
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[mine.id].name == "Houndstooth Bank (Travel Rewards ··1234)"
    assert by_id[theirs.id].name == "HOUNDSTOOTH BANK (Cash Rewards ··5678)"  # the same bank
    assert by_id[tartan.id].label is None and by_id[tartan.id].name == "Tartan Bank"


def test_test_items_with_the_same_first_account_are_numbered(db):
    """Sandbox test Items made from one test user share the bank and the card: "login N"."""
    from tests.factories import make_account

    first, second = (make_item(db, institution_name="First Platypus Bank") for _ in range(2))
    for item in (first, second):
        make_account(db, item, name="Plaid Credit Card", mask="3333")
    nameless = make_item(db, institution_id=None, institution_name=None)
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[first.id].name == "First Platypus Bank (login 1)"
    assert by_id[second.id].name == "First Platypus Bank (login 2)"
    assert by_id[nameless.id].display_name is None
    assert by_id[nameless.id].name == "A bank connection"


def test_a_hidden_account_never_names_a_connection(db):
    """A label shows on every page (the pill's tooltip, the banners), so it comes from a visible
    account: a login whose only account is hidden makes its group fall back to "login N"."""
    from tests.factories import make_account

    first = make_item(db)
    make_account(db, first, name="Gold Card", mask="1001", is_hidden=True)
    make_account(db, first, name="Platinum Card", mask="1005")
    second = make_item(db)
    make_account(db, second, name="Old card", mask="9999", is_hidden=True)
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[first.id].name == "American Express (login 1)"
    assert by_id[second.id].name == "American Express (login 2)"
    make_account(db, second, name="Blue Cash", mask="2002")
    by_id = {s.item_id: s for s in connection_status(db, now=NOW).items}
    assert by_id[first.id].name == "American Express (Platinum Card ··1005)"
    assert by_id[second.id].name == "American Express (Blue Cash ··2002)"
