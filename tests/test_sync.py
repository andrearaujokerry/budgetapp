"""The sync engine against FakePlaidGateway (ARCHITECTURE §6, §8; SYNC-2, SYNC-4 … SYNC-7, REL-1).

Every test links one fake Item (``Linked``): a ``plaid_items`` row, the fake's Item and its access
token in the in-memory secret store. ``linked.sync()`` runs ``sync.sync_item`` with sessions on the
test connection (each "commit" releases a savepoint), then expires the ``db`` session so reads see
what the sync wrote.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from budgetapp import db as app_db
from budgetapp import periods
from budgetapp import secrets as app_secrets
from budgetapp.models import (
    Account,
    PlaidItem,
    Statement,
    SyncRun,
    Transaction,
    TransactionTombstone,
)
from budgetapp.services import (
    alerts,
    csv_import,
    reconcile,
    spending,
    sync,
    userstate,
)
from budgetapp.services.events import SyncOutcome
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_item,
    make_rule,
    make_split,
    make_statement,
    make_sync_run,
    make_tombstone,
    make_transaction,
    plaid_credit_liability,
    plaid_txn,
)
from tests.fake_plaid import ADDED, Event, FakePlaidGateway

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


def no_sleep(_seconds: float) -> None:
    return None


class Linked:
    """A fake Plaid Item linked in the test database, with its access token saved."""

    def __init__(
        self,
        db,
        session_factory,
        fake: FakePlaidGateway,
        store,
        settings,
        notifier,
        *,
        item_id: str = "item-amex",
        consented: tuple[str, ...] = ("transactions", "liabilities"),
        **item_values: Any,
    ) -> None:
        self.db = db
        self.session_factory = session_factory
        self.fake = fake
        self.store = store
        self.settings = settings
        self.notifier = notifier
        self.fake_item = fake.add_item(item_id=item_id, consented_products=consented)
        app_secrets.store_access_token(settings, item_id, self.fake_item.access_token, store=store)
        values = {"last_sync_success_at": None, "consented_products": list(consented)}
        values.update(item_values)
        self.item = make_item(db, plaid_item_id=item_id, **values)

    @property
    def account_pid(self) -> str:
        return self.fake_item.default_account_id

    def sync(self, **kwargs: Any) -> SyncOutcome:
        kwargs.setdefault("trigger", "cli")
        kwargs.setdefault("sleep", no_sleep)
        outcome = sync.sync_item(
            self.session_factory,
            self.fake,
            self.store,
            self.settings,
            self.item.id,
            notifier=self.notifier,
            **kwargs,
        )
        self.db.expire_all()
        return outcome

    def rows(self) -> list[Transaction]:
        self.db.expire_all()
        return list(self.db.scalars(select(Transaction).order_by(Transaction.id)))

    def row(self, plaid_id: str) -> Transaction | None:
        self.db.expire_all()
        return self.db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == plaid_id)
        ).one_or_none()

    def account(self) -> Account:
        self.db.expire_all()
        return self.db.scalars(select(Account).where(Account.item_id == self.item.id)).one()

    def tombstone(self, plaid_id: str) -> TransactionTombstone | None:
        self.db.expire_all()
        return self.db.get(TransactionTombstone, plaid_id)

    def runs(self) -> list[SyncRun]:
        self.db.expire_all()
        return list(self.db.scalars(select(SyncRun).order_by(SyncRun.id)))

    def ledger(self) -> list[tuple]:
        """Everything a sync could change in the ledger (rows, splits, tombstones)."""
        self.db.expire_all()
        rows = [
            (
                t.plaid_transaction_id,
                t.account_id,
                t.amount,
                t.date,
                t.posted_date,
                t.pending,
                t.name,
                t.category_id,
                t.category_source,
                t.notes,
                tuple(t.tags),
                tuple((s.category_id, s.amount) for s in t.splits),
                t.needs_review,
            )
            for t in self.rows()
        ]
        tombs = [
            (t.plaid_transaction_id, t.user_state, t.consumed_by_transaction_id)
            for t in self.db.scalars(
                select(TransactionTombstone).order_by(TransactionTombstone.plaid_transaction_id)
            )
        ]
        return [rows, tombs]


def edit(db, txn: Transaction, *, category=None, notes=None, tags=()) -> None:
    """What the Transactions page does when the user edits a row."""
    if category is not None:
        txn.category_id = category.id
        txn.category_source = "user"
        txn.rule_id = None
    if notes is not None:
        txn.notes = notes
    if tags:
        txn.tags = list(tags)
    txn.user_edited_at = NOW
    db.flush()


@pytest.fixture
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def linked(db, session_factory, fake_plaid, secrets, settings, notifier, at_now) -> Linked:
    return Linked(db, session_factory, fake_plaid, secrets, settings, notifier)


@pytest.fixture
def alert_calls(monkeypatch) -> list[SyncOutcome]:
    calls: list[SyncOutcome] = []

    def record(session, notifier, outcome):
        calls.append(outcome)
        return []

    monkeypatch.setattr(alerts, "evaluate_after_sync", record)
    return calls


@pytest.fixture
def reconcile_calls(monkeypatch) -> list[int]:
    calls: list[int] = []

    def record(session, item_id):
        calls.append(item_id)
        return reconcile.ReconcileResult()

    monkeypatch.setattr(reconcile, "reconcile_item", record)
    return calls


# --------------------------------------------------------------------------- idempotency (REL-1)


def test_a_first_sync_inserts_and_a_second_sync_changes_nothing(linked, fake_plaid):
    fake_plaid.add_transaction(linked.fake_item, amount="12.34", date="2026-10-10")
    fake_plaid.add_pending(
        linked.fake_item,
        amount="45.00",
        date="2026-10-14",
        name="SHAKE SHACK",
        merchant_name="Shake Shack",
    )
    first = linked.sync()
    assert (first.status, first.added, first.modified, first.removed) == ("success", 2, 0, 0)
    before, cursor = linked.ledger(), linked.item.transactions_cursor
    assert cursor
    second = linked.sync()
    assert (second.status, second.added, second.modified, second.removed) == ("success", 0, 0, 0)
    assert linked.ledger() == before
    assert linked.item.transactions_cursor == cursor
    assert [(r.status, r.added_count) for r in linked.runs()] == [("success", 2), ("success", 0)]


def test_re_delivered_rows_never_duplicate(linked, fake_plaid):
    txn = fake_plaid.add_transaction(linked.fake_item, amount="9.99")
    linked.sync()
    linked.fake_item.events.append(Event(ADDED, txn))  # Plaid sends the same id again
    again = linked.sync()
    assert (again.added, again.modified) == (0, 0)
    assert len(linked.rows()) == 1


def test_one_id_on_several_pages_lands_once_with_its_last_version(linked, fake_plaid):
    fake_plaid.page_size = 1
    txn = fake_plaid.add_transaction(linked.fake_item, amount="10.00", name="FIRST")
    fake_plaid.modify_transaction(linked.fake_item, txn["transaction_id"], name="SECOND")
    fake_plaid.modify_transaction(linked.fake_item, txn["transaction_id"], amount="11.50")
    out = linked.sync()
    assert out.added == 1 and len(fake_plaid.calls_to("transactions_sync")) == 3
    (row,) = linked.rows()
    assert (row.name, row.amount) == ("SECOND", Decimal("11.50"))


def test_added_then_removed_in_one_sync_never_lands(linked, fake_plaid):
    fake_plaid.page_size = 1
    txn = fake_plaid.add_pending(linked.fake_item, amount="3.00")
    fake_plaid.remove_transaction(linked.fake_item, txn["transaction_id"])
    out = linked.sync()
    assert (out.added, out.removed) == (0, 0) and linked.rows() == []


# --------------------------------------------------------------------------- pagination failures


def test_a_transport_error_mid_pagination_restarts_from_the_original_cursor(linked, fake_plaid):
    fake_plaid.page_size = 2
    for n in range(5):
        fake_plaid.add_transaction(linked.fake_item, amount=f"{n + 1}.00")
    fake_plaid.script_sync(linked.fake_item, 2, error=fake_plaid.transport_error())
    out = linked.sync()
    assert out.status == "success" and out.added == 5
    cursors = [c["cursor"] for c in fake_plaid.calls_to("transactions_sync")]
    assert cursors[0] is None and cursors[2] is None  # page 2 failed: start again from scratch
    assert len(linked.rows()) == 5


def test_a_plaid_error_mid_pagination_changes_nothing_and_the_retry_starts_over(
    linked, fake_plaid, settings
):
    fake_plaid.add_transaction(linked.fake_item, amount="1.00")
    linked.sync()
    start = linked.item.transactions_cursor
    before = linked.ledger()
    fake_plaid.page_size = 2
    for n in range(5):
        fake_plaid.add_transaction(linked.fake_item, amount=f"{n + 2}.00")
    calls_so_far = linked.fake_item.sync_calls
    fake_plaid.script_sync(
        linked.fake_item,
        calls_so_far + 2,
        error=fake_plaid.plaid_error("ITEM_LOGIN_REQUIRED", display_message="Sign in again"),
    )
    failed = linked.sync()
    assert failed.status == "error" and failed.error_code == "ITEM_LOGIN_REQUIRED"
    assert linked.ledger() == before
    assert linked.item.transactions_cursor == start
    assert (linked.item.status, linked.item.error_display_message) == (
        "needs_reconnect",
        "Sign in again",
    )
    retry = linked.sync()
    assert retry.status == "success" and retry.added == 5
    retry_cursors = [c["cursor"] for c in fake_plaid.calls_to("transactions_sync")][-3:]
    assert retry_cursors[0] == start
    assert linked.item.status == "healthy" and linked.item.error_code is None


def test_a_mutation_during_pagination_restarts_and_picks_up_the_new_rows(linked, fake_plaid):
    fake_plaid.page_size = 2
    for n in range(4):
        fake_plaid.add_transaction(linked.fake_item, amount=f"{n + 1}.00")
    late = plaid_txn(transaction_id="late-arrival", account_id=linked.account_pid, amount="99.00")
    fake_plaid.mutate_during_pagination(linked.fake_item, on_call=2, events=[Event(ADDED, late)])
    out = linked.sync()
    assert out.status == "success" and out.added == 5
    assert linked.row("late-arrival") is not None
    assert [c["cursor"] for c in fake_plaid.calls_to("transactions_sync")][2] is None


def test_too_many_restarts_record_a_transport_error(linked, fake_plaid):
    fake_plaid.add_transaction(linked.fake_item)
    for _ in range(sync.MAX_PAGINATION_RESTARTS + 1):
        fake_plaid.queue_error("transactions_sync", fake_plaid.transport_error())
    out = linked.sync()
    assert (out.status, out.error_code, out.item_status) == ("error", "TRANSPORT_ERROR", "error")
    assert linked.rows() == [] and linked.item.transactions_cursor is None
    assert linked.runs()[-1].error_code == "TRANSPORT_ERROR"


def test_a_cursor_that_moved_is_refetched_not_overwritten(linked, fake_plaid, session_factory):
    fake_plaid.add_transaction(linked.fake_item, transaction_id="only-one")
    moved = fake_plaid._cursor(linked.fake_item, 0)
    real_sync = fake_plaid.transactions_sync
    seen: list[str | None] = []

    def moving(token, cursor, **kwargs):
        seen.append(cursor)
        if len(seen) == 1:  # someone else stored a cursor while this sync was fetching
            with app_db.transaction(session_factory) as s:
                s.get(PlaidItem, linked.item.id).transactions_cursor = moved
        return real_sync(token, cursor, **kwargs)

    fake_plaid.transactions_sync = moving
    out = linked.sync()
    assert out.status == "success" and out.added == 1
    assert seen == [None, moved]
    assert len(linked.rows()) == 1


# --------------------------------------------------------------------------- NOT_READY and history


def test_not_ready_records_success_and_keeps_the_cursor_null(linked, fake_plaid):
    fake_plaid.set_update_status(linked.fake_item, "NOT_READY")
    fake_plaid.add_transaction(linked.fake_item)
    out = linked.sync()
    assert (out.status, out.added, out.update_status) == ("not_ready", 0, "NOT_READY")
    assert out.ok
    assert linked.item.transactions_cursor is None  # never ''
    assert linked.item.transactions_update_status == "NOT_READY"
    run = linked.runs()[-1]
    assert (run.status, run.update_status) == ("success", "NOT_READY")
    fake_plaid.set_update_status(linked.fake_item, "INITIAL_UPDATE_COMPLETE")
    assert linked.sync().added == 1


def test_initial_then_historical_backfill_adds_the_older_rows(linked, fake_plaid, alert_calls):
    fake_plaid.set_update_status(linked.fake_item, "INITIAL_UPDATE_COMPLETE")
    fake_plaid.add_transaction(linked.fake_item, date="2026-10-10", amount="5.00")
    fake_plaid.add_pending(linked.fake_item, date="2026-10-19", amount="6.00")
    first = linked.sync()
    assert first.added == 2 and first.initial_history_load and not first.history_completed_now
    assert linked.item.history_complete_at is None
    assert linked.account().plaid_history_start is None
    for day in ("2024-11-02", "2025-06-30"):
        fake_plaid.add_transaction(linked.fake_item, date=day, amount="7.00")
    fake_plaid.set_update_status(linked.fake_item, "HISTORICAL_UPDATE_COMPLETE")
    second = linked.sync()
    assert second.added == 2 and second.initial_history_load and second.history_completed_now
    assert linked.item.history_complete_at == NOW
    assert linked.account().plaid_history_start == dt.date(2024, 11, 2)
    third = linked.sync()
    assert not third.initial_history_load and not third.history_completed_now
    assert [o.initial_history_load for o in alert_calls] == [True, True, False]


# ------------------------------------------------------------------- pending → posted (SYNC-4)


def test_pending_to_posted_in_one_sync_carries_category_notes_and_tags(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(
        linked.fake_item, amount="20.00", name="UBER TRIP", merchant_name="Uber"
    )
    linked.sync()
    row = linked.row(pending["transaction_id"])
    edit(db, row, category=get_category(db, "Travel"), notes="Airport ride", tags=("work",))
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"])
    out = linked.sync()
    assert (out.added, out.removed) == (1, 1)
    assert linked.row(pending["transaction_id"]) is None
    new = linked.row(posted["transaction_id"])
    assert (new.category_id, new.category_source) == (get_category(db, "Travel").id, "user")
    assert (new.notes, new.tags, new.pending) == ("Airport ride", ["work"], False)
    assert new.posted_date == new.date
    tomb = linked.tombstone(pending["transaction_id"])
    assert tomb.consumed_by_transaction_id == new.id
    assert tomb.user_state["notes"] == "Airport ride"


def test_posted_row_first_in_the_batch_still_replaces_the_pending_row(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="8.00")
    linked.sync()
    edit(db, linked.row(pending["transaction_id"]), notes="Lunch")
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], added_first=True)
    linked.sync()
    assert [r.plaid_transaction_id for r in linked.rows()] == [posted["transaction_id"]]
    assert linked.row(posted["transaction_id"]).notes == "Lunch"


def test_pending_to_posted_across_two_syncs_uses_the_tombstone(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="31.00", merchant_name="Shake Shack")
    linked.sync()
    edit(
        db,
        linked.row(pending["transaction_id"]),
        category=get_category(db, "Groceries"),
        notes="Burgers",
    )
    fake_plaid.remove_transaction(linked.fake_item, pending["transaction_id"])
    gone = linked.sync()
    assert gone.removed == 1 and linked.rows() == []
    tomb = linked.tombstone(pending["transaction_id"])
    assert tomb.user_state["notes"] == "Burgers" and tomb.consumed_by_transaction_id is None
    posted = fake_plaid.add_transaction(
        linked.fake_item,
        amount="31.00",
        merchant_name="Shake Shack",
        pending_transaction_id=pending["transaction_id"],
    )
    linked.sync()
    row = linked.row(posted["transaction_id"])
    assert (row.notes, row.category_id) == ("Burgers", get_category(db, "Groceries").id)
    assert linked.tombstone(pending["transaction_id"]).consumed_by_transaction_id == row.id


def test_a_tip_changes_the_amount_but_the_edits_still_carry(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="50.00", merchant_name="Nopa")
    linked.sync()
    dining = get_category(db, "Dining & Drinks")
    edit(db, linked.row(pending["transaction_id"]), category=dining, notes="Birthday dinner")
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], amount="58.00")
    linked.sync()
    row = linked.row(posted["transaction_id"])
    assert (row.amount, row.notes, row.category_id, row.category_source) == (
        Decimal("58.00"),
        "Birthday dinner",
        dining.id,
        "user",
    )


def test_a_stale_pending_id_is_never_inserted_again(linked, fake_plaid):
    pending = fake_plaid.add_pending(linked.fake_item, amount="4.00")
    linked.sync()
    fake_plaid.post_pending(linked.fake_item, pending["transaction_id"])
    linked.sync()
    linked.fake_item.events.append(Event(ADDED, pending))  # a late, stale copy
    out = linked.sync()
    assert out.added == 0 and len(linked.rows()) == 1


def test_an_id_named_by_a_stored_posted_row_is_never_inserted(linked, fake_plaid):
    fake_plaid.add_transaction(linked.fake_item, pending_transaction_id="never-seen-pending")
    linked.sync()
    ghost = plaid_txn(
        transaction_id="never-seen-pending", account_id=linked.account_pid, pending=True
    )
    linked.fake_item.events.append(Event(ADDED, ghost))
    assert linked.sync().added == 0 and len(linked.rows()) == 1


def test_a_removed_id_is_never_resurrected(linked, fake_plaid):
    txn = fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    fake_plaid.remove_transaction(linked.fake_item, txn["transaction_id"])
    linked.sync()
    linked.fake_item.events.append(Event(ADDED, txn))
    assert linked.sync().added == 0 and linked.rows() == []


# --------------------------------------------------------------------------- churn heuristic


def test_unlinked_churn_carries_edits_to_the_right_one_of_two_similar_charges(
    linked, fake_plaid, db
):
    a = fake_plaid.add_transaction(
        linked.fake_item,
        amount="5.25",
        date="2026-10-10",
        name="STARBUCKS 1234",
        merchant_name="Starbucks",
    )
    b = fake_plaid.add_transaction(
        linked.fake_item,
        amount="5.75",
        date="2026-10-11",
        name="STARBUCKS 1234",
        merchant_name="Starbucks",
    )
    linked.sync()
    edit(db, linked.row(a["transaction_id"]), notes="With Sam", tags=("social",))
    edit(db, linked.row(b["transaction_id"]), notes="Team coffee")
    new_a = fake_plaid.churn(linked.fake_item, a["transaction_id"])
    new_b = fake_plaid.churn(linked.fake_item, b["transaction_id"])
    out = linked.sync()
    assert (out.added, out.removed) == (2, 2)
    row_a, row_b = linked.row(new_a["transaction_id"]), linked.row(new_b["transaction_id"])
    assert (row_a.notes, row_a.tags) == ("With Sam", ["social"])
    assert (row_b.notes, row_b.tags) == ("Team coffee", [])
    assert linked.tombstone(a["transaction_id"]).consumed_by_transaction_id == row_a.id
    assert linked.tombstone(b["transaction_id"]).consumed_by_transaction_id == row_b.id


def test_churn_never_carries_edits_to_an_unrelated_new_charge(linked, fake_plaid, db):
    a = fake_plaid.add_transaction(linked.fake_item, amount="5.25", merchant_name="Starbucks")
    linked.sync()
    edit(db, linked.row(a["transaction_id"]), notes="With Sam")
    fake_plaid.remove_transaction(linked.fake_item, a["transaction_id"])
    other_merchant = fake_plaid.add_transaction(
        linked.fake_item, amount="5.25", name="LYFT RIDE", merchant_name="Lyft"
    )
    too_big = fake_plaid.add_transaction(linked.fake_item, amount="9.25", merchant_name="Starbucks")
    too_late = fake_plaid.add_transaction(
        linked.fake_item, amount="5.25", merchant_name="Starbucks", date="2026-10-30"
    )
    linked.sync()
    for txn in (other_merchant, too_big, too_late):
        assert linked.row(txn["transaction_id"]).notes is None
    tomb = linked.tombstone(a["transaction_id"])
    assert tomb.consumed_by_transaction_id is None and tomb.user_state["notes"] == "With Sam"


def test_a_tombstone_from_an_earlier_sync_is_matched_within_30_days_only(linked, fake_plaid, db):
    account = make_account_for(linked)
    recent = make_tombstone(
        db,
        account,
        plaid_transaction_id="recent-gone",
        amount=Decimal("12.00"),
        date=dt.date(2026, 10, 12),
        name="BLUE BOTTLE COFFEE",
        merchant_name="Blue Bottle Coffee",
        pending=True,
        removed_at=NOW - dt.timedelta(days=10),
        user_state=userstate.to_json(userstate.UserState(notes="Kept")),
    )
    make_tombstone(
        db,
        account,
        plaid_transaction_id="old-gone",
        amount=Decimal("40.00"),
        date=dt.date(2026, 9, 1),
        name="BLUE BOTTLE COFFEE",
        merchant_name="Blue Bottle Coffee",
        removed_at=NOW - dt.timedelta(days=31),
        user_state=userstate.to_json(userstate.UserState(notes="Too old")),
    )
    near = fake_plaid.add_transaction(linked.fake_item, amount="12.00", date="2026-10-13")
    far = fake_plaid.add_transaction(linked.fake_item, amount="40.00", date="2026-09-02")
    linked.sync()
    assert linked.row(near["transaction_id"]).notes == "Kept"
    assert linked.row(far["transaction_id"]).notes is None
    assert linked.tombstone(recent.plaid_transaction_id).consumed_by_transaction_id is not None
    assert linked.tombstone("old-gone").consumed_by_transaction_id is None


def make_account_for(linked: Linked) -> Account:
    """The card's account row, as the first sync would create it (without syncing)."""
    return make_account(
        linked.db,
        linked.item,
        plaid_account_id=linked.account_pid,
        name="Platinum Card",
        mask="1000",
        type="credit",
        subtype="credit card",
    )


