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
from sqlalchemy.exc import OperationalError

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Account, PlaidItem, TransactionTombstone
from budgetapp.plaid_gateway import PlaidError
from budgetapp.services import linking, reconcile, userstate
from budgetapp.services import status as status_service
from budgetapp.services import sync as sync_service
from budgetapp.services.linking import LinkFailed, LinkMetadata, LinkRefused
from tests.bank_factories import make_bank_account
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
TARTAN = {"institution": {"institution_id": "ins_109511", "name": "Tartan Bank"}}
LEGACY = app_secrets.LEGACY_PRODUCTION_TOKEN


def prod_key(item_id: str) -> str:
    """Where a production Item's token lives (C6)."""
    return app_secrets.production_access_token_key(item_id)


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
    link = linking.new_link_token(db, fake_plaid, settings)
    assert link.token.startswith("link-sandbox-") and link.attempt is None
    assert fake_plaid.calls_to("create_link_token") == [
        {"access_token": None, "additional_consented_products": ()}
    ]


def test_a_production_link_token_sets_the_link_in_progress_marker(
    db, production, prod_plaid, secrets
):
    link = linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert link.token.startswith("link-production-") and link.attempt
    assert app_secrets.get_link_in_progress(store=secrets) == NOW + app_secrets.LINK_TOKEN_LIFETIME
    with pytest.raises(LinkRefused, match="may still be in progress"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert len(prod_plaid.calls_to("create_link_token")) == 1


def test_production_offers_another_link_while_connections_exist(
    db, production, prod_plaid, secrets
):
    """BANK-1: any bank can be linked, so connections that exist never block a new link (the
    duplicate check happens when Plaid's window closes, before the exchange)."""
    amex = make_item(db, plaid_item_id="item-amex")
    app_secrets.store_access_token(production, "item-amex", "access-production-a", store=secrets)
    link = linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert link.attempt and len(prod_plaid.calls_to("create_link_token")) == 1
    assert linking.connections_used(db, production, store=secrets) == 1
    assert amex.id


@pytest.mark.parametrize("trace", ["v1 token", "orphan", "marker", "recovery file"])
def test_production_refuses_while_any_trace_of_a_link_exists(
    db, production, prod_plaid, secrets, trace
):
    if trace == "v1 token":  # a v1 token and no Item row: whose is it? Re-attach first
        secrets.set(LEGACY, "access-production-existing")
    elif trace == "orphan":  # a token of its own whose Item row is gone
        app_secrets.store_access_token(production, "lost", "access-production-l", store=secrets)
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


def test_only_the_attempts_own_link_exit_clears_the_production_marker(
    db, production, settings, prod_plaid, secrets, caplog
):
    caplog.set_level(logging.INFO, logger="budgetapp.services.linking")
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    assert not linking.link_exited(db, settings, store=secrets, attempt=attempt)  # Sandbox
    assert not linking.link_exited(db, production, store=secrets, mode="update", attempt=attempt)
    for event in ("CLOSE_OAUTH", "FAIL_OAUTH"):  # events: Link is still open after them
        assert not linking.link_exited(db, production, store=secrets, event=event, attempt=attempt)
    assert not linking.link_exited(db, production, store=secrets, event="EXIT")  # whose exit?
    assert not linking.link_exited(db, production, store=secrets, event="EXIT", attempt="other")
    assert app_secrets.get_link_in_progress(store=secrets) is not None
    assert linking.link_exited(
        db, production, store=secrets, event="EXIT", attempt=attempt, error_code="X"
    )
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert "closed without linking" in caplog.text and "reported an event" in caplog.text


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
        linking.exchange(
            session_factory, fake_plaid, secrets, sandbox, public, AMEX, duplicate_check=False
        )


def test_saving_the_same_item_twice_completes_the_existing_row(db, session_factory):
    meta = LinkMetadata("ins_10", "American Express")
    first = linking.save_item(session_factory, "dup", meta, None, None)
    second = linking.save_item(session_factory, "dup", meta, None, None)
    assert first[0] == second[0] and first[1] and not second[1]
    assert len(items(db)) == 1


def test_production_never_exchanges_a_bank_that_is_already_linked(
    db, session_factory, production, prod_plaid, secrets
):
    """BANK-3: the same institution again is refused before the exchange (nothing is used),
    and this attempt's marker is cleared."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    make_item(db, institution_id="ins_10")  # e.g. linked in another tab meanwhile
    fake_item = prod_plaid.add_item(item_id="second")
    with pytest.raises(LinkRefused, match="American Express is already linked"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
            attempt=attempt,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert app_secrets.get_link_in_progress(store=secrets) is None  # this attempt is over
    assert len(items(db)) == 1


def test_a_production_link_stores_the_one_token_and_clears_the_marker(
    db, session_factory, production, prod_plaid, secrets
):
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    fake_item = prod_plaid.add_item(item_id="the-amex")
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(fake_item),
        AMEX,
        attempt=attempt,
    )
    assert result.plaid_item_id == "the-amex"
    assert secrets.values[prod_key("the-amex")] == fake_item.access_token  # C6: its own key
    assert LEGACY not in secrets.values
    assert app_secrets.production_item_ids(store=secrets) == ["the-amex"]
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert linking.link_blocks(db, production, store=secrets) == ()  # another bank may follow
    assert linking.connections_used(db, production, store=secrets) == 1


def test_a_different_production_token_is_never_replaced(
    db, session_factory, production, prod_plaid, secrets
):
    """The backstop behind the pre-exchange checks: a token that reached this Item's Keychain key
    while the exchange ran (say ./run setup --access-token in Terminal) is kept, the new one is
    parked."""
    fake_item = prod_plaid.add_item(item_id="new")
    real = prod_plaid.exchange_public_token

    def exchange_while_setup_runs(public_token):
        secrets.set(prod_key("new"), "access-production-older")
        return real(public_token)

    prod_plaid.exchange_public_token = exchange_while_setup_runs  # type: ignore[method-assign]
    with pytest.raises(LinkFailed, match="Re-attach"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert secrets.values[prod_key("new")] == "access-production-older"
    assert items(db) == []
    assert app_secrets.unsaved_production_tokens(production)  # the new token is in a recovery file


def test_a_production_exchange_that_plaid_refuses_clears_the_marker(
    db, session_factory, production, prod_plaid, secrets
):
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
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
            attempt=attempt,
        )
    assert app_secrets.get_link_in_progress(store=secrets) is None


def test_an_exchange_with_an_unknown_outcome_keeps_the_marker(
    db, session_factory, production, prod_plaid, secrets
):
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
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
            attempt=attempt,
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


def test_linking_moves_the_lone_csv_history_onto_the_new_card(
    db, session_factory, fake_plaid, secrets, sandbox
):
    """CSV's real ``reconcile.attach_manual_account`` runs after linking (§6)."""
    manual = make_account(db, manual=True)
    rows = [
        make_transaction(db, manual, source="csv", amount=amount, notes="From the CSV")
        for amount in ("12.00", "30.50")
    ]
    manual_id = manual.id
    fake_item = fake_plaid.add_item(item_id="it")
    result = linking.exchange(
        session_factory, fake_plaid, secrets, sandbox, fake_plaid.public_token_for(fake_item), AMEX
    )
    db.expire_all()
    card = db.scalars(select(Account).where(Account.item_id.is_not(None))).one()
    assert not result.attach_needed and result.attached is not None
    assert result.attached.moved_transactions == 2
    assert db.get(Account, manual_id) is None  # the manual account is gone
    assert {(t.account_id, t.notes) for t in rows} == {(card.id, "From the CSV")}
    assert len(items(db)) == 1


@pytest.mark.parametrize(
    "failure",
    [reconcile.AttachError("Choose a card linked through Plaid."), RuntimeError("a bug")],
    ids=["refused", "failed"],
)
def test_an_attach_that_fails_never_fails_the_link(
    db, session_factory, fake_plaid, secrets, sandbox, monkeypatch, failure
):
    def attach(session, manual_id, plaid_account_id):
        session.execute(
            Account.__table__.update().where(Account.id == manual_id).values(name="half-moved")
        )
        raise failure

    monkeypatch.setattr(reconcile, "attach_manual_account", attach)
    manual = make_account(db, manual=True)
    fake_item = fake_plaid.add_item(item_id="it")
    result = linking.exchange(
        session_factory, fake_plaid, secrets, sandbox, fake_plaid.public_token_for(fake_item), AMEX
    )
    assert result.attach_needed and result.attached is None and len(items(db)) == 1
    db.expire_all()
    assert db.get(Account, manual.id).name == "American Express (CSV)"  # rolled back


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
    make_account(db, item)  # a card: statement details apply
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
        item.id, "healthy", dt.datetime(2027, 10, 20, tzinfo=dt.UTC), True, "American Express"
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


def test_production_re_attaches_the_saved_v1_token_never_a_new_link(
    db, session_factory, production, prod_plaid, secrets
):
    """A lost database with the v1 token in Keychain: /item/get names the Item, its row is
    rebuilt and the token moves to the Item's own key (C6)."""
    fake_item = prod_plaid.add_item(item_id="restored")
    secrets.set(LEGACY, fake_item.access_token)
    assert linking.reattach_available(db, production, store=secrets)
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert result.plaid_item_id == "restored" and result.created
    assert secrets.values[prod_key("restored")] == fake_item.access_token
    assert LEGACY not in secrets.values
    assert not linking.reattach_available(db, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production) == []


def test_re_attaching_a_token_plaid_rejects_changes_nothing(
    db, session_factory, production, prod_plaid, secrets
):
    secrets.set(LEGACY, "access-production-dead")
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


def test_preflight_checks_that_plaid_answers_without_singling_out_a_bank(fake_plaid, production):
    """BANK-2: the checklist and the live check are no longer about American Express."""
    result = linking.preflight(fake_plaid, production)
    assert [c.key for c in result.checklist] == ["use_case", "oauth"]
    assert all(c.href.startswith("https://dashboard.plaid.com/") for c in result.checklist)
    assert "American Express" not in " ".join(c.label + c.detail for c in result.checklist)
    assert "no action is needed" in result.checklist[1].detail
    assert result.institution.status == "ok"
    assert "Production keys" in result.institution.message
    assert fake_plaid.calls_to("search_institutions") == [
        {"query": "Bank", "products": ("transactions",)}
    ]


def test_preflight_reports_an_empty_answer_or_a_failed_search(fake_plaid, production):
    fake_plaid.institutions = []
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
    secrets.set(LEGACY, "access-production-kept")
    reattach = linking.connection_overview(db, production, store=secrets)
    assert reattach.reattach_available and not reattach.can_link
    assert reattach.connections_used == 1 and reattach.connections_left == 9


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


# --------------------------------------------------------------------------- review fixes
# One link at a time (Principle 4): the marker belongs to the attempt that set it, the OAuth
# events never clear it, and the exchange itself re-checks every trace of a connection.


def test_close_oauth_keeps_the_guard_closed_while_link_can_still_finish(
    db, production, prod_plaid, secrets
):
    """CLOSE_OAUTH / FAIL_OAUTH are Link events: the Amex window closed, Link is still open and
    the first token can still finish, so a second tab must not get a new-link token."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    for event in ("CLOSE_OAUTH", "FAIL_OAUTH"):
        linking.link_exited(db, production, store=secrets, event=event, attempt=attempt)
    with pytest.raises(LinkRefused, match="may still be in progress"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert len(prod_plaid.calls_to("create_link_token")) == 1


def test_a_late_exit_of_an_older_attempt_leaves_the_newer_marker(
    db, production, prod_plaid, secrets, freeze_today
):
    """Tab A's marker expired (Link left open over 4 hours); tab B links; then A's Link is closed:
    A's onExit must not clear B's marker while B's Link is open."""
    first = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    freeze_today(now=NOW + app_secrets.LINK_TOKEN_LIFETIME + dt.timedelta(minutes=1))
    second = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    assert first != second
    assert not linking.link_exited(db, production, store=secrets, event="EXIT", attempt=first)
    with pytest.raises(LinkRefused):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert len(prod_plaid.calls_to("create_link_token")) == 2
    assert linking.link_exited(db, production, store=secrets, event="EXIT", attempt=second)


@pytest.mark.parametrize("trace", ["keychain token", "v1 token", "recovery file"])
def test_exchange_is_refused_before_plaid_while_any_trace_of_a_connection_exists(
    db, session_factory, production, prod_plaid, secrets, trace
):
    """A production token in Keychain without an Item row (a crash between the Keychain write and
    the commit, or a restored database), or one waiting in a recovery file, IS a connection the
    database doesn't show: its bank is unknown, so another link could duplicate it. Re-attach or
    store it first."""
    if trace == "keychain token":
        app_secrets.store_access_token(production, "orphan", "access-production-o", store=secrets)
        expected = "doesn't show"
    elif trace == "v1 token":
        secrets.set(LEGACY, "access-production-orphan")
        expected = "doesn't show"
    else:
        app_secrets.write_access_token_recovery_file(production, "item-x", "access-production-x")
        expected = "recovery file"
    fake_item = prod_plaid.add_item(item_id="second")
    with pytest.raises(LinkRefused, match=expected):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert items(db) == []
    if trace != "recovery file":
        assert app_secrets.unsaved_production_tokens(production) == []


def test_a_second_exchange_while_the_first_runs_is_refused(
    db, session_factory, production, prod_plaid, secrets
):
    """Two onSuccess at once (two tabs): only one exchange runs; the other is refused before it
    reaches Plaid, instead of passing the same checks."""
    first = prod_plaid.add_item(item_id="first")
    second = prod_plaid.add_item(item_id="second")
    public_second = prod_plaid.public_token_for(second)
    real = prod_plaid.exchange_public_token
    nested: dict[str, BaseException] = {}

    def exchange_public_token(public_token: str):
        if not nested:
            try:
                linking.exchange(
                    session_factory, prod_plaid, secrets, production, public_second, AMEX
                )
            except LinkRefused as exc:
                nested["refused"] = exc
        return real(public_token)

    prod_plaid.exchange_public_token = exchange_public_token  # type: ignore[method-assign]
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(first),
        AMEX,
    )
    assert isinstance(nested["refused"], linking.LinkBusy)
    assert result.plaid_item_id == "first"
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1
    assert [i.plaid_item_id for i in items(db)] == ["first"]


