"""Linking (ARCHITECTURE §3, §7; SYNC-1): Link tokens, the guarded exchange, update mode, re-attach,
the CSV attach choice and the Connection page's overview.

Production paths run with ``load_settings("production", <scratch paths>)``, the in-memory secret
store and ``FakePlaidGateway(plaid_env="production")``: nothing touches the real Keychain, Plaid or
the production database.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Account, PlaidItem, TransactionTombstone
from budgetapp.plaid_gateway import PlaidError
from budgetapp.services import linking, reconcile, userstate
from budgetapp.services.linking import LinkFailed, LinkMetadata, LinkRefused
from tests.factories import (
    get_category,
    make_account,
    make_item,
    make_sync_run,
    make_tombstone,
    make_transaction,
)
from tests.fake_plaid import FakePlaidGateway

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
AMEX = {"institution": {"institution_id": "ins_10", "name": "American Express"}}


@pytest.fixture(autouse=True)
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def production(tmp_path, monkeypatch):
    """Production settings whose state folder (recovery files) is a temp folder."""
    monkeypatch.setattr(app_secrets, "fallback_recovery_dir", lambda: tmp_path / "fallback")
    return load_settings(
        "production",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        install_dir=tmp_path / "venv",
    )


@pytest.fixture
def prod_plaid() -> FakePlaidGateway:
    return FakePlaidGateway(plaid_env="production")


@pytest.fixture
def sandbox(settings, tmp_path):
    return settings.replace(state_dir=tmp_path / "state")


def no_sleep(_seconds: float) -> None:
    return None


def items(db) -> list[PlaidItem]:
    db.expire_all()
    return list(db.scalars(select(PlaidItem).order_by(PlaidItem.id)))


# --------------------------------------------------------------------------- new-link tokens


def test_sandbox_link_tokens_need_no_guard(db, fake_plaid, settings):
    make_item(db)  # Sandbox may hold several Items
    token = linking.new_link_token(db, fake_plaid, settings)
    assert token.startswith("link-sandbox-")
    assert fake_plaid.calls_to("create_link_token") == [
        {"access_token": None, "additional_consented_products": ()}
    ]


def test_a_production_link_token_sets_the_link_in_progress_marker(
    db, production, prod_plaid, secrets
):
    assert linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert app_secrets.get_link_in_progress(store=secrets) == NOW + app_secrets.LINK_TOKEN_LIFETIME
    with pytest.raises(LinkRefused, match="link was started"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert len(prod_plaid.calls_to("create_link_token")) == 1


def test_production_never_offers_a_second_link(db, production, prod_plaid, secrets):
    make_item(db)
    with pytest.raises(LinkRefused, match="already linked"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []
    assert app_secrets.get_link_in_progress(store=secrets) is None


@pytest.mark.parametrize("trace", ["token", "marker", "recovery file"])
def test_production_refuses_while_any_trace_of_a_link_exists(
    db, production, prod_plaid, secrets, trace
):
    if trace == "token":
        secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, "access-production-existing")
    elif trace == "marker":
        app_secrets.set_link_in_progress(store=secrets)
    else:
        app_secrets.write_access_token_recovery_file(production, "item-x", "access-production-lost")
    with pytest.raises(LinkRefused):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []


def test_a_failed_token_request_clears_the_marker(db, production, prod_plaid, secrets):
    prod_plaid.queue_error("create_link_token", prod_plaid.plaid_error("INVALID_API_KEYS"))
    with pytest.raises(PlaidError):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert app_secrets.get_link_in_progress(store=secrets) is None


def test_link_exit_clears_a_production_marker_only(production, settings, secrets, caplog):
    caplog.set_level(logging.INFO, logger="budgetapp.services.linking")
    app_secrets.set_link_in_progress(store=secrets)
    linking.link_exited(settings, store=secrets, event="CLOSE_OAUTH")
    assert app_secrets.get_link_in_progress(store=secrets) is not None  # Sandbox: nothing to clear
    linking.link_exited(production, store=secrets, mode="update")
    assert app_secrets.get_link_in_progress(store=secrets) is not None  # update mode: no marker
    linking.link_exited(production, store=secrets, event="FAIL_OAUTH", error_code="X")
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert "closed without linking" in caplog.text


# --------------------------------------------------------------------------- exchange


def test_exchange_saves_the_token_then_the_item_and_accounts(
    db, session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(
        item_id="new-item", consent_expiration_time="2027-10-20T00:00:00Z"
    )
    fake_plaid.set_balances(fake_item, fake_item.default_account_id, current="321.09", limit=8000)
    public = fake_plaid.public_token_for(fake_item)
    result = linking.exchange(session_factory, fake_plaid, secrets, sandbox, public, AMEX)
    assert (result.plaid_item_id, result.institution_name, result.account_count) == (
        "new-item",
        "American Express",
        1,
    )
    assert result.created and not result.attach_needed
    assert (
        secrets.values[app_secrets.sandbox_access_token_key("new-item")] == fake_item.access_token
    )
    assert app_secrets.sandbox_item_ids(store=secrets) == ["new-item"]
    (item,) = items(db)
    assert (item.status, item.linked_at, item.last_consented_at, item.days_requested) == (
        "healthy",
        NOW,
        NOW,
        730,
    )
    assert item.consent_expires_at == dt.datetime(2027, 10, 20, tzinfo=dt.UTC)
    assert item.consented_products == ["liabilities", "transactions"]
    assert item.transactions_cursor is None
    account = db.scalars(select(Account)).one()
    assert (account.plaid_account_id, account.current_balance, account.credit_limit) == (
        fake_item.default_account_id,
        Decimal("321.09"),
        Decimal("8000.00"),
    )


def test_exchange_falls_back_to_links_account_list(
    db, session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(item_id="it")
    fake_plaid.queue_error("get_accounts", fake_plaid.plaid_error("INTERNAL_SERVER_ERROR"))
    metadata = {
        **AMEX,
        "accounts": [
            {
                "id": "acc-x",
                "name": "Gold Card",
                "mask": "1234",
                "type": "credit",
                "subtype": "credit card",
            }
        ],
        "link_session_id": "session-1",
    }
    linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        sandbox,
        fake_plaid.public_token_for(fake_item),
        metadata,
    )
    account = db.scalars(select(Account)).one()
    assert (account.plaid_account_id, account.name, account.mask) == ("acc-x", "Gold Card", "1234")


def test_transport_failures_are_retried_within_the_public_tokens_life(
    session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(item_id="it")
    for _ in range(2):
        fake_plaid.queue_error("exchange_public_token", fake_plaid.transport_error())
    slept: list[float] = []
    result = linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        sandbox,
        fake_plaid.public_token_for(fake_item),
        AMEX,
        sleep=slept.append,
        monotonic=lambda: 0.0,
    )
    assert result.plaid_item_id == "it" and slept == [2.0, 4.0]
    assert len(fake_plaid.calls_to("exchange_public_token")) == 3


def test_retries_stop_when_the_public_token_expires(
    db, session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(item_id="it")
    for _ in range(10):
        fake_plaid.queue_error("exchange_public_token", fake_plaid.transport_error())
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    with pytest.raises(LinkFailed, match="expired"):
        linking.exchange(
            session_factory,
            fake_plaid,
            secrets,
            sandbox,
            fake_plaid.public_token_for(fake_item),
            AMEX,
            sleep=sleep,
            monotonic=lambda: clock["t"],
            window=10.0,
        )
    assert len(fake_plaid.calls_to("exchange_public_token")) == 3 and items(db) == []


def test_the_trial_connection_limit_is_shown_plainly_and_never_retried(
    db, session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(item_id="it")
    fake_plaid.queue_error(
        "exchange_public_token", fake_plaid.plaid_error("TRIAL_CONNECTION_LIMIT")
    )
    with pytest.raises(LinkFailed, match="all 10 connections"):
        linking.exchange(
            session_factory,
            fake_plaid,
            secrets,
            sandbox,
            fake_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert len(fake_plaid.calls_to("exchange_public_token")) == 1 and items(db) == []


def test_a_keychain_failure_stops_before_the_database_and_keeps_the_token_safe(
    db, session_factory, fake_plaid, secrets, sandbox, tmp_path
):
    fake_item = fake_plaid.add_item(item_id="lost")
    secrets.fail_on[("set", app_secrets.sandbox_access_token_key("lost"))] = (
        app_secrets.SecretStoreError("denied")
    )
    with pytest.raises(LinkFailed) as info:
        linking.exchange(
            session_factory,
            fake_plaid,
            secrets,
            sandbox,
            fake_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert fake_item.access_token not in str(info.value)
    assert "--access-token" in str(info.value)
    assert items(db) == []
    (recovery,) = (tmp_path / "state" / "recovery").glob("*.txt")
    assert fake_item.access_token in recovery.read_text()


def test_a_public_token_used_twice_is_refused_by_plaid(
    session_factory, fake_plaid, secrets, sandbox
):
    fake_item = fake_plaid.add_item(item_id="it")
    public = fake_plaid.public_token_for(fake_item)
    linking.exchange(session_factory, fake_plaid, secrets, sandbox, public, AMEX)
    with pytest.raises(LinkFailed, match="Nothing was saved"):
        linking.exchange(session_factory, fake_plaid, secrets, sandbox, public, AMEX)


def test_saving_the_same_item_twice_completes_the_existing_row(db, session_factory):
    meta = LinkMetadata("ins_10", "American Express")
    first = linking.save_item(session_factory, "dup", meta, None, None)
    second = linking.save_item(session_factory, "dup", meta, None, None)
    assert first[0] == second[0] and first[1] and not second[1]
    assert len(items(db)) == 1


def test_production_never_exchanges_while_an_item_exists(
    db, session_factory, production, prod_plaid, secrets
):
    make_item(db, institution_id="ins_10")
    app_secrets.set_link_in_progress(store=secrets)
    fake_item = prod_plaid.add_item(item_id="second")
    with pytest.raises(LinkRefused, match="already linked"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert len(items(db)) == 1


def test_a_production_link_stores_the_one_token_and_clears_the_marker(
    db, session_factory, production, prod_plaid, secrets
):
    app_secrets.set_link_in_progress(store=secrets)
    fake_item = prod_plaid.add_item(item_id="the-amex")
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(fake_item),
        AMEX,
    )
    assert result.plaid_item_id == "the-amex"
    assert secrets.values[app_secrets.PRODUCTION_ACCESS_TOKEN] == fake_item.access_token
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert app_secrets.production_link_blockers(production, store=secrets)  # never a second link


def test_a_different_production_token_is_never_replaced(
    db, session_factory, production, prod_plaid, secrets
):
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, "access-production-older")
    fake_item = prod_plaid.add_item(item_id="new")
    with pytest.raises(LinkFailed, match="Re-attach"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert secrets.values[app_secrets.PRODUCTION_ACCESS_TOKEN] == "access-production-older"
    assert items(db) == []
    assert app_secrets.unsaved_production_tokens(production)  # the new token is in a recovery file


def test_a_production_exchange_that_plaid_refuses_clears_the_marker(
    session_factory, production, prod_plaid, secrets
):
    app_secrets.set_link_in_progress(store=secrets)
    fake_item = prod_plaid.add_item(item_id="x")
    prod_plaid.queue_error(
        "exchange_public_token", prod_plaid.plaid_error("TRIAL_CONNECTION_LIMIT")
    )
    with pytest.raises(LinkFailed):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert app_secrets.get_link_in_progress(store=secrets) is None


def test_an_exchange_with_an_unknown_outcome_keeps_the_marker(
    session_factory, production, prod_plaid, secrets
):
    app_secrets.set_link_in_progress(store=secrets)
    fake_item = prod_plaid.add_item(item_id="x")
    prod_plaid.queue_error("exchange_public_token", prod_plaid.transport_error())
    prod_plaid.queue_error("exchange_public_token", prod_plaid.plaid_error("INVALID_PUBLIC_TOKEN"))
    with pytest.raises(LinkFailed):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
            sleep=no_sleep,
        )
    # the first attempt may have reached Plaid: keep blocking new links until the marker expires
    assert app_secrets.get_link_in_progress(store=secrets) is not None


def test_link_metadata_ignores_anything_unexpected():
    meta = LinkMetadata.from_json(
        {
            "institution": {"institution_id": " ins_10 ", "name": "American Express"},
            "accounts": [{"id": "a", "mask": 12}, "junk", {"name": "no id"}],
            "public_token": "public-sandbox-should-not-be-kept",
        }
    )
    assert meta.institution_id == "ins_10" and meta.accounts == (linking.LinkAccount("a"),)
    assert LinkMetadata.from_json("nonsense") == LinkMetadata()


def test_an_empty_public_token_is_refused(session_factory, fake_plaid, secrets, sandbox):
    with pytest.raises(LinkRefused):
        linking.exchange(session_factory, fake_plaid, secrets, sandbox, "", AMEX)


# --------------------------------------------------------------------------- manual CSV accounts


@pytest.fixture
def attach_calls(monkeypatch):
    calls: list[tuple[int, int]] = []

    def attach(session, manual_id, plaid_account_id):
        calls.append((manual_id, plaid_account_id))
        return reconcile.AttachResult(moved_transactions=3)

    monkeypatch.setattr(reconcile, "attach_manual_account", attach)
    return calls


def test_a_lone_manual_account_is_attached_to_the_lone_card(
    db, session_factory, fake_plaid, secrets, sandbox, attach_calls
):
    manual = make_account(db, manual=True)
    fake_item = fake_plaid.add_item(item_id="it")
    result = linking.exchange(
        session_factory, fake_plaid, secrets, sandbox, fake_plaid.public_token_for(fake_item), AMEX
    )
    card = db.scalars(select(Account).where(Account.item_id.is_not(None))).one()
    assert attach_calls == [(manual.id, card.id)]
    assert result.attached == reconcile.AttachResult(moved_transactions=3)
    assert not result.attach_needed


def test_several_manual_accounts_are_left_for_the_user_to_choose(
    db, session_factory, fake_plaid, secrets, sandbox, attach_calls
):
    make_account(db, manual=True)
    make_account(db, manual=True)
    fake_item = fake_plaid.add_item(item_id="it")
    result = linking.exchange(
        session_factory, fake_plaid, secrets, sandbox, fake_plaid.public_token_for(fake_item), AMEX
    )
    assert attach_calls == [] and result.attach_needed and result.attached is None


def test_until_csv_attaching_exists_the_link_still_succeeds(
    db, session_factory, fake_plaid, secrets, sandbox
):
    make_account(db, manual=True)
    fake_item = fake_plaid.add_item(item_id="it")
    result = linking.exchange(
        session_factory, fake_plaid, secrets, sandbox, fake_plaid.public_token_for(fake_item), AMEX
    )
    assert result.attach_needed and len(items(db)) == 1


def test_attach_manual_checks_both_accounts(db, attach_calls):
    manual = make_account(db, manual=True)
    card = make_account(db)
    assert linking.attach_manual(db, manual.id, card.id).moved_transactions == 3
    with pytest.raises(LinkRefused):
        linking.attach_manual(db, card.id, card.id)
    with pytest.raises(LinkRefused):
        linking.attach_manual(db, manual.id, manual.id)


def test_the_attach_choice_lists_imported_accounts_and_linked_cards(db):
    assert linking.attach_choice(db) is None
    manual = make_account(db, manual=True, name="Amex CSV", mask=None)
    assert linking.attach_choice(db) is None  # nothing linked yet
    card = make_account(db)
    make_transaction(db, manual, source="csv")
    make_transaction(db, manual, source="csv")
    choice = linking.attach_choice(db)
    assert choice.automatic
    assert choice.manual == (
        linking.AccountOption(manual.id, "Amex CSV", "2 imported transactions"),
    )
    assert choice.linked == (linking.AccountOption(card.id, card.display_name),)


# --------------------------------------------------------------------------- update mode


def linked_item(db, fake_plaid, secrets, settings, **values):
    fake_item = fake_plaid.add_item(item_id=values.pop("plaid_item_id", "upd"))
    app_secrets.store_access_token(
        settings, fake_item.item_id, fake_item.access_token, store=secrets
    )
    return fake_item, make_item(db, plaid_item_id=fake_item.item_id, **values)


def test_update_mode_tokens_reuse_the_items_access_token(db, fake_plaid, secrets, settings):
    fake_item, item = linked_item(db, fake_plaid, secrets, settings)
    linking.update_link_token(db, fake_plaid, item, store=secrets, settings=settings)
    linking.update_link_token(
        db, fake_plaid, item.id, add_liabilities=True, store=secrets, settings=settings
    )
    assert fake_plaid.calls_to("create_link_token") == [
        {"access_token": fake_item.access_token, "additional_consented_products": ()},
        {"access_token": fake_item.access_token, "additional_consented_products": ("liabilities",)},
    ]
    with pytest.raises(LinkRefused):
        linking.update_link_token(db, fake_plaid, 999999, store=secrets, settings=settings)


def test_completing_update_mode_marks_the_item_healthy_and_reads_consent(
    db, session_factory, fake_plaid, secrets, settings
):
    fake_item, item = linked_item(
        db,
        fake_plaid,
        secrets,
        settings,
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
        error_message="expired",
        error_display_message="Sign in again",
        last_consented_at=NOW - dt.timedelta(days=360),
        consented_products=["transactions"],
        liabilities_status="not_consented",
    )
    fake_item.consent_expiration_time = "2027-10-20T00:00:00Z"
    result = linking.complete_update(
        session_factory, fake_plaid, secrets, settings, item.id, metadata={"link_session_id": "s"}
    )
    assert result == linking.UpdateResult(
        item.id, "healthy", dt.datetime(2027, 10, 20, tzinfo=dt.UTC), True
    )
    db.expire_all()
    row = db.get(PlaidItem, item.id)
    assert (row.status, row.error_code, row.error_display_message) == ("healthy", None, None)
    assert row.last_consented_at == NOW and row.liabilities_status is None
    assert fake_plaid.calls_to("exchange_public_token") == []  # update mode never exchanges


def test_update_mode_on_a_vanished_item_is_refused(session_factory, fake_plaid, secrets, settings):
    with pytest.raises(LinkRefused):
        linking.complete_update(session_factory, fake_plaid, secrets, settings, 999999)


# --------------------------------------------------------------------------- re-attach


def test_orphaned_sandbox_items_are_recreated_from_plaid(
    db, session_factory, fake_plaid, secrets, settings
):
    fake_item = fake_plaid.add_item(item_id="orphan")
    app_secrets.store_access_token(settings, "orphan", fake_item.access_token, store=secrets)
    assert linking.orphaned_sandbox_items(db, store=secrets) == ["orphan"]
    (result,) = linking.recover_orphaned_items(session_factory, fake_plaid, secrets, settings)
    assert (result.plaid_item_id, result.account_count, result.institution_name) == (
        "orphan",
        1,
        "American Express",
    )
    assert linking.orphaned_sandbox_items(db, store=secrets) == []
    assert linking.recover_orphaned_items(session_factory, fake_plaid, secrets, settings) == []


def test_production_re_attaches_the_saved_token_never_a_new_link(
    db, session_factory, production, prod_plaid, secrets
):
    fake_item = prod_plaid.add_item(item_id="restored")
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, fake_item.access_token)
    assert linking.reattach_available(db, production, store=secrets)
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert result.plaid_item_id == "restored" and result.created
    assert not linking.reattach_available(db, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production) == []


def test_re_attaching_a_token_plaid_rejects_changes_nothing(
    db, session_factory, production, prod_plaid, secrets
):
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, "access-production-dead")
    with pytest.raises(LinkFailed, match="INVALID_ACCESS_TOKEN"):
        linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert items(db) == []


# --------------------------------------------------------------------------- Sandbox test Items


def test_a_sandbox_item_is_created_without_link_through_the_same_exchange(
    db, session_factory, fake_plaid, secrets, sandbox
):
    result = linking.sandbox_create_item(
        session_factory,
        fake_plaid,
        secrets,
        sandbox,
        username="user_transactions_dynamic",
        password="x",
    )
    (call,) = fake_plaid.calls_to("sandbox_create_public_token")
    assert (
        call["institution_id"] == "ins_109508"
        and call["override_username"] == "user_transactions_dynamic"
    )
    assert call["initial_products"] == ("transactions", "liabilities")
    assert result.institution_name == "First Platypus Bank"
    assert len(fake_plaid.calls_to("exchange_public_token")) == 1


def test_sandbox_test_items_are_refused_in_production(
    session_factory, production, prod_plaid, secrets
):
    with pytest.raises(LinkRefused):
        linking.sandbox_create_item(session_factory, prod_plaid, secrets, production)
    assert prod_plaid.calls == []


# --------------------------------------------------------------------------- pre-flight


def test_preflight_finds_american_express_with_liabilities(fake_plaid, production):
    result = linking.preflight(fake_plaid, production)
    assert [c.key for c in result.checklist] == ["use_case", "profiles", "amex_oauth"]
    assert all(c.href.startswith("https://dashboard.plaid.com/") for c in result.checklist)
    assert result.institution.status == "ok" and result.institution.oauth is True
    assert fake_plaid.calls_to("search_institutions") == [
        {"query": "American Express", "products": ("liabilities",)}
    ]


def test_preflight_reports_a_missing_institution_or_a_failed_search(fake_plaid, production):
    fake_plaid.institutions = [
        i for i in fake_plaid.institutions if i["institution_id"] != "ins_10"
    ]
    assert linking.preflight(fake_plaid, production).institution.status == "missing"
    fake_plaid.queue_error("search_institutions", fake_plaid.transport_error())
    failed = linking.preflight(fake_plaid, production).institution
    assert failed.status == "error" and "optional" in failed.message


# --------------------------------------------------------------------------- lost edits


def test_lost_edits_are_listed_until_dismissed(db):
    account = make_account(db)
    groceries = get_category(db, "Groceries")
    state = userstate.UserState(
        category_id=groceries.id,
        notes="Party supplies",
        tags=("party",),
        splits=(
            userstate.SplitState(groceries.id, Decimal("10.00")),
            userstate.SplitState(get_category(db, "Home").id, Decimal("5.00")),
        ),
        source_amount=Decimal("15.00"),
    )
    lost = make_tombstone(
        db,
        account,
        plaid_transaction_id="lost-1",
        amount=Decimal("15.00"),
        removed_at=NOW - dt.timedelta(days=2),
        user_state=userstate.to_json(state),
    )
    make_tombstone(db, account, plaid_transaction_id="plain", user_state=None)
    carrier = make_transaction(db, account)
    make_tombstone(
        db,
        account,
        plaid_transaction_id="carried",
        user_state=userstate.to_json(userstate.UserState(notes="x")),
        consumed_by_transaction_id=carrier.id,
    )
    (entry,) = linking.lost_edits(db)
    assert entry.plaid_transaction_id == "lost-1"
    assert (entry.category_name, entry.notes, entry.tags, entry.split_count) == (
        "Groceries",
        "Party supplies",
        ("party",),
        2,
    )
    assert entry.expires_on == (lost.removed_at + dt.timedelta(days=90)).astimezone().date()
    assert linking.dismiss_lost_edit(db, "lost-1")
    assert linking.lost_edits(db) == []
    assert db.get(TransactionTombstone, "lost-1") is not None  # still blocks the removed id
    assert not linking.dismiss_lost_edit(db, "lost-1")
    assert not linking.dismiss_lost_edit(db, "carried")
    assert not linking.dismiss_lost_edit(db, "unknown")


# --------------------------------------------------------------------------- overview


def test_the_overview_of_an_empty_sandbox(db, settings, secrets):
    overview = linking.connection_overview(db, settings, store=secrets)
    assert not overview.linked and overview.can_link and overview.link_actions
    assert overview.status.state == "not_linked" and overview.runs == ()
    assert not overview.production and overview.reminder_days == 30


def test_the_overview_before_a_production_link(db, production, secrets):
    overview = linking.connection_overview(db, production, store=secrets)
    assert overview.production and overview.can_link and overview.link_blockers == ()
    app_secrets.set_link_in_progress(store=secrets)
    blocked = linking.connection_overview(db, production, store=secrets)
    assert not blocked.can_link and len(blocked.link_blockers) == 1
    app_secrets.clear_link_in_progress(store=secrets)
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, "access-production-kept")
    reattach = linking.connection_overview(db, production, store=secrets)
    assert reattach.reattach_available and not reattach.can_link


def test_the_overview_of_a_linked_item(db, settings, secrets):
    item = make_item(
        db,
        consented_products=["transactions"],
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
    )
    card = make_account(db, item, current_balance=Decimal("10.00"))
    for n in range(25):
        make_sync_run(db, item, started_at=NOW - dt.timedelta(hours=n))
    overview = linking.connection_overview(db, settings, store=secrets)
    (view,) = overview.items
    assert view.needs_reconnect and not view.liabilities_consented
    assert "sign in again" in view.error_help
    assert [a.id for a in view.accounts] == [card.id]
    assert len(overview.runs) == 20 and overview.runs[0].started_at == NOW
    assert overview.link_actions and overview.attach is None
    assert db.scalar(select(func.count()).select_from(PlaidItem)) == 1