def test_a_posted_candidate_wins_over_a_pending_one_for_a_pending_source(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="15.00", merchant_name="Chipotle")
    linked.sync()
    edit(db, linked.row(pending["transaction_id"]), notes="Burrito")
    fake_plaid.remove_transaction(linked.fake_item, pending["transaction_id"])
    still_pending = fake_plaid.add_pending(
        linked.fake_item, amount="15.00", merchant_name="Chipotle"
    )
    posted = fake_plaid.add_transaction(linked.fake_item, amount="15.00", merchant_name="Chipotle")
    linked.sync()
    assert linked.row(posted["transaction_id"]).notes == "Burrito"
    assert linked.row(still_pending["transaction_id"]).notes is None


def test_edits_carried_once_are_not_offered_again_when_the_carrier_goes(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="22.00", merchant_name="Joe's")
    linked.sync()
    edit(db, linked.row(pending["transaction_id"]), notes="Pizza night")
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"])
    linked.sync()
    churned = fake_plaid.churn(linked.fake_item, posted["transaction_id"])
    linked.sync()
    assert linked.row(churned["transaction_id"]).notes == "Pizza night"
    first = linked.tombstone(pending["transaction_id"])
    assert userstate.from_json(first.user_state) is None  # handed over, never listed twice
    second = linked.tombstone(posted["transaction_id"])
    assert second.consumed_by_transaction_id == linked.row(churned["transaction_id"]).id