def test_an_exchange_keeps_new_links_out_while_it_runs(
    db, session_factory, production, prod_plaid, secrets
):
    """Even with no marker left (Link stayed open past its 4 hours), the exchange holds one while
    it talks to Plaid, so no other tab can start a link meanwhile; it is cleared once the token
    is safe."""
    fake_item = prod_plaid.add_item(item_id="amex")
    real = prod_plaid.exchange_public_token
    seen: dict[str, object] = {}

    def exchange_public_token(public_token: str):
        seen["marker"] = app_secrets.get_link_in_progress(store=secrets)
        try:
            linking.new_link_token(db, prod_plaid, production, store=secrets)
        except LinkRefused as exc:
            seen["refused"] = exc
        return real(public_token)

    prod_plaid.exchange_public_token = exchange_public_token  # type: ignore[method-assign]
    linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(fake_item),
        AMEX,
    )
    assert seen["marker"] is not None and "refused" in seen
    assert prod_plaid.calls_to("create_link_token") == []
    assert app_secrets.get_link_in_progress(store=secrets) is None


def test_a_crash_before_the_item_commit_never_lets_a_second_exchange_through(
    db, session_factory, production, prod_plaid, secrets, monkeypatch
):
    """The reviewer's chain: tab B's token reaches Keychain but its Item row isn't committed
    (database hiccup); tab A's Link then finishes: A's token must not be exchanged."""
    item_a = prod_plaid.add_item(item_id="item-a")
    item_b = prod_plaid.add_item(item_id="item-b")
    real_save = linking.save_item

    def flaky_save(*args, **kwargs):
        monkeypatch.setattr(linking, "save_item", real_save)
        raise OperationalError("INSERT ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr(linking, "save_item", flaky_save)
    with pytest.raises(OperationalError):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(item_b),
            AMEX,
        )
    assert secrets.values[prod_key("item-b")] == item_b.access_token
    assert items(db) == []
    with pytest.raises(LinkRefused, match="doesn't show"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(item_a),
            AMEX,
        )
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1  # one connection, not two
    assert linking.reattach_available(db, production, store=secrets)  # and the way back


