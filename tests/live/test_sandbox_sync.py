"""Live Plaid Sandbox: the sync engine against the real Sandbox (ARCHITECTURE §12; Gate A).

Run with ``./run test --sandbox`` (``BUDGETAPP_LIVE_SANDBOX=1``) once the Sandbox keys are in
Keychain (``./run setup --keys-only``); otherwise every test here is skipped. The keys are read
from Keychain (only the Sandbox items, see ``secrets.is_sandbox_account``); the access tokens of
the test Items stay in the in-memory store, and the database is the test database, rolled back
after each test. Plaid Link is never driven (Items come from ``/sandbox/public_token/create``).
``/transactions/refresh`` is called at most once per Item (2 per minute per Item allowed).

* ``custom`` user (an Amex-like card with known totals, ``linking.CUSTOM_CHARGES``): the totals
  match to the cent and a second sync changes nothing — the Gate A evidence, printed with ``-s``.
* ``user_transactions_dynamic``: notes on a pending charge follow it when the refresh posts it.
* ``/sandbox/item/reset_login``: the next sync marks the Item ``needs_reconnect``.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.models import Account, PlaidItem, Transaction, system_category
from budgetapp.plaid_gateway import RealPlaidGateway
from budgetapp.services import linking, sync

pytestmark = pytest.mark.sandbox

BACKFILL_DELAYS = (5.0, 5.0, 10.0, 10.0, 15.0, 15.0, 30.0, 30.0, 60.0, 60.0)
BACKFILL_CAP = 360.0
NOTE = "Live test: carried from the pending charge"


@pytest.fixture
def sandbox_gateway() -> RealPlaidGateway:
    """The real Plaid Sandbox, with the keys from Keychain (skipped when they aren't saved)."""
    keychain = app_secrets.KeychainStore()
    try:
        client_id, secret = app_secrets.plaid_credentials("sandbox", store=keychain)
    except app_secrets.MissingSecretError:
        pytest.skip("no Plaid Sandbox keys in Keychain (./run setup --keys-only)")
    return RealPlaidGateway("sandbox", client_id, secret)


def create_item(session_factory, gateway, secrets, settings, user: str) -> linking.LinkResult:
    username, password = linking.sandbox_credentials(user, clock.today())
    return linking.sandbox_create_item(
        session_factory, gateway, secrets, settings, username=username, password=password
    )


def backfill(session_factory, gateway, secrets, settings, notifier, item_id: int) -> list:
    outcomes = sync.backfill_until_complete(
        session_factory,
        gateway,
        secrets,
        settings,
        item_id,
        notifier=notifier,
        delays=BACKFILL_DELAYS,
        cap=BACKFILL_CAP,
    )
    assert outcomes and all(o.status != "error" for o in outcomes), outcomes
    assert any(o.update_status == sync.HISTORICAL_UPDATE_COMPLETE for o in outcomes), outcomes
    return outcomes


def ledger(db, item_id: int) -> list[tuple]:
    db.expire_all()
    rows = db.execute(
        select(
            Transaction.plaid_transaction_id,
            Transaction.amount,
            Transaction.date,
            Transaction.pending,
            Transaction.name,
            Transaction.category_id,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(Account.item_id == item_id)
        .order_by(Transaction.plaid_transaction_id)
    ).all()
    return [tuple(row) for row in rows]


def test_the_amex_like_card_has_its_known_totals_and_a_second_sync_changes_nothing(
    db, session_factory, secrets, settings, notifier, sandbox_gateway
):
    result = create_item(session_factory, sandbox_gateway, secrets, settings, "custom")
    outcomes = backfill(
        session_factory, sandbox_gateway, secrets, settings, notifier, result.item_id
    )

    db.expire_all()
    (card,) = db.scalars(select(Account).where(Account.item_id == result.item_id)).all()
    assert (card.type, card.subtype, card.mask) == ("credit", "credit card", "1005")
    rows = list(db.scalars(select(Transaction).where(Transaction.account_id == card.id)))
    assert len(rows) == len(linking.CUSTOM_CHARGES)
    posted = sum((t.amount for t in rows if not t.pending), Decimal("0.00"))
    pending = sum((t.amount for t in rows if t.pending), Decimal("0.00"))
    assert posted == linking.custom_posted_total() == Decimal("793.61")
    assert pending == linking.custom_pending_total() == Decimal("42.10")
    payment = next(t for t in rows if t.amount == Decimal("-500.00"))
    assert payment.category_id == system_category(db, "card_payments").id
    item = db.get(PlaidItem, result.item_id)
    assert item.history_complete_at is not None and item.transactions_cursor
    if item.liabilities_status == "ok":  # RESEARCH T3.7: the settable liability fields
        assert card.statement_balance == Decimal("1254.17")
        assert card.minimum_payment == Decimal("40.00")

    before = ledger(db, result.item_id)
    again = sync.sync_item(
        session_factory,
        sandbox_gateway,
        secrets,
        settings,
        result.item_id,
        trigger="cli",
        notifier=notifier,
    )
    assert again.status == "success"
    assert (again.added, again.modified, again.removed) == (0, 0, 0)
    assert ledger(db, result.item_id) == before
    print(
        f"\nGate A evidence (Plaid Sandbox, custom Amex-like card): {len(rows)} transactions, "
        f"posted ${posted}, pending ${pending}, after {len(outcomes)} sync(s); "
        f"a second sync added 0, changed 0, removed 0 and left the ledger identical."
    )


def test_notes_on_a_pending_charge_follow_it_when_it_posts(
    db, session_factory, secrets, settings, notifier, sandbox_gateway
):
    result = create_item(
        session_factory, sandbox_gateway, secrets, settings, "user_transactions_dynamic"
    )
    backfill(session_factory, sandbox_gateway, secrets, settings, notifier, result.item_id)
    db.expire_all()
    pending = db.scalars(
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(Account.item_id == result.item_id, Transaction.pending.is_(True))
        .order_by(Transaction.date.desc(), Transaction.id)
    ).first()
    if pending is None:
        pytest.skip("the Sandbox sent no pending charge this time")
    pending_id = pending.plaid_transaction_id
    pending.notes = NOTE
    pending.user_edited_at = clock.now()
    db.flush()

    plaid_item_id = db.get(PlaidItem, result.item_id).plaid_item_id
    assert sync.request_refresh(sandbox_gateway, secrets, settings, plaid_item_id) is None
    deadline = time.monotonic() + 240
    while True:
        time.sleep(15)
        outcome = sync.sync_item(
            session_factory,
            sandbox_gateway,
            secrets,
            settings,
            result.item_id,
            trigger="manual",
            notifier=notifier,
        )
        assert outcome.status == "success", outcome
        db.expire_all()
        gone = db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == pending_id)
        ).first()
        if gone is None or time.monotonic() > deadline:
            break
    assert gone is None, "the refresh did not post the pending charge within 4 minutes"
    carried = list(db.scalars(select(Transaction).where(Transaction.notes == NOTE)))
    assert len(carried) == 1
    assert not carried[0].pending and carried[0].plaid_transaction_id != pending_id


def test_a_reset_login_makes_the_next_sync_ask_to_reconnect(
    db, session_factory, secrets, settings, notifier, sandbox_gateway
):
    result = create_item(session_factory, sandbox_gateway, secrets, settings, "user_good")
    token = sync.access_token_for(settings, result.plaid_item_id, secrets)
    sandbox_gateway.sandbox_reset_login(token)
    outcome = sync.sync_item(
        session_factory,
        sandbox_gateway,
        secrets,
        settings,
        result.item_id,
        trigger="cli",
        notifier=notifier,
    )
    assert (outcome.status, outcome.error_code) == ("error", "ITEM_LOGIN_REQUIRED")
    db.expire_all()
    assert db.get(PlaidItem, result.item_id).status == "needs_reconnect"