# --------------------------------------------------------------------------- modified and removed


def test_modified_rows_keep_every_user_field(linked, fake_plaid, db):
    txn = fake_plaid.add_transaction(linked.fake_item, amount="50.00", name="TARGET 001")
    linked.sync()
    row = linked.row(txn["transaction_id"])
    shopping, home = get_category(db, "Shopping"), get_category(db, "Home")
    edit(db, row, category=shopping, notes="Gifts", tags=("holiday",))
    make_split(db, row, shopping, "30.00")
    make_split(db, row, home, "20.00")
    fake_plaid.modify_transaction(linked.fake_item, txn["transaction_id"], name="TARGET 00012345")
    out = linked.sync()
    assert out.modified == 1
    row = linked.row(txn["transaction_id"])
    assert row.name == "TARGET 00012345"
    assert (row.category_id, row.category_source, row.notes, row.tags) == (
        shopping.id,
        "user",
        "Gifts",
        ["holiday"],
    )
    assert [(s.category_id, s.amount) for s in row.splits] == [
        (shopping.id, Decimal("30.00")),
        (home.id, Decimal("20.00")),
    ]


def test_a_modified_amount_rescales_the_splits_and_asks_for_review(linked, fake_plaid, db):
    txn = fake_plaid.add_transaction(linked.fake_item, amount="50.00")
    linked.sync()
    row = linked.row(txn["transaction_id"])
    shopping, home = get_category(db, "Shopping"), get_category(db, "Home")
    make_split(db, row, shopping, "30.00")
    make_split(db, row, home, "20.00")
    fake_plaid.modify_transaction(linked.fake_item, txn["transaction_id"], amount="60.00")
    linked.sync()
    row = linked.row(txn["transaction_id"])
    assert [s.amount for s in row.splits] == [Decimal("36.00"), Decimal("24.00")]
    assert row.needs_review and "rescaled" in row.review_reason