def test_a_token_nothing_could_save_is_handed_back_once_and_never_in_the_message(
    db, session_factory, production, prod_plaid, secrets, monkeypatch
):
    """F1's TokenNotSavedError with no recovery file: the token exists only in this process, so
    the caller shows it once. The marker stays, so linking stays paused meanwhile."""
    fake_item = prod_plaid.add_item(item_id="amex")
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    secrets.fail_on[("set", prod_key("amex"))] = app_secrets.SecretStoreError("locked")

    def unwritable(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(app_secrets, "write_access_token_recovery_file", unwritable)
    with pytest.raises(linking.TokenNotSaved) as info:
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
            attempt=attempt,
        )
    message = str(info.value)
    assert info.value.access_token == fake_item.access_token
    assert fake_item.access_token not in message
    assert "password manager" in message and "--access-token" in message
    assert "show it to the user" not in message  # no developer instructions for the user
    assert app_secrets.get_link_in_progress(store=secrets) is not None
    assert items(db) == []


@pytest.mark.parametrize("failure", ["keychain refuses", "another token is saved there"])
def test_a_token_parked_in_a_recovery_file_frees_its_link_marker(
    db, session_factory, production, prod_plaid, secrets, failure
):
    """Final review (ops-4): the unsaved recovery file keeps new links away by itself, so the
    attempt's marker goes. It used to stay for 4 hours, so after the owner stored the token and
    re-attached it, the page still said a link "may still be in progress"."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    tartan = prod_plaid.add_item(
        item_id="item-tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    if failure == "keychain refuses":
        secrets.fail_on[("set", prod_key("item-tartan"))] = app_secrets.SecretStoreError("no")
    else:
        secrets.values[prod_key("item-tartan")] = "access-production-older"
    with pytest.raises(LinkFailed):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(tartan),
            link_meta(TARTAN["institution"], ("Checking", "0001")),
            attempt=attempt,
        )
    assert app_secrets.get_link_in_progress(store=secrets) is None
    (reason,) = linking.link_blocks(db, production, store=secrets)
    assert "recovery file" in reason and "may still be in progress" not in reason
    if failure == "keychain refuses":  # ./run setup --env production --access-token, Re-attach
        del secrets.fail_on[("set", prod_key("item-tartan"))]
        app_secrets.store_access_token(
            production, "item-tartan", tartan.access_token, store=secrets
        )
        assert app_secrets.mark_recovery_saved(production, tartan.access_token)
        linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
        assert linking.link_blocks(db, production, store=secrets) == ()


@pytest.mark.parametrize("odd", ["item.with.dot", "a b"])
def test_an_item_id_no_keychain_name_can_hold_ends_in_a_recovery_file(
    db, session_factory, production, prod_plaid, secrets, odd
):
    """Final review (security-1): after the exchange (a Trial connection is used) such an Item id
    escaped as a bare ValueError, leaving the token nowhere and nothing to count or block on."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    fake_item = prod_plaid.add_item(
        item_id=odd, institution_id="ins_109511", institution_name="Tartan Bank"
    )
    with pytest.raises(LinkFailed) as info:
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            TARTAN,
            attempt=attempt,
        )
    (path,) = app_secrets.unsaved_production_tokens(production)
    assert str(path) in str(info.value) and fake_item.access_token not in str(info.value)
    assert fake_item.access_token in path.read_text()
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1
    assert linking.connections_used(db, production, store=secrets) == 1
    assert any(str(path) in reason for reason in linking.link_blocks(db, production, store=secrets))
    assert items(db) == []