def test_removed_rows_disappear_and_leave_a_tombstone(linked, fake_plaid, db):
    keep = fake_plaid.add_transaction(linked.fake_item, amount="1.00")
    plain = fake_plaid.add_transaction(linked.fake_item, amount="2.00")
    edited = fake_plaid.add_transaction(linked.fake_item, amount="3.00")
    linked.sync()
    edit(db, linked.row(edited["transaction_id"]), notes="Refund pending")
    fake_plaid.remove_transaction(linked.fake_item, plain["transaction_id"])
    fake_plaid.remove_transaction(linked.fake_item, edited["transaction_id"])
    fake_plaid.remove_transaction(linked.fake_item, "never-stored")  # unknown ids are a no-op
    out = linked.sync()
    assert out.removed == 2
    assert [r.plaid_transaction_id for r in linked.rows()] == [keep["transaction_id"]]
    assert userstate.from_json(linked.tombstone(plain["transaction_id"]).user_state) is None
    assert linked.tombstone(edited["transaction_id"]).user_state["notes"] == "Refund pending"
    assert linked.tombstone("never-stored") is None


def test_tombstones_older_than_90_days_are_pruned(linked, fake_plaid, db):
    account = make_account_for(linked)
    make_tombstone(
        db, account, plaid_transaction_id="ancient", removed_at=NOW - dt.timedelta(days=91)
    )
    make_tombstone(
        db, account, plaid_transaction_id="young", removed_at=NOW - dt.timedelta(days=89)
    )
    fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    assert linked.tombstone("ancient") is None and linked.tombstone("young") is not None


# --------------------------------------------------------------------------- categorization


def test_new_rows_are_categorized_and_modified_rows_recategorized(linked, fake_plaid, db):
    flight = fake_plaid.add_transaction(
        linked.fake_item, amount="420.00", pfc_primary="TRAVEL", pfc_detailed="TRAVEL_FLIGHTS"
    )
    payment = fake_plaid.add_transaction(
        linked.fake_item,
        amount="-500.00",
        name="ONLINE PAYMENT - THANK YOU",
        merchant_name=None,
        pfc_primary="LOAN_PAYMENTS",
        pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    mine = fake_plaid.add_transaction(linked.fake_item, amount="30.00")
    linked.sync()
    travel, groceries = get_category(db, "Travel"), get_category(db, "Groceries")
    assert (
        linked.row(flight["transaction_id"]).category_id,
        linked.row(flight["transaction_id"]).category_source,
    ) == (travel.id, "plaid")
    paid = linked.row(payment["transaction_id"])
    assert (paid.category_id, paid.category_source) == (card_payments(db).id, "system")
    edit(db, linked.row(mine["transaction_id"]), category=travel)
    for txn in (flight, mine):
        fake_plaid.modify_transaction(
            linked.fake_item,
            txn["transaction_id"],
            personal_finance_category={
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_GROCERIES",
                "confidence_level": "HIGH",
                "version": "v2",
            },
        )
    linked.sync()
    assert linked.row(flight["transaction_id"]).category_id == groceries.id
    assert linked.row(mine["transaction_id"]).category_id == travel.id  # the user's choice stays


def test_rules_apply_to_new_rows(linked, fake_plaid, db):
    gifts = get_category(db, "Gifts & Donations")
    make_rule(db, gifts, merchant_pattern="Blue Bottle")
    txn = fake_plaid.add_transaction(linked.fake_item, amount="18.00")
    linked.sync()
    row = linked.row(txn["transaction_id"])
    assert (row.category_id, row.category_source) == (gifts.id, "rule")


# ------------------------------------------------------------------- money and display fields


def test_locations_with_decimal_coordinates_are_stored(linked, fake_plaid):
    txn = fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    location = linked.row(txn["transaction_id"]).location
    assert location["lat"] == Decimal("37.7955") and location["lon"] == Decimal("-122.3937")
    assert location["city"] == "San Francisco"


def test_rows_without_a_currency_are_flagged(linked, fake_plaid):
    txn = fake_plaid.add_transaction(linked.fake_item, iso_currency_code=None)
    linked.sync()
    row = linked.row(txn["transaction_id"])
    assert row.needs_review and "without a currency" in row.review_reason


def test_money_problems_are_explained():
    assert "more than two decimals" in sync._money_problem(
        {"amount": Decimal("12.345"), "iso_currency_code": "USD"}
    )
    assert sync._money_problem({"amount": Decimal("12.34"), "iso_currency_code": "USD"}) is None
    message = sync._money_problem(
        {"amount": 5, "iso_currency_code": None, "unofficial_currency_code": "BTC"}
    )
    assert "BTC" in message


# --------------------------------------------------------------------------- accounts (SYNC-7)


def test_balances_and_accounts_come_from_accounts_get(linked, fake_plaid):
    fake_plaid.set_balances(
        linked.fake_item, linked.account_pid, current="1500.25", available="3499.75", limit=5000
    )
    extra = fake_plaid.add_account(linked.fake_item, name="Gold Card", mask="2002")
    fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    accounts = {a.plaid_account_id: a for a in linked.db.scalars(select(Account))}
    card = accounts[linked.account_pid]
    assert (card.current_balance, card.available_balance, card.credit_limit) == (
        Decimal("1500.25"),
        Decimal("3499.75"),
        Decimal("5000.00"),
    )
    assert card.balances_as_of == NOW and card.mask == "1000"
    assert accounts[extra["account_id"]].name == "Gold Card"  # no transactions, still listed


def test_account_churn_keeps_the_account_and_carries_edits(linked, fake_plaid, db):
    txn = fake_plaid.add_transaction(linked.fake_item, amount="70.00", merchant_name="REI")
    linked.sync()
    account_id = linked.account().id
    edit(db, linked.row(txn["transaction_id"]), notes="Tent")
    new_pid = fake_plaid.churn_account(linked.fake_item, linked.account_pid)
    out = linked.sync()
    accounts = list(db.scalars(select(Account)))
    assert len(accounts) == 1 and accounts[0].id == account_id
    assert accounts[0].plaid_account_id == new_pid and "re-issued" in accounts[0].notice
    (row,) = linked.rows()
    assert row.account_id == account_id and row.notes == "Tent"
    assert (out.added, out.removed) == (1, 1)


def test_an_account_re_issued_without_new_transactions_is_kept_by_accounts_get(
    linked, fake_plaid, db
):
    txn = fake_plaid.add_transaction(linked.fake_item, amount="70.00", merchant_name="REI")
    linked.sync()
    account_id = linked.account().id
    edit(db, linked.row(txn["transaction_id"]), notes="Tent")
    old_pid = linked.account_pid
    new_pid = fake_plaid.churn_account(linked.fake_item, old_pid, redeliver=False)
    quiet = linked.sync()  # no transactions: only /accounts/get shows the new account id
    assert (quiet.added, quiet.removed) == (0, 0)
    accounts = list(db.scalars(select(Account)))
    assert [a.id for a in accounts] == [account_id]
    assert accounts[0].plaid_account_id == new_pid and "re-issued" in accounts[0].notice
    # Plaid re-delivers the history under the new account later: the edits follow the charge.
    fake_plaid.remove_transaction(linked.fake_item, txn["transaction_id"])
    fake_plaid.add_transaction(
        linked.fake_item,
        transaction_id="rei-again",
        account_id=new_pid,
        amount="70.00",
        merchant_name="REI",
        name=txn["name"],
        date=txn["date"],
        authorized_date=txn["authorized_date"],
    )
    later = linked.sync()
    assert (later.added, later.removed) == (1, 1)
    (row,) = linked.rows()
    assert (row.plaid_transaction_id, row.account_id, row.notes) == (
        "rei-again",
        account_id,
        "Tent",
    )


# --------------------------------------------------------------------------- consent (SYNC-6)


def test_consent_and_consented_products_come_from_item_get(linked, fake_plaid):
    linked.fake_item.consent_expiration_time = "2027-04-01T00:00:00Z"
    linked.fake_item.consented_products = ["transactions"]
    linked.fake_item.last_failed_update = "2026-10-19T03:00:00Z"
    linked.sync()
    item = linked.item
    assert item.consent_expires_at == dt.datetime(2027, 4, 1, tzinfo=dt.UTC)
    assert item.consented_products == ["transactions"]
    assert item.plaid_last_update_at == dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.UTC)
    assert item.plaid_last_failed_update_at == dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.UTC)
    assert item.item_snapshot["item"]["item_id"] == "item-amex"
    assert item.liabilities_status == "not_consented"


def test_an_item_error_reported_by_item_get_marks_the_item(linked, fake_plaid, monkeypatch):
    real = fake_plaid.get_item

    def broken(token):
        body = real(token)
        body["item"]["error"] = {
            "error_type": "ITEM_ERROR",
            "error_code": "ITEM_LOGIN_REQUIRED",
            "error_message": "the login details of this item have changed",
            "display_message": "Sign in to Amex again",
        }
        return body

    monkeypatch.setattr(fake_plaid, "get_item", broken)
    out = linked.sync()
    assert out.status == "success" and out.item_status == "needs_reconnect"
    assert linked.item.error_display_message == "Sign in to Amex again"


# --------------------------------------------------------------------------- liabilities (SYNC-7)


def test_liabilities_are_read_only_when_consented(
    db, session_factory, fake_plaid, secrets, settings, notifier, at_now
):
    linked = Linked(
        db, session_factory, fake_plaid, secrets, settings, notifier, consented=("transactions",)
    )
    linked.sync()
    assert fake_plaid.calls_to("get_liabilities") == []
    assert linked.item.liabilities_status == "not_consented"


def test_liabilities_fill_the_card_and_snapshot_the_statement(linked, fake_plaid, db):
    linked.sync()
    account = linked.account()
    assert (account.statement_balance, account.minimum_payment, account.last_payment_amount) == (
        Decimal("1500.00"),
        Decimal("40.00"),
        Decimal("1200.00"),
    )
    assert (account.statement_issue_date, account.payment_due_date, account.is_overdue) == (
        dt.date(2026, 10, 3),
        dt.date(2026, 10, 28),
        False,
    )
    assert account.liabilities_as_of == NOW and linked.item.liabilities_status == "ok"
    (statement,) = db.scalars(select(Statement))
    assert (statement.closing_date, statement.closing_date_source, statement.plaid_issue_date) == (
        dt.date(2026, 10, 3),
        "liabilities",
        dt.date(2026, 10, 3),
    )
    assert (statement.reported_new_balance, statement.reported_new_balance_source) == (
        Decimal("1500.00"),
        "liabilities",
    )
    linked.sync()
    assert db.scalar(select(func.count()).select_from(Statement)) == 1  # upserted, not doubled


def test_statement_snapshots_never_overwrite_what_the_user_entered(linked, fake_plaid, db):
    account = make_account_for(linked)
    typed = make_statement(
        db,
        account,
        "2026-10-05",
        closing_date_source="user",
        reported_new_balance=Decimal("1499.99"),
        reported_new_balance_source="user",
        reported_previous_balance=Decimal("1200.00"),
        notes="From the PDF",
    )
    linked.sync()
    db.expire_all()
    row = db.get(Statement, typed.id)
    assert row.plaid_issue_date == dt.date(2026, 10, 3)  # attached to the nearby statement
    assert (row.closing_date, row.closing_date_source) == (dt.date(2026, 10, 5), "user")
    assert (row.reported_new_balance, row.reported_new_balance_source) == (
        Decimal("1499.99"),
        "user",
    )
    assert (row.reported_previous_balance, row.notes) == (Decimal("1200.00"), "From the PDF")
    assert db.scalar(select(func.count()).select_from(Statement)) == 1


def test_a_snapshot_moves_a_liabilities_closing_date_to_the_issue_date(linked, db):
    account = make_account_for(linked)
    earlier = make_statement(db, account, "2026-10-01", closing_date_source="liabilities")
    linked.sync()
    db.expire_all()
    row = db.get(Statement, earlier.id)
    assert (row.closing_date, row.plaid_issue_date, row.reported_new_balance) == (
        dt.date(2026, 10, 3),
        dt.date(2026, 10, 3),
        Decimal("1500.00"),
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NO_LIABILITY_ACCOUNTS", "unsupported"),
        ("PRODUCTS_NOT_SUPPORTED", "unsupported"),
        ("ADDITIONAL_CONSENT_REQUIRED", "not_consented"),
        ("INSTITUTION_DOWN", "error"),
    ],
)
def test_liabilities_errors_set_the_liabilities_status(linked, fake_plaid, code, expected):
    fake_plaid.queue_error("get_liabilities", fake_plaid.plaid_error(code))
    out = linked.sync()
    assert out.status == "success" and out.item_status == "healthy"
    assert linked.item.liabilities_status == expected


def test_null_liability_fields_are_stored_as_unknown(linked, fake_plaid):
    fake_plaid.set_liabilities(
        linked.fake_item,
        [
            plaid_credit_liability(
                account_id=linked.account_pid,
                last_statement_issue_date=None,
                last_statement_balance=None,
                minimum_payment_amount=None,
                next_payment_due_date=None,
                is_overdue=None,
            )
        ],
    )
    linked.sync()
    account = linked.account()
    assert account.statement_balance is None and account.payment_due_date is None
    assert account.is_overdue is None
    assert linked.db.scalar(select(func.count()).select_from(Statement)) == 0


# --------------------------------------------------------------------------- errors (SYNC-5)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("ITEM_LOGIN_REQUIRED", "needs_reconnect"),
        ("PENDING_DISCONNECT", "needs_reconnect"),
        ("USER_PERMISSION_REVOKED", "needs_reconnect"),
        ("INSTITUTION_DOWN", "error"),
        ("TRANSACTIONS_SYNC_LIMIT", "error"),
        ("INTERNAL_SERVER_ERROR", "error"),
        ("ITEM_NOT_FOUND", "error"),
    ],
)
def test_plaid_errors_map_to_the_item_status(linked, fake_plaid, alert_calls, code, status):
    fake_plaid.add_transaction(linked.fake_item)
    fake_plaid.queue_error(
        "transactions_sync", fake_plaid.plaid_error(code, display_message="Plaid says")
    )
    out = linked.sync()
    assert (out.status, out.error_code, out.item_status) == ("error", code, status)
    item = linked.item
    assert (item.status, item.error_code, item.error_display_message) == (
        status,
        code,
        "Plaid says",
    )
    assert item.transactions_cursor is None and linked.rows() == []
    run = linked.runs()[-1]
    assert (run.status, run.error_code) == ("error", code)
    assert alert_calls[-1] is out
    assert not fake_plaid.calls_to("get_accounts")  # nothing else is asked after a failure


def test_a_success_clears_the_last_error(linked, fake_plaid):
    fake_plaid.queue_error("transactions_sync", fake_plaid.plaid_error("INSTITUTION_DOWN"))
    linked.sync()
    assert linked.item.status == "error"
    linked.sync()
    item = linked.item
    assert (item.status, item.error_code, item.error_message, item.error_display_message) == (
        "healthy",
        None,
        None,
        None,
    )
    assert item.last_sync_success_at == NOW