def test_a_sandbox_token_nothing_could_save_is_simply_lost(
    session_factory, fake_plaid, secrets, sandbox, monkeypatch
):
    fake_item = fake_plaid.add_item(item_id="lost")
    secrets.fail_on[("set", app_secrets.sandbox_access_token_key("lost"))] = (
        app_secrets.SecretStoreError("denied")
    )

    def unwritable(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(app_secrets, "write_access_token_recovery_file", unwritable)
    with pytest.raises(LinkFailed, match="Create another test Item") as info:
        linking.exchange(
            session_factory,
            fake_plaid,
            secrets,
            sandbox,
            fake_plaid.public_token_for(fake_item),
            AMEX,
        )
    assert not isinstance(info.value, linking.TokenNotSaved)


def test_an_expired_exchange_says_plainly_that_the_outcome_is_unknown(
    db, session_factory, production, prod_plaid, secrets
):
    """No access token was ever received, so nothing can be re-attached: say so, and when
    linking unlocks (local time)."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    fake_item = prod_plaid.add_item(item_id="amex")
    for _ in range(5):
        prod_plaid.queue_error("exchange_public_token", prod_plaid.transport_error())
    t = {"now": 0.0}

    def sleep(seconds):
        t["now"] += seconds

    with pytest.raises(LinkFailed) as info:
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
            attempt=attempt,
            sleep=sleep,
            monotonic=lambda: t["now"],
            window=10.0,
        )
    message = str(info.value)
    assert "re-attach" not in message and "may or may not" in message
    assert "dashboard.plaid.com" in message
    unlock = (NOW + app_secrets.LINK_TOKEN_LIFETIME).astimezone().strftime("%-I:%M %p")
    assert f"Linking stays paused until {unlock}" in message
    assert app_secrets.get_link_in_progress(store=secrets) is not None


def test_production_links_any_institution(db, session_factory, production, prod_plaid, secrets):
    """BANK-1: the v1 "only American Express" refusal is gone: a first link of Chase is
    exchanged like any other, and its marker is cleared."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    db.commit()
    other = prod_plaid.add_item(item_id="chase", institution_id="ins_3", institution_name="Chase")
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(other),
        {"institution": {"institution_id": "ins_3", "name": "Chase"}},
        attempt=attempt,
    )
    assert (result.plaid_item_id, result.institution_name) == ("chase", "Chase")
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1
    assert app_secrets.get_link_in_progress(store=secrets) is None


@pytest.mark.parametrize(
    "institution",
    [
        {"institution_id": "ins_10", "name": "American Express"},
        {"institution_id": "ins_999999", "name": "American Express - @ Work"},
        {"institution_id": "ins_999998", "name": "Amex"},
        {"institution_id": "ins_999997", "name": None},
        None,
    ],
    ids=["ins_10", "another-amex-entry", "amex-name", "no-name", "no-institution"],
)
def test_a_first_link_is_exchanged_whatever_link_says_about_its_bank(
    db, session_factory, production, prod_plaid, secrets, institution
):
    """With nothing linked there is nothing to duplicate: partial metadata is fine."""
    fake_item = prod_plaid.add_item(item_id="amex")
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(fake_item),
        {"institution": institution},
    )
    assert result.plaid_item_id == "amex"


def test_link_blocks_explain_a_paused_link_in_local_time(db, production, prod_plaid, secrets):
    linking.new_link_token(db, prod_plaid, production, store=secrets)
    (text,) = linking.link_blocks(db, production, store=secrets)
    started = NOW.astimezone().strftime("%-I:%M %p")
    unlocks = (NOW + app_secrets.LINK_TOKEN_LIFETIME).astimezone().strftime("%-I:%M %p")
    assert f"started at {started}" in text and f"linking unlocks at {unlocks}" in text
    assert "UTC" not in text and "marker" not in text
    assert "close the Plaid window" in text


def test_the_overview_explains_a_marker_set_without_an_attempt_too(db, production, secrets):
    app_secrets.set_link_in_progress(store=secrets)  # F1's own helper: no attempt id
    (text,) = linking.connection_overview(db, production, store=secrets).link_blockers
    started = NOW.astimezone().strftime("%-I:%M %p")
    assert text.startswith(f"A Plaid link started at {started} may still be in progress")
    assert "UTC" not in text


def test_a_second_re_attach_while_the_first_runs_is_refused(
    db, session_factory, production, prod_plaid, secrets
):
    """A double click on "Re-attach existing connection": the second request is refused instead of
    inserting the same Item (a unique-key error, the 500 page)."""
    fake_item = prod_plaid.add_item(item_id="restored")
    secrets.set(LEGACY, fake_item.access_token)
    real = prod_plaid.get_accounts
    nested: dict[str, BaseException] = {}

    def get_accounts(token: str):
        if not nested:
            try:
                linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
            except LinkRefused as exc:
                nested["refused"] = exc
        return real(token)

    prod_plaid.get_accounts = get_accounts  # type: ignore[method-assign]
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert isinstance(nested["refused"], linking.LinkBusy) and result.created
    assert [i.plaid_item_id for i in items(db)] == ["restored"]


def test_re_attach_keeps_plaids_creation_date_for_the_consent_estimate(
    db, session_factory, production, prod_plaid, secrets
):
    """Amex may leave consent_expiration_time empty; the estimate is a year after the last
    consent, so a re-attached Item must not restart it from today (/item/get's created_at)."""
    fake_item = prod_plaid.add_item(item_id="old-amex", consent_expiration_time=None)
    secrets.set(LEGACY, fake_item.access_token)
    created = NOW - dt.timedelta(days=330)
    real = prod_plaid.get_item

    def get_item(token: str):
        body = real(token)
        body["item"]["created_at"] = created.isoformat().replace("+00:00", "Z")
        return body

    prod_plaid.get_item = get_item  # type: ignore[method-assign]
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    db.expire_all()
    item = db.get(PlaidItem, result.item_id)
    assert item.consent_expires_at is None
    assert item.linked_at == created and item.last_consented_at == created
    assert status_service.consent_due_at(item) == created + dt.timedelta(days=365)