def test_an_unexpected_failure_is_recorded_as_internal_and_re_raised(
    linked, fake_plaid, alert_calls, monkeypatch
):
    fake_plaid.add_transaction(linked.fake_item)

    def explode(pages):
        raise RuntimeError("boom with access-sandbox-1234567890 inside")

    monkeypatch.setattr(sync, "fold_pages", explode)
    with pytest.raises(RuntimeError):
        linked.sync()
    linked.db.expire_all()
    item = linked.item
    assert (item.status, item.error_code) == ("error", "INTERNAL")
    assert (
        "RuntimeError" in item.error_message
        and "access-sandbox-1234567890" not in item.error_message
    )
    run = linked.runs()[-1]
    assert (run.status, run.error_code) == ("error", "INTERNAL")
    assert alert_calls[-1].internal_error and alert_calls[-1].error_code == "INTERNAL"
    assert linked.rows() == []


def test_a_missing_access_token_is_an_error_not_a_crash(linked, secrets):
    secrets.values.clear()
    out = linked.sync()
    assert (out.status, out.error_code) == ("error", "ACCESS_TOKEN_MISSING")
    assert linked.item.status == "error"


def test_keychain_approval_is_recorded_and_re_raised(linked, secrets):
    secrets.fail_on[("get", None)] = app_secrets.KeychainApprovalNeeded(
        "plaid.access_token", -25308
    )
    with pytest.raises(app_secrets.KeychainApprovalNeeded):
        linked.sync()
    linked.db.expire_all()
    assert linked.item.error_code == "KEYCHAIN_APPROVAL_NEEDED"


# --------------------------------------------------------------------------- locks and bookkeeping


def test_a_concurrent_sync_is_recorded_as_skipped(linked, fake_plaid, session_factory):
    fake_plaid.add_transaction(linked.fake_item)
    engine = session_factory.kw["bind"].engine
    with app_db.sync_lock(engine, linked.item.id) as acquired:
        assert acquired
        out = linked.sync()
    assert out.status == "skipped" and out.ok is False
    assert linked.runs()[-1].status == "skipped"
    assert fake_plaid.calls_to("transactions_sync") == []
    assert linked.sync().added == 1  # the lock was released


def test_older_running_runs_are_marked_interrupted(linked, db):
    stale = make_sync_run(db, linked.item, status="running", started_at=NOW - dt.timedelta(hours=2))
    linked.sync()
    db.expire_all()
    assert db.get(SyncRun, stale.id).status == "interrupted"


def test_a_prepared_running_run_is_reused(linked, db):
    prepared = make_sync_run(db, linked.item, status="running", trigger="manual", started_at=NOW)
    out = linked.sync(trigger="manual", run_id=prepared.id)
    assert out.sync_run_id == prepared.id
    db.expire_all()
    assert [r.status for r in linked.runs()] == ["success"]


def test_the_sync_run_records_the_counts(linked, fake_plaid):
    txn = fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    fake_plaid.modify_transaction(linked.fake_item, txn["transaction_id"], name="CHANGED")
    fake_plaid.add_transaction(linked.fake_item)
    out = linked.sync()
    run = linked.runs()[-1]
    assert (run.added_count, run.modified_count, run.removed_count) == (1, 1, 0)
    assert (run.trigger, run.status, run.update_status) == (
        "cli",
        "success",
        "HISTORICAL_UPDATE_COMPLETE",
    )
    assert (run.started_at, run.finished_at) == (NOW, NOW) and out.sync_run_id == run.id
    assert linked.item.last_sync_attempt_at == NOW


def test_an_unknown_trigger_is_refused(linked):
    with pytest.raises(ValueError):
        linked.sync(trigger="cron")


# --------------------------------------------------------------------------- hooks


def test_reconcile_runs_when_rows_were_inserted(linked, fake_plaid, reconcile_calls):
    fake_plaid.add_transaction(linked.fake_item)
    linked.sync()
    linked.sync()
    assert reconcile_calls == [linked.item.id]


def test_reconcile_runs_when_history_completes_without_new_rows(
    linked, fake_plaid, reconcile_calls
):
    fake_plaid.set_update_status(linked.fake_item, "INITIAL_UPDATE_COMPLETE")
    linked.sync()
    assert reconcile_calls == []
    fake_plaid.set_update_status(linked.fake_item, "HISTORICAL_UPDATE_COMPLETE")
    linked.sync()
    assert reconcile_calls == [linked.item.id]


def test_reconcile_failure_rolls_the_whole_batch_back(linked, fake_plaid, monkeypatch):
    fake_plaid.add_transaction(linked.fake_item)

    def broken(session, item_id):
        raise RuntimeError("reconcile bug")

    monkeypatch.setattr(reconcile, "reconcile_item", broken)
    with pytest.raises(RuntimeError):
        linked.sync()
    assert linked.rows() == [] and linked.item.transactions_cursor is None


def test_the_alerts_hook_gets_the_outcome(linked, fake_plaid, alert_calls):
    txn = fake_plaid.add_transaction(linked.fake_item)
    out = linked.sync(trigger="schedule")
    assert alert_calls == [out]
    assert out.inserted_transaction_ids == (linked.row(txn["transaction_id"]).id,)
    assert (out.trigger, out.item_status, out.initial_history_load) == ("schedule", "healthy", True)


# ---------------------------------------------------- CSV reconciliation, end to end (SYNC-8)
# The real reconcile module inside the apply transaction (ARCHITECTURE §6): CSV rows pair with the
# Plaid rows a sync inserts, the sync that completes Plaid's history holds the CSV rows Plaid
# doesn't have, and a pending charge that posts pairs with the CSV row held while it was pending.


def amex_reference(posted: str, n: int) -> str:
    """An Amex Reference that embeds the posting date ``posted`` (YYYYDDD, RESEARCH T4.3b)."""
    day = dt.date.fromisoformat(posted)
    return f"3{day.year}{day.timetuple().tm_yday:03d}{n:010d}"


def csv_row(db, account, n: int, *, amount: str, date: str, posted: str, name: str, **kwargs):
    """An imported Amex CSV row on the card, as ``csv_import`` stores it."""
    return make_transaction(
        db,
        account,
        source="csv",
        amount=amount,
        date=date,
        posted_date=posted,
        name=name,
        original_description=name,
        csv_reference=amex_reference(posted, n),
        **kwargs,
    )


def test_the_sync_that_completes_the_history_holds_the_unpaired_csv_rows(linked, fake_plaid, db):
    account = make_account_for(linked)
    rows = {
        "twin": csv_row(
            db, account, 1, amount="25.00", date="2026-10-09", posted="2026-10-10", name="TARTINE"
        ),
        "missing": csv_row(
            db, account, 2, amount="40.00", date="2026-09-20", posted="2026-09-21", name="KIOSK"
        ),
        "older": csv_row(
            db, account, 3, amount="15.00", date="2024-09-01", posted="2024-09-02", name="CAFE"
        ),
    }
    ids = {key: row.id for key, row in rows.items()}
    september = periods.month_period(2026, 9)
    fake_plaid.set_update_status(linked.fake_item, "INITIAL_UPDATE_COMPLETE")
    plaid = fake_plaid.add_transaction(
        linked.fake_item,
        amount="25.00",
        date="2026-10-10",
        authorized_date="2026-10-09",
        name="TARTINE BAKERY",
        merchant_name="Tartine Bakery",
    )
    first = linked.sync()
    assert first.added == 1 and not first.history_completed_now
    assert db.get(Transaction, ids["twin"]) is None  # paired by the sync that inserted its twin
    assert linked.row(plaid["transaction_id"]).csv_reference == amex_reference("2026-10-10", 1)
    assert db.get(Transaction, ids["missing"]).duplicate_status == "none"  # history incomplete
    assert spending.period_spending(db, september).total == Decimal("40.00")

    fake_plaid.add_transaction(linked.fake_item, amount="7.00", date="2024-11-02")
    fake_plaid.set_update_status(linked.fake_item, "HISTORICAL_UPDATE_COMPLETE")
    second = linked.sync()
    assert second.history_completed_now
    assert linked.account().plaid_history_start == dt.date(2024, 11, 2)
    # held by this same sync: out of every total at once, listed on /import
    assert db.get(Transaction, ids["missing"]).duplicate_status == "suspect"
    assert db.get(Transaction, ids["older"]).duplicate_status == "none"  # before Plaid's history
    assert spending.period_spending(db, september).total == Decimal("0.00")
    assert [(s.id, s.pending) for s in csv_import.list_suspects(db)] == [(ids["missing"], False)]


def test_completing_the_history_without_new_rows_still_holds_them(linked, fake_plaid, db):
    account = make_account_for(linked)
    fake_plaid.set_update_status(linked.fake_item, "INITIAL_UPDATE_COMPLETE")
    fake_plaid.add_transaction(linked.fake_item, amount="5.00", date="2026-08-01")
    linked.sync()
    row = csv_row(db, account, 5, amount="40.00", date="2026-09-20", posted="2026-09-21", name="X")
    fake_plaid.set_update_status(linked.fake_item, "HISTORICAL_UPDATE_COMPLETE")
    out = linked.sync()
    assert (out.added, out.history_completed_now) == (0, True)
    db.expire_all()
    assert row.duplicate_status == "suspect"


@pytest.mark.parametrize("decision", ["held", "kept"])
def test_a_pending_charge_that_posts_pairs_with_the_csv_row_held_for_it(
    linked, fake_plaid, db, decision
):
    account = make_account_for(linked)
    fake_plaid.add_transaction(linked.fake_item, amount="60.00", date="2026-09-01")
    pending = fake_plaid.add_pending(
        linked.fake_item,
        amount="18.50",
        date="2026-10-14",
        name="SWEETGREEN",
        merchant_name="Sweetgreen",
    )
    row = csv_row(
        db,
        account,
        6,
        amount="18.50",
        date="2026-10-14",
        posted="2026-10-15",
        name="SWEETGREEN SAN FRANCISCO CA",
        notes="Lunch with Sam",
    )
    row_id = row.id
    first = linked.sync()  # history complete, but Plaid's twin is still pending: held
    assert first.history_completed_now
    db.expire_all()
    assert db.get(Transaction, row_id).duplicate_status == "suspect"
    (held,) = csv_import.list_suspects(db)
    assert (held.id, held.pending, held.pending_name) == (row_id, True, "Sweetgreen")
    if decision == "kept":
        assert csv_import.keep_suspect(db, row_id)
        db.flush()

    fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], date="2026-10-15")
    linked.sync()
    posted = linked.row(f"{pending['transaction_id']}-posted")
    assert db.get(Transaction, row_id) is None  # merged into the posted charge
    assert (posted.csv_reference, posted.notes, posted.duplicate_status) == (
        amex_reference("2026-10-15", 6),
        "Lunch with Sam",
        "none",
    )
    assert csv_import.list_suspects(db) == []
    assert [t.amount for t in linked.rows()] == [Decimal("60.00"), Decimal("18.50")]


# --------------------------------------------------------------------------- unit pieces


def test_fold_pages_keeps_the_last_occurrence_and_removed_wins_within_a_page():
    a1 = {"transaction_id": "a", "account_id": "acc", "name": "A1"}
    a2 = {"transaction_id": "a", "account_id": "acc", "name": "A2"}
    b = {"transaction_id": "b", "account_id": "acc", "name": "B"}
    pages = [
        {
            "added": [a1, b],
            "modified": [],
            "removed": [],
            "accounts": [{"account_id": "acc", "name": "x"}],
        },
        {
            "added": [],
            "modified": [a2],
            "removed": [{"transaction_id": "b", "account_id": "acc"}],
            "accounts": [{"account_id": "acc", "name": "y"}],
            "next_cursor": "c2",
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
        },
        {
            "added": [b],
            "modified": [],
            "removed": [{"transaction_id": "b", "account_id": "acc"}],
            "accounts": [],
            "next_cursor": "c3",
        },
    ]
    batch = sync.fold_pages(pages)
    assert batch.payloads == {"a": a2}
    assert batch.removed == {"b": "acc"}
    assert batch.accounts["acc"]["name"] == "y"
    assert (batch.next_cursor, batch.update_status) == ("c3", None)


def test_description_similarity():
    assert (
        sync.description_similarity("Starbucks", "STARBUCKS 1234", "Starbucks", "STARBUCKS #99")
        == 1.0
    )
    assert sync.description_similarity(None, "UBER TRIP HELP.UBER.COM", None, "UBER TRIP") >= 0.9
    assert sync.description_similarity("Lyft", "LYFT", "Starbucks", "STARBUCKS") < 0.5


def test_timestamps_from_plaid():
    assert sync.parse_timestamp("2026-10-15T06:00:00Z") == dt.datetime(
        2026, 10, 15, 6, tzinfo=dt.UTC
    )
    assert sync.parse_timestamp(None) is None
    assert sync.parse_timestamp("not a time") is None
    assert sync.parse_timestamp("2026-10-15T06:00:00") == dt.datetime(
        2026, 10, 15, 6, tzinfo=dt.UTC
    )


def test_describe_error_has_words_for_the_usual_codes():
    assert "sign in again" in sync.describe_error("ITEM_LOGIN_REQUIRED")
    assert "slow down" in sync.describe_error("TRANSACTIONS_SYNC_LIMIT")
    assert "isn't answering" in sync.describe_error("INSTITUTION_NOT_RESPONDING")
    assert sync.describe_error(None) is None
    assert sync.describe_error("SOMETHING_NEW") is None


def test_a_has_user_state_query_ignores_json_null(db, linked):
    account = make_account_for(linked)
    make_tombstone(db, account, plaid_transaction_id="json-null", user_state=None)
    make_tombstone(
        db,
        account,
        plaid_transaction_id="edited",
        user_state=userstate.to_json(userstate.UserState(notes="x")),
    )
    assert (
        db.scalar(text("SELECT count(*) FROM transaction_tombstones WHERE user_state IS NOT NULL"))
        == 2
    )
    found = db.scalars(
        select(TransactionTombstone.plaid_transaction_id).where(sync.has_user_state())
    ).all()
    assert found == ["edited"]


# --------------------------------------------------------------------------- more edge cases


def test_a_posted_row_replaces_a_pending_row_plaid_has_not_removed_yet(linked, fake_plaid, db):
    pending = fake_plaid.add_pending(linked.fake_item, amount="12.00", merchant_name="Sweetgreen")
    linked.sync()
    edit(db, linked.row(pending["transaction_id"]), notes="Salad")
    fake_plaid.page_size = 1  # the pending row's change comes on a page before the posted row
    fake_plaid.modify_transaction(linked.fake_item, pending["transaction_id"], amount="12.50")
    posted = fake_plaid.add_transaction(
        linked.fake_item,
        amount="12.50",
        merchant_name="Sweetgreen",
        pending_transaction_id=pending["transaction_id"],
    )
    out = linked.sync()
    assert (out.added, out.modified, out.removed) == (1, 0, 1)
    assert [r.plaid_transaction_id for r in linked.rows()] == [posted["transaction_id"]]
    assert linked.row(posted["transaction_id"]).notes == "Salad"
    fake_plaid.remove_transaction(linked.fake_item, pending["transaction_id"])  # Plaid catches up
    late = linked.sync()
    assert (late.added, late.removed) == (0, 0) and len(linked.rows()) == 1


def test_edits_never_jump_between_a_charge_and_a_credit(linked, fake_plaid, db):
    charge = fake_plaid.add_transaction(linked.fake_item, amount="0.40", merchant_name="Parking")
    linked.sync()
    edit(db, linked.row(charge["transaction_id"]), notes="Meter")
    fake_plaid.remove_transaction(linked.fake_item, charge["transaction_id"])
    credit = fake_plaid.add_transaction(linked.fake_item, amount="-0.40", merchant_name="Parking")
    linked.sync()
    assert linked.row(credit["transaction_id"]).notes is None