# --------------------------------------------------------------------------- wave 3: any bank
# BANK-1 (any institution, the 10 Items), BANK-3 (duplicates refused before the exchange), BANK-4
# (Keychain per Item, re-attach per token, the v1 token), BANK-5 (Add accounts), BANK-8 (CSV).


def checking(account_id: str = "acc-chk", *, name: str = "Checking", mask: str = "0001"):
    from tests.factories import plaid_account

    return plaid_account(
        account_id=account_id,
        name=name,
        official_name=None,
        mask=mask,
        type="depository",
        subtype="checking",
        current="900.00",
        available="850.00",
    )


def tartan_bank(db, *, accounts=(("Checking", "0001"),), **values):
    """A Tartan Bank connection in the database with its checking accounts."""
    values.setdefault("institution_id", "ins_109511")
    values.setdefault("institution_name", "Tartan Bank")
    item = make_item(db, **values)
    for name, mask in accounts:
        make_bank_account(db, item, name=name, mask=mask)
    return item


def link_meta(institution: dict | None, *accounts: tuple[str, str]) -> dict:
    return {
        "institution": institution,
        "accounts": [
            {"id": f"x-{n}", "name": name, "mask": mask, "type": "depository"}
            for n, (name, mask) in enumerate(accounts)
        ],
    }


@pytest.mark.parametrize("env", ["sandbox", "production"])
def test_a_bank_already_linked_is_refused_before_the_exchange(
    db, session_factory, sandbox, production, fake_plaid, prod_plaid, secrets, env
):
    """BANK-3: same institution_id → refused before exchanging, pointing to Add accounts and
    Reconnect; nothing reaches Plaid's exchange, in the Sandbox too (BANK-9 rehearses it)."""
    settings, plaid = (sandbox, fake_plaid) if env == "sandbox" else (production, prod_plaid)
    tartan_bank(db)
    again = plaid.add_item(
        item_id="tartan-again", institution_id="ins_109511", institution_name="Tartan Bank"
    )
    with pytest.raises(LinkRefused) as info:
        linking.exchange(
            session_factory,
            plaid,
            secrets,
            settings,
            plaid.public_token_for(again),
            link_meta(TARTAN["institution"], ("Checking", "0001")),
        )
    message = str(info.value)
    assert "Tartan Bank is already linked" in message and "not exchanged" in message
    assert "Add accounts" in message and "Reconnect" in message
    assert plaid.calls_to("exchange_public_token") == []
    assert len(items(db)) == 1


def test_a_different_login_at_a_linked_bank_is_exchanged(
    db, session_factory, production, prod_plaid, secrets
):
    """The confirm dialog's "a different login at a bank I've already linked": accepted when no
    picked account has the name and mask of one already linked there."""
    attempt = linking.new_link_token(db, prod_plaid, production, store=secrets).attempt
    tartan_bank(db, accounts=(("Checking", "0001"),))
    other = prod_plaid.add_item(
        item_id="tartan-joint",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking(mask="0777", name="Joint Checking")],
    )
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(other),
        link_meta(TARTAN["institution"], ("Joint Checking", "0777"), ("Checking", "0002")),
        attempt=attempt,
        different_login=True,
    )
    assert result.plaid_item_id == "tartan-joint" and len(items(db)) == 2
    assert secrets.values[prod_key("tartan-joint")] == other.access_token


@pytest.mark.parametrize(
    "picked",
    [("Checking", "0001"), ("  checking ", "0001")],
    ids=["same", "same-name-other-case"],
)
def test_a_different_login_with_an_account_already_linked_is_refused(
    db, session_factory, production, prod_plaid, secrets, picked
):
    """Even with the choice, the same name and mask at that bank is the same login again."""
    tartan_bank(db, accounts=(("Checking", "0001"),))
    again = prod_plaid.add_item(
        item_id="tartan-2", institution_id="ins_109511", institution_name="Tartan Bank"
    )
    with pytest.raises(LinkRefused, match="is already linked at Tartan Bank") as info:
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(again),
            link_meta(TARTAN["institution"], ("Savings", "0009"), picked),
            different_login=True,
        )
    assert "Checking ··0001" in str(info.value)
    assert prod_plaid.calls_to("exchange_public_token") == []


def test_masks_are_compared_with_masks_only(db, sandbox, fake_plaid, secrets, session_factory):
    """The same name with another mask is another account (masks never meet account numbers:
    the app has none)."""
    tartan_bank(db, accounts=(("Checking", "0001"),))
    meta = LinkMetadata.from_json(link_meta(TARTAN["institution"], ("Checking", "1000")))
    assert linking.duplicate_problem(db, meta, different_login=True) is None
    assert linking.duplicate_problem(db, meta) is not None


@pytest.mark.parametrize(
    ("metadata", "words"),
    [
        (link_meta(None, ("Checking", "0001")), "didn't say which bank"),
        ({"institution": TARTAN["institution"], "accounts": []}, "didn't list the accounts"),
        (link_meta({"name": "Tartan Bank"}, ("Savings", "0002")), "didn't say which bank"),
        (link_meta({"institution_id": "", "name": "Tartan Bank"}), "didn't say which bank"),
    ],
    ids=["no-institution", "no-accounts", "name-without-id", "empty-id"],
)
def test_metadata_that_cant_be_checked_is_refused_once_something_is_linked(db, metadata, words):
    tartan_bank(db)
    meta = LinkMetadata.from_json(metadata)
    assert words in linking.duplicate_problem(db, meta, different_login=True)


def test_an_item_saved_without_an_institution_id_is_matched_by_name(db):
    tartan_bank(db, institution_id=None)
    meta = LinkMetadata.from_json(link_meta(TARTAN["institution"], ("Checking", "0009")))
    assert "already linked" in linking.duplicate_problem(db, meta)
    other = LinkMetadata.from_json(link_meta({"institution_id": "ins_3", "name": "Chase"}))
    assert linking.duplicate_problem(db, other) is None