def test_a_row_that_replaced_its_pending_row_is_not_matched_to_other_lost_edits(
    linked, fake_plaid, db
):
    pending = fake_plaid.add_pending(linked.fake_item, amount="25.00", merchant_name="Trader Joe's")
    other = fake_plaid.add_transaction(
        linked.fake_item, amount="25.00", merchant_name="Trader Joe's"
    )
    linked.sync()
    edit(db, linked.row(pending["transaction_id"]), notes="Mine")
    edit(db, linked.row(other["transaction_id"]), notes="Someone else's")
    fake_plaid.remove_transaction(linked.fake_item, other["transaction_id"])
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"])
    linked.sync()
    assert linked.row(posted["transaction_id"]).notes == "Mine"
    assert linked.tombstone(other["transaction_id"]).consumed_by_transaction_id is None


def test_the_cursor_compare_and_set_rolls_back_a_batch_whose_cursor_moved(
    linked, fake_plaid, monkeypatch
):
    from sqlalchemy import update

    fake_plaid.add_transaction(linked.fake_item, transaction_id="once")
    moves: list[int] = []

    def meddle(session, item_id):
        if not moves:  # someone stores a cursor inside the apply transaction, once
            moves.append(item_id)
            session.execute(
                update(PlaidItem).where(PlaidItem.id == item_id).values(transactions_cursor="x")
            )
        return reconcile.ReconcileResult()

    monkeypatch.setattr(reconcile, "reconcile_item", meddle)
    out = linked.sync()
    assert out.status == "success" and out.added == 1
    assert len(fake_plaid.calls_to("transactions_sync")) == 2  # refetched after the rollback
    assert linked.item.transactions_cursor not in (None, "x")
    assert len(linked.rows()) == 1


# --------------------------------------------------------------------------- new charges (ALR-3)
# SyncOutcome.inserted_transaction_ids lists only charges the app had not seen before, so an
# ALR-3 evaluator built to its contract alerts once per charge (ARCHITECTURE §11).

RECENT = NOW.date() - dt.timedelta(days=2)


@pytest.fixture
def alr3(monkeypatch) -> dict[str, int]:
    """An evaluator that follows the documented ALR-3 contract (alerts.evaluate_after_sync): rows
    in outcome.inserted_transaction_ids, dated within 7 days, never on an initial history load,
    keyed on the pending id the posted row names (else its own id). Counts alerts per key."""
    sent: dict[str, int] = {}

    def evaluate(session, notifier, outcome):
        if outcome.status != "success" or outcome.initial_history_load:
            return []
        for tid in outcome.inserted_transaction_ids:
            row = session.get(Transaction, tid)
            if row is None or row.amount < Decimal("1000"):
                continue
            if (NOW.date() - (row.authorized_date or row.date)).days > 7:
                continue
            key = row.pending_transaction_id or row.plaid_transaction_id
            sent[key] = sent.get(key, 0) + 1
        return []

    monkeypatch.setattr(alerts, "evaluate_after_sync", evaluate)
    return sent


def test_a_re_delivery_after_account_churn_is_not_a_new_charge(linked, fake_plaid, alr3):
    """RESEARCH T1.10: a card replacement can re-issue the account and re-deliver its history
    under new ids. The same charge must not alert again."""
    linked.sync()  # history complete from here on
    big = fake_plaid.add_transaction(
        linked.fake_item, amount="2500.00", date=RECENT, name="APPLE STORE", merchant_name="Apple"
    )
    first = linked.sync()
    assert first.inserted_transaction_ids == (linked.row(big["transaction_id"]).id,)
    new_pid = fake_plaid.churn_account(linked.fake_item, linked.account_pid)
    churn = linked.sync()
    again = linked.row(f"{big['transaction_id']}-{new_pid}")
    assert churn.added == 1 and churn.inserted_transaction_ids == ()
    assert linked.tombstone(big["transaction_id"]).consumed_by_transaction_id == again.id
    assert alr3 == {big["transaction_id"]: 1}


def test_an_unlinked_posting_is_not_a_new_charge(linked, fake_plaid, alr3):
    """RESEARCH T1.4: Amex may leave pending_transaction_id null. With no edits to carry, the
    posted row is still matched to the pending row it replaces."""
    linked.sync()
    pending = fake_plaid.add_pending(
        linked.fake_item, amount="1800.00", date=RECENT, name="DELTA AIR", merchant_name="Delta"
    )
    first = linked.sync()
    assert first.inserted_transaction_ids == (linked.row(pending["transaction_id"]).id,)
    posted = fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], link=False)
    second = linked.sync()
    row = linked.row(posted["transaction_id"])
    assert row.pending_transaction_id is None and second.inserted_transaction_ids == ()
    assert linked.tombstone(pending["transaction_id"]).consumed_by_transaction_id == row.id
    assert alr3 == {pending["transaction_id"]: 1}


def test_a_linked_posting_is_not_a_new_charge_either(linked, fake_plaid, alr3):
    linked.sync()
    pending = fake_plaid.add_pending(linked.fake_item, amount="1200.00", date=RECENT, name="HOTEL")
    linked.sync()
    fake_plaid.post_pending(linked.fake_item, pending["transaction_id"], amount="1250.00")
    out = linked.sync()
    assert out.added == 1 and out.inserted_transaction_ids == ()
    assert alr3 == {pending["transaction_id"]: 1}


def test_a_genuinely_new_charge_beside_an_account_churn_stays_new(linked, fake_plaid):
    fake_plaid.add_transaction(linked.fake_item, amount="12.00", date=RECENT, name="BLUE BOTTLE")
    linked.sync()
    fake_plaid.churn_account(linked.fake_item, linked.account_pid)
    fresh = fake_plaid.add_transaction(
        linked.fake_item, amount="1500.00", date=RECENT, name="BEST BUY", merchant_name="Best Buy"
    )
    out = linked.sync()
    assert out.added == 2
    assert out.inserted_transaction_ids == (linked.row(fresh["transaction_id"]).id,)


def test_matching_a_large_churn_compares_only_nearby_charges(linked, fake_plaid, db, monkeypatch):
    """An account churn re-delivers the whole history. The matcher compares a removed charge only
    with new rows of the same account and sign within 7 days, not with every row (2,600 rows were
    6.8 million comparisons, about 18 s)."""
    for n in range(600):
        fake_plaid.add_transaction(
            linked.fake_item,
            amount=f"{(n % 40) + 3}.{(n * 7) % 100:02d}",
            date=dt.date(2026, 10, 1) - dt.timedelta(days=n % 300),
            name=f"MERCHANT {n % 60}",
            merchant_name=f"Merchant {n % 60}",
        )
    linked.sync()
    calls = {"n": 0}
    real = sync.replacement_score

    def counting(tomb, txn):
        calls["n"] += 1
        return real(tomb, txn)

    monkeypatch.setattr(sync, "replacement_score", counting)
    fake_plaid.churn_account(linked.fake_item, linked.account_pid)
    out = linked.sync()
    assert out.added == 600 and out.inserted_transaction_ids == ()
    assert 0 < calls["n"] < 600 * 50  # every pair would be 360,000
    unmatched = db.scalar(
        select(func.count())
        .select_from(TransactionTombstone)
        .where(TransactionTombstone.consumed_by_transaction_id.is_(None))
    )
    assert unmatched == 0


def test_not_ready_leaves_the_last_success_alone_until_data_arrives(
    linked, fake_plaid, session_factory
):
    """No data has arrived yet: --if-due keeps polling (no 5-hour pause after a NOT_READY run),
    and the 36-hour stale check counts from linking."""
    fake_plaid.set_update_status(linked.fake_item, "NOT_READY")
    assert linked.sync().status == "not_ready"
    assert linked.item.last_sync_success_at is None
    fake_plaid.set_update_status(linked.fake_item, "HISTORICAL_UPDATE_COMPLETE")
    fake_plaid.add_transaction(linked.fake_item, amount="5.00")
    result = sync.sync_all(
        session_factory,
        fake_plaid,
        linked.store,
        linked.settings,
        trigger="schedule",
        notifier=linked.notifier,
        if_due=True,
        sleep=no_sleep,
    )
    assert result.not_due == () and [o.added for o in result.outcomes] == [1]
    linked.db.expire_all()
    assert linked.db.get(PlaidItem, linked.item.id).last_sync_success_at == NOW