@pytest.mark.parametrize("position", [19, 20, 24])
def test_a_different_login_picking_more_accounts_than_are_checked_is_refused(
    db, session_factory, production, prod_plaid, secrets, position
):
    """Final review (security-2a): Link's metadata keeps the first 20 picked accounts, and an
    account already linked further down the list was never compared, so the same login was
    exchanged again as a new Item. More picked accounts than are checked now refuse the link."""
    tartan_bank(db, accounts=(("Everyday Checking", "0101"),))
    picked = [(f"Savings {n}", f"9{n:03d}") for n in range(25)]
    picked[position] = ("Everyday Checking", "0101")
    meta = link_meta(TARTAN["institution"], *picked)
    again = prod_plaid.add_item(
        item_id="item-again", institution_id="ins_109511", institution_name="Tartan Bank"
    )
    with pytest.raises(LinkRefused, match="not exchanged"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(again),
            meta,
            different_login=True,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert len(items(db)) == 1
    assert LinkMetadata.from_json(meta).accounts_truncated
    twenty = link_meta(TARTAN["institution"], *picked[:20])
    assert not LinkMetadata.from_json(twenty).accounts_truncated


def test_a_connection_saved_without_its_bank_refuses_new_links_until_a_sync_names_it(
    db, session_factory, production, prod_plaid, secrets
):
    """Final review (security-2b): a first link whose metadata names no institution (fine while
    nothing is linked) and whose /item/get failed right after linking was saved with neither the
    bank's id nor its name, so no later link matched it and the same bank was exchanged again."""
    first = prod_plaid.add_item(
        item_id="item-1",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    prod_plaid.queue_error("get_item", prod_plaid.plaid_error("INTERNAL_SERVER_ERROR"))
    linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(first),
        {"institution": None},
    )
    (row,) = items(db)
    assert (row.institution_id, row.institution_name) == (None, None)
    again = prod_plaid.add_item(
        item_id="item-2",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking("acc-2")],
    )
    for different_login in (False, True):
        with pytest.raises(LinkRefused, match="saved without the name of its bank"):
            linking.exchange(
                session_factory,
                prod_plaid,
                secrets,
                production,
                prod_plaid.public_token_for(again),
                link_meta(TARTAN["institution"], ("Checking", "0001")),
                different_login=different_login,
            )
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1
    assert sync_service.refresh_item(session_factory, prod_plaid, first.access_token, row.id)
    with pytest.raises(LinkRefused, match="Tartan Bank is already linked"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(again),
            link_meta(TARTAN["institution"], ("Checking", "0001")),
        )
    assert len(prod_plaid.calls_to("exchange_public_token")) == 1


def test_another_bank_is_never_a_duplicate(db, session_factory, sandbox, fake_plaid, secrets):
    make_item(db)  # American Express
    tartan = fake_plaid.add_item(
        item_id="tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    result = linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        sandbox,
        fake_plaid.public_token_for(tartan),
        link_meta(TARTAN["institution"], ("Checking", "0001")),
    )
    assert result.institution_name == "Tartan Bank" and len(items(db)) == 2


def test_sandbox_test_items_may_share_a_test_bank_but_production_always_checks(
    db, session_factory, sandbox, production, fake_plaid, prod_plaid, secrets
):
    for user in ("custom", "bank"):
        username, password = linking.sandbox_credentials(user, NOW.date())
        linking.sandbox_create_item(
            session_factory, fake_plaid, secrets, sandbox, username=username, password=password
        )
    assert [i.institution_id for i in items(db)] == ["ins_109508", "ins_109508"]
    fake_item = prod_plaid.add_item(item_id="x")
    with pytest.raises(ValueError, match="always check"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fake_item),
            AMEX,
            duplicate_check=False,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []


def test_the_bank_test_user_has_checking_savings_and_a_card():
    config = linking.bank_user_config(NOW.date())
    kinds = [(a["type"], a["subtype"]) for a in config["override_accounts"]]
    assert kinds == [
        ("depository", "checking"),
        ("depository", "savings"),
        ("credit", "credit card"),
    ]
    only = linking.bank_user_config(NOW.date(), bank_only=True)
    assert [a["type"] for a in only["override_accounts"]] == ["depository", "depository"]
    paychecks = [
        t["amount"]
        for t in config["override_accounts"][0]["transactions"]
        if "PAYROLL" in t["description"]
    ]
    assert paychecks == [Decimal("-2100.00")] * 2  # money in is negative (Plaid's sign)
    assert linking.sandbox_credentials("bank", NOW.date())[0] == "user_custom"


# --------------------------------------------------------------------------- the 10 connections


def test_the_app_counts_every_production_item_it_knows(db, production, secrets):
    """BANK-1: rows, Keychain's index (a lost database keeps it) and recovery files all count;
    a v1 token with nothing else known counts as one."""
    assert linking.connections_used(db, production, store=secrets) == 0
    secrets.set(LEGACY, "access-production-v1")
    assert linking.connections_used(db, production, store=secrets) == 1
    secrets.delete(LEGACY)
    make_item(db, plaid_item_id="a")
    app_secrets.store_access_token(production, "a", "access-production-a", store=secrets)
    app_secrets.store_access_token(production, "b", "access-production-b", store=secrets)
    app_secrets.write_access_token_recovery_file(production, "c", "access-production-c")
    assert linking.connections_used(db, production, store=secrets) == 3


def test_ten_items_end_new_links(db, session_factory, production, prod_plaid, secrets):
    for n in range(10):
        make_item(db, plaid_item_id=f"item-{n}", institution_id=f"ins_{100 + n}")
        app_secrets.store_access_token(
            production, f"item-{n}", f"access-production-{n}", store=secrets
        )
    (reason,) = linking.link_blocks(db, production, store=secrets)
    assert reason.startswith("All 10 of your Plaid connections are used")
    with pytest.raises(LinkRefused, match="All 10"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []
    fresh = prod_plaid.add_item(item_id="eleventh", institution_id="ins_999")
    with pytest.raises(LinkRefused, match="All 10"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(fresh),
            {"institution": {"institution_id": "ins_999", "name": "Other Bank"}},
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    overview = linking.connection_overview(db, production, store=secrets)
    assert not overview.can_link and overview.connections_left == 0


# --------------------------------------------------------------------------- the v1 token (C6)


def test_a_link_first_moves_the_v1_token_of_the_one_item(db, production, prod_plaid, secrets):
    """The owner's live upgrade: one Amex Item, its token under the v1 name. The page shows Link
    (nothing to re-attach); clicking it moves the token, then issues the link token."""
    make_item(db, plaid_item_id="item-amex")
    secrets.set(LEGACY, "access-production-v1")
    overview = linking.connection_overview(db, production, store=secrets)
    assert overview.can_link and not overview.reattach_available
    linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert secrets.values[prod_key("item-amex")] == "access-production-v1"
    assert LEGACY not in secrets.values


def test_a_v1_token_that_cannot_be_moved_pauses_linking(db, production, prod_plaid, secrets):
    """Otherwise the second Item would leave the first without a token it can find."""
    make_item(db, plaid_item_id="item-amex")
    secrets.set(LEGACY, "access-production-v1")
    secrets.fail_on[("set", prod_key("item-amex"))] = app_secrets.SecretStoreError("locked")
    with pytest.raises(LinkRefused, match="./run doctor --env production"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert prod_plaid.calls_to("create_link_token") == []
    assert secrets.values[LEGACY] == "access-production-v1"


def test_re_attach_asks_plaid_whose_v1_token_it_is(
    db, session_factory, production, prod_plaid, secrets
):
    """Two Items and the v1 token: /item/get names its Item, and the token moves to that Item's
    key; no row is added and no link is made."""
    amex = prod_plaid.add_item(item_id="item-amex")
    make_item(db, plaid_item_id="item-amex")
    tartan_bank(db, plaid_item_id="item-tartan")
    app_secrets.store_access_token(
        production, "item-tartan", "access-production-tartan", store=secrets
    )
    secrets.set(LEGACY, amex.access_token)
    assert linking.reattach_available(db, production, store=secrets)
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert (result.plaid_item_id, result.created) == ("item-amex", False)
    assert secrets.values[prod_key("item-amex")] == amex.access_token
    assert LEGACY not in secrets.values and len(items(db)) == 2
    assert prod_plaid.calls_to("exchange_public_token") == []


def test_a_v1_token_beside_another_items_own_token_is_re_attached_not_moved(
    db, session_factory, production, prod_plaid, secrets
):
    """A lost database: Tartan Bank was re-attached first, while the v1 Amex token (whose Item row
    was lost too) is still under the v1 name. The one Item has its own token, so the v1 token
    can't be its: it is never moved there; Re-attach asks Plaid and restores American Express."""
    amex = prod_plaid.add_item(item_id="item-amex")
    tartan = prod_plaid.add_item(
        item_id="item-tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    tartan_bank(db, plaid_item_id="item-tartan")
    app_secrets.store_access_token(production, "item-tartan", tartan.access_token, store=secrets)
    secrets.set(LEGACY, amex.access_token)
    assert linking.legacy_unplaced(db, store=secrets)
    assert linking.reattach_available(db, production, store=secrets)
    assert "re-attach it first" in " ".join(linking.link_blocks(db, production, store=secrets))
    assert sync_service.migrate_legacy_token(db, production, secrets).status == "kept"
    assert (
        secrets.values[prod_key("item-tartan")] == tartan.access_token
    )  # Tartan Bank's token untouched
    (result,) = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert (result.plaid_item_id, result.created) == ("item-amex", True)
    assert secrets.values[prod_key("item-amex")] == amex.access_token
    assert LEGACY not in secrets.values and not linking.legacy_unplaced(db, store=secrets)


def test_each_orphaned_token_is_re_attached_to_its_own_item(
    db, session_factory, production, prod_plaid, secrets
):
    """BANK-4: a lost database; Keychain still has two Items' tokens. Re-attach rebuilds both,
    each with its own token."""
    amex = prod_plaid.add_item(item_id="item-amex")
    tartan = prod_plaid.add_item(
        item_id="item-tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    for fake in (amex, tartan):
        app_secrets.store_access_token(production, fake.item_id, fake.access_token, store=secrets)
    assert linking.orphaned_production_items(db, store=secrets) == ["item-amex", "item-tartan"]
    assert not linking.connection_overview(db, production, store=secrets).can_link
    results = linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production)
    assert [(r.plaid_item_id, r.institution_name) for r in results] == [
        ("item-amex", "American Express"),
        ("item-tartan", "Tartan Bank"),
    ]
    assert linking.orphaned_production_items(db, store=secrets) == []
    assert [c["access_token"] for c in prod_plaid.calls_to("get_item")] == [
        amex.access_token,
        tartan.access_token,
    ]


# --------------------------------------------------------------------------- update mode (BANK-5)


def test_add_accounts_is_update_mode_with_account_selection(db, fake_plaid, secrets, settings):
    fake_item, item = linked_item(db, fake_plaid, secrets, settings)
    linking.update_link_token(
        db, fake_plaid, item.id, add_accounts=True, store=secrets, settings=settings
    )
    assert fake_plaid.calls_to("create_link_token") == [
        {
            "access_token": fake_item.access_token,
            "additional_consented_products": (),
            "account_selection": True,
        }
    ]
    with pytest.raises(ValueError):
        linking.update_link_token(
            db,
            fake_plaid,
            item.id,
            add_accounts=True,
            add_liabilities=True,
            store=secrets,
            settings=settings,
        )


def test_statement_details_are_offered_to_card_connections_only(db, fake_plaid, secrets, settings):
    """BANK-5: "Grant access to statement details" is for card connections only."""
    _fake, item = linked_item(db, fake_plaid, secrets, settings, institution_name="Tartan Bank")
    make_bank_account(db, item)
    with pytest.raises(LinkRefused, match="Tartan Bank has none"):
        linking.update_link_token(
            db, fake_plaid, item, add_liabilities=True, store=secrets, settings=settings
        )
    assert fake_plaid.calls_to("create_link_token") == []


def test_update_mode_on_the_v1_item_uses_and_moves_its_token(db, production, prod_plaid, secrets):
    fake_item = prod_plaid.add_item(item_id="item-amex")
    item = make_item(db, plaid_item_id="item-amex")
    secrets.set(LEGACY, fake_item.access_token)
    linking.update_link_token(db, prod_plaid, item, store=secrets, settings=production)
    assert prod_plaid.calls_to("create_link_token")[0]["access_token"] == fake_item.access_token
    assert secrets.values[prod_key("item-amex")] == fake_item.access_token


# --------------------------------------------------------------------------- CSV history (BANK-8)


def test_csv_history_moves_only_onto_an_amex_card(db, attach_calls):
    manual = make_account(db, manual=True)
    other_card = make_account(db, tartan_bank(db, accounts=()), name="Tartan Cash Card")
    assert linking.attach_choice(db) is None  # no American Express card: nothing to offer
    with pytest.raises(LinkRefused, match="only onto a linked American Express card"):
        linking.attach_manual(db, manual.id, other_card.id)
    amex_card = make_account(db)
    choice = linking.attach_choice(db)
    assert [o.id for o in choice.linked] == [amex_card.id] and choice.automatic
    assert linking.attach_manual(db, manual.id, amex_card.id).moved_transactions == 3


def test_linking_a_bank_leaves_the_csv_history_alone(
    db, session_factory, fake_plaid, secrets, sandbox, attach_calls
):
    make_account(db, manual=True)
    tartan = fake_plaid.add_item(
        item_id="tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    result = linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        sandbox,
        fake_plaid.public_token_for(tartan),
        link_meta(TARTAN["institution"], ("Checking", "0001")),
    )
    assert attach_calls == [] and result.attached is None and not result.attach_needed


# --------------------------------------------------------------------------- the overview (BANK-5)


def test_the_overview_shows_each_connection_with_its_kind_of_accounts(db, production, secrets):
    amex = make_item(db, plaid_item_id="item-amex")
    make_account(db, amex)
    tartan = tartan_bank(db, plaid_item_id="item-tartan", consented_products=["transactions"])
    for item_id in ("item-amex", "item-tartan"):
        app_secrets.store_access_token(
            production, item_id, f"access-production-{item_id}", store=secrets
        )
    overview = linking.connection_overview(db, production, store=secrets)
    by_id = {i.id: i for i in overview.items}
    assert overview.can_link and overview.link_actions and overview.connections_used == 2
    amex_view, tartan_view = by_id[amex.id], by_id[tartan.id]
    assert amex_view.is_amex and amex_view.has_cards and amex_view.consent_known
    assert not tartan_view.is_amex and not tartan_view.has_cards and not tartan_view.consent_known
    assert not tartan_view.statement_details_missing  # no card: nothing to grant
    (account,) = tartan_view.accounts
    assert not account.is_card and account.kind == "Checking"
    assert amex_view.accounts[0].is_card and amex_view.accounts[0].kind == "Credit card"


# ------------------------------------------------------------- W3-BANK review fixes (safety)


def v1_amex(db, prod_plaid, secrets):
    """The owner's live state before wave 3: one Amex Item, its token under the v1 name."""
    amex = prod_plaid.add_item(item_id="item-amex")
    row = make_item(db, plaid_item_id="item-amex", last_sync_success_at=None)
    make_account(db, row, plaid_account_id=amex.default_account_id, mask="1005")
    secrets.set(LEGACY, amex.access_token)
    return amex, row


def test_a_v1_copy_the_move_could_not_delete_never_blocks_linking(
    db, session_factory, production, prod_plaid, secrets, notifier
):
    """Safety review (major): the first sync moved the v1 token (new key written and read back)
    but couldn't delete the old key. That copy can't lose its owner (placing the v1 token
    compares values), so the page offers Link and a link goes through, instead of a refusal that
    blamed a failed save; the next allowed delete removes the copy."""
    amex, row = v1_amex(db, prod_plaid, secrets)
    secrets.fail_on[("delete", LEGACY)] = app_secrets.KeychainApprovalNeeded(LEGACY, -25308)
    out = sync_service.sync_item(
        session_factory,
        prod_plaid,
        secrets,
        production,
        row.id,
        trigger="schedule",
        notifier=notifier,
        sleep=no_sleep,
    )
    assert out.status == "success"
    assert secrets.values[prod_key("item-amex")] == secrets.values[LEGACY] == amex.access_token
    overview = linking.connection_overview(db, production, store=secrets)
    assert overview.can_link and overview.link_blockers == () and not overview.reattach_available

    link = linking.new_link_token(db, prod_plaid, production, store=secrets)  # retries; refused
    db.commit()
    assert link.attempt and LEGACY in secrets.values
    tartan = prod_plaid.add_item(
        item_id="item-tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        accounts=[checking()],
    )
    result = linking.exchange(
        session_factory,
        prod_plaid,
        secrets,
        production,
        prod_plaid.public_token_for(tartan),
        link_meta(TARTAN["institution"], ("Checking", "0001")),
        attempt=link.attempt,
        sleep=no_sleep,
    )
    assert result.plaid_item_id == "item-tartan" and len(items(db)) == 2
    assert secrets.values[prod_key("item-tartan")] == tartan.access_token
    assert linking.legacy_state(db, store=secrets) == app_secrets.LegacyTokenState(
        "copy", "item-amex"
    )
    # Tartan Bank's syncs never touch the copy; American Express's delete it once Keychain allows.
    del secrets.fail_on[("delete", LEGACY)]
    assert sync_service.access_token_for(production, "item-tartan", secrets) == tartan.access_token
    assert LEGACY in secrets.values
    assert sync_service.access_token_for(production, "item-amex", secrets) == amex.access_token
    assert LEGACY not in secrets.values


def test_a_v1_token_whose_move_failed_still_pauses_linking_with_the_right_reason(
    db, production, prod_plaid, secrets
):
    """The other half of C6's "on any doubt": the new key couldn't be written, so the v1 token is
    still the only copy and still unmoved. A second Item now would leave it without an owner."""
    v1_amex(db, prod_plaid, secrets)
    secrets.fail_on[("set", prod_key("item-amex"))] = app_secrets.SecretStoreError("locked")
    with pytest.raises(LinkRefused, match="couldn't save the access token of your first"):
        linking.new_link_token(db, prod_plaid, production, store=secrets)
    assert linking.legacy_state(db, store=secrets) == app_secrets.LegacyTokenState(
        "movable", "item-amex"
    )


def test_re_attach_only_deletes_a_v1_copy(db, session_factory, production, prod_plaid, secrets):
    """A copy of a token saved under its Item's own key needs no /item/get: Re-attach (run for
    an orphan) deletes it and asks Plaid about nothing else."""
    amex, _row = v1_amex(db, prod_plaid, secrets)
    app_secrets.store_access_token(production, "item-amex", amex.access_token, store=secrets)
    assert linking.recover_orphaned_items(session_factory, prod_plaid, secrets, production) == []
    assert prod_plaid.calls_to("get_item") == [] and LEGACY not in secrets.values


def test_link_metadata_naming_a_linked_bank_without_its_id_is_refused_before_the_exchange(
    db, session_factory, production, prod_plaid, secrets
):
    """Safety review (minor): metadata with the institution's name but no id (Plaid always sends
    both; this is an odd or tampered request) matched no saved Item, so a second American
    Express link was exchanged. It is refused now, and nothing reaches Plaid or Keychain."""
    amex, _row = v1_amex(db, prod_plaid, secrets)
    sync_service.migrate_legacy_token(db, production, secrets)
    link = linking.new_link_token(db, prod_plaid, production, store=secrets)
    db.commit()
    second = prod_plaid.add_item(item_id="item-amex-2")
    before = {k: v for k, v in secrets.values.items() if "link_in_progress" not in k}
    with pytest.raises(LinkRefused, match="didn't say which bank"):
        linking.exchange(
            session_factory,
            prod_plaid,
            secrets,
            production,
            prod_plaid.public_token_for(second),
            {"institution": {"name": "American Express"}, "accounts": []},
            attempt=link.attempt,
            sleep=no_sleep,
        )
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert {k: v for k, v in secrets.values.items() if "link_in_progress" not in k} == before
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert app_secrets.production_item_ids(store=secrets) == ["item-amex"]
