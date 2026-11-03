import datetime as dt
from decimal import Decimal

import pytest

from budgetapp.services import userstate
from budgetapp.services.userstate import SplitState, UserState
from tests.factories import (
    get_category,
    make_account,
    make_split,
    make_transaction,
)

D = Decimal


def userstate_card_payments_id(db):
    from budgetapp.services.categorize import system_category_id

    return system_category_id(db, "card_payments")


def _splits(txn):
    return [(s.category_id, s.amount) for s in txn.splits]


class TestRescale:
    @pytest.mark.parametrize(
        ("parts", "new_total"),
        [
            (["60.00", "40.00"], "104.50"),
            (["33.33", "33.33", "33.34"], "10.00"),
            (["0.01", "99.99"], "50.00"),
            (["10.00", "20.00", "30.00"], "61.01"),
            (["12.34", "56.78"], "-69.12"),
            (["1.00", "1.00", "1.00"], "0.10"),
            (["70.00", "30.00"], "1.00"),
        ],
    )
    def test_sums_exactly(self, parts, new_total):
        splits = tuple(SplitState(i + 1, D(p)) for i, p in enumerate(parts))
        result = userstate.rescale_splits(splits, D(new_total))
        assert sum(s.amount for s in result) == D(new_total)
        assert all(s.amount != 0 for s in result)

    def test_leftover_cents_go_to_the_largest_split(self):
        splits = (SplitState(1, D("33.33")), SplitState(2, D("33.34")), SplitState(3, D("33.33")))
        result = userstate.rescale_splits(splits, D("10.00"))
        assert [s.amount for s in result] == [D("3.33"), D("3.34"), D("3.33")]
        # 33.33 × 10.01 / 100 = 3.3363 rounds to 3.34; the largest split absorbs the remainder.
        result = userstate.rescale_splits(splits, D("10.01"))
        assert [s.amount for s in result] == [D("3.34"), D("3.33"), D("3.34")]

    def test_proportional(self):
        result = userstate.rescale_splits((SplitState(1, D("60")), SplitState(2, D("40"))), D("50"))
        assert [s.amount for s in result] == [D("30.00"), D("20.00")]

    def test_impossible_rescale_returns_nothing(self):
        assert (
            userstate.rescale_splits((SplitState(1, D("5")), SplitState(2, D("-5"))), D("1")) == ()
        )
        assert userstate.rescale_splits((SplitState(1, D("5")),), D("0")) == ()

    def test_unchanged_total_returns_the_same_splits(self):
        splits = (SplitState(1, D("6.00")), SplitState(2, D("4.00")))
        assert userstate.rescale_splits(splits, D("10.00")) == splits


class TestCaptureApply:
    def test_untouched_row_has_no_state(self, db):
        assert userstate.capture(make_transaction(db)) is None

    def test_capture_everything_the_user_owns(self, db):
        groceries = get_category(db, "Groceries")
        home = get_category(db, "Home")
        txn = make_transaction(
            db,
            amount="100.00",
            category=groceries,
            category_source="user",
            notes="weekly shop",
            tags=["Food", "food", " family "],
        )
        make_split(db, txn, groceries, "60.00", note="food")
        make_split(db, txn, home, "40.00")
        state = userstate.capture(txn)
        assert state.category_id == groceries.id
        assert state.notes == "weekly shop"
        assert state.tags == ("Food", "family")
        assert [(s.category_id, s.amount, s.note) for s in state.splits] == [
            (groceries.id, D("60.00"), "food"),
            (home.id, D("40.00"), None),
        ]
        assert state.source_amount == D("100.00")

    def test_automatic_category_is_not_user_state(self, db):
        txn = make_transaction(
            db, category=get_category(db, "Groceries"), category_source="plaid", notes="hi"
        )
        state = userstate.capture(txn)
        assert state.category_id is None and state.notes == "hi"

    def test_apply_to_an_equal_amount_copies_splits(self, db, freeze_today):
        freeze_today("2026-10-20")
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        source = make_transaction(db, amount="100.00", category=groceries, category_source="user")
        make_split(db, source, groceries, "60.00")
        make_split(db, source, home, "40.00")
        state = userstate.capture(source)
        target = make_transaction(db, amount="100.00", tags=["existing"])
        flagged = userstate.apply(target, state)
        db.flush()
        assert flagged is False and target.needs_review is False
        assert target.category_id == groceries.id and target.category_source == "user"
        assert _splits(target) == [(groceries.id, D("60.00")), (home.id, D("40.00"))]
        assert target.tags == ["existing"]
        assert target.user_edited_at is not None

    def test_apply_to_a_different_amount_rescales_and_flags(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        state = UserState(
            splits=(SplitState(groceries.id, D("60.00")), SplitState(home.id, D("40.00"))),
            source_amount=D("100.00"),
        )
        target = make_transaction(db, amount="104.50")
        assert userstate.apply(target, state) is True
        db.flush()
        assert sum(s.amount for s in target.splits) == D("104.50")
        assert _splits(target) == [(groceries.id, D("62.70")), (home.id, D("41.80"))]
        assert target.needs_review is True
        assert "rescaled" in target.review_reason

    def test_pending_target_takes_the_largest_split_category(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        state = UserState(
            splits=(SplitState(groceries.id, D("30.00")), SplitState(home.id, D("70.00")))
        )
        target = make_transaction(db, amount="100.00", pending=True)
        assert userstate.apply(target, state) is True
        db.flush()
        assert target.splits == []
        assert target.category_id == home.id and target.category_source == "user"
        assert target.needs_review is True

    def test_unscalable_splits_collapse(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        state = UserState(
            splits=(SplitState(groceries.id, D("50.00")), SplitState(home.id, D("50.00")))
        )
        target = make_transaction(db, amount="0.00")
        assert userstate.apply(target, state) is True
        assert target.splits == [] and target.category_id == groceries.id

    def test_notes_are_never_lost(self, db):
        target = make_transaction(db, notes="mine")
        userstate.apply(target, UserState(notes="carried"))
        assert target.notes == "carried\n\nmine"
        userstate.apply(target, UserState(notes="carried\n\nmine"))
        assert target.notes == "carried\n\nmine"

    def test_include_category_false(self, db):
        target = make_transaction(db)
        userstate.apply(
            target,
            UserState(category_id=get_category(db, "Home").id, notes="n"),
            include_category=False,
        )
        assert target.category_source == "default" and target.notes == "n"

    def test_a_payment_target_never_takes_a_carried_category_or_splits(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        payments = db.get(type(home), userstate_card_payments_id(db))
        target = make_transaction(db, amount="-100.00", category=payments, category_source="system")
        state = UserState(
            category_id=home.id,
            notes="carried",
            splits=(SplitState(groceries.id, D("-60.00")), SplitState(home.id, D("-40.00"))),
        )
        assert userstate.apply(target, state) is True
        db.flush()
        assert (target.category_id, target.category_source) == (payments.id, "system")
        assert target.splits == [] and target.notes == "carried"
        assert "category you chose and the splits" in target.review_reason

    def test_a_carried_card_payments_choice_on_a_payment_is_no_loss(self, db):
        payments_id = userstate_card_payments_id(db)
        target = make_transaction(
            db,
            amount="-100.00",
            category=db.get(type(get_category(db, "Home")), payments_id),
            category_source="system",
        )
        assert userstate.apply(target, UserState(category_id=payments_id)) is False
        assert target.needs_review is False

    def test_the_targets_own_splits_are_kept(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        target = make_transaction(db, amount="100.00")
        make_split(db, target, groceries, "70.00")
        make_split(db, target, home, "30.00")
        state = UserState(
            category_id=home.id,
            splits=(SplitState(groceries.id, D("50.00")), SplitState(home.id, D("50.00"))),
        )
        userstate.apply(target, state)
        db.flush()
        assert _splits(target) == [(groceries.id, D("70.00")), (home.id, D("30.00"))]
        assert target.category_source == "default"

    def test_splits_still_land_when_the_category_was_already_carried(self, db):
        """decide(inherited=…) may have set the same user category first."""
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        target = make_transaction(db, amount="100.00", category=home, category_source="user")
        state = UserState(
            category_id=home.id,
            splits=(SplitState(groceries.id, D("60.00")), SplitState(home.id, D("40.00"))),
        )
        userstate.apply(target, state)
        db.flush()
        assert _splits(target) == [(groceries.id, D("60.00")), (home.id, D("40.00"))]

    def test_apply_nothing(self, db):
        target = make_transaction(db)
        assert userstate.apply(target, None) is False
        assert userstate.apply(target, UserState()) is False
        assert target.user_edited_at is None

    def test_fit_splits_after_plaid_changes_the_amount(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        txn = make_transaction(db, amount="100.00")
        make_split(db, txn, groceries, "60.00")
        make_split(db, txn, home, "40.00")
        assert userstate.fit_splits_to_amount(txn) is False
        txn.amount = D("95.00")
        assert userstate.fit_splits_to_amount(txn) is True
        db.flush()
        assert sum(s.amount for s in txn.splits) == D("95.00")
        assert txn.needs_review

    def test_split_moves_between_accounts(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        manual = make_account(db, manual=True)
        csv_row = make_transaction(db, manual, source="csv", amount="20.00")
        make_split(db, csv_row, groceries, "5.00")
        make_split(db, csv_row, home, "15.00")
        plaid_row = make_transaction(db, amount="20.00")
        userstate.apply(plaid_row, userstate.capture(csv_row))
        db.delete(csv_row)
        db.flush()
        assert _splits(plaid_row) == [(groceries.id, D("5.00")), (home.id, D("15.00"))]


class TestPairingRecipe:
    """The CSV ↔ Plaid pairing order from the ``services/reconcile.py`` contract."""

    def test_documented_order_moves_state_and_reference(self, db):
        from sqlalchemy.exc import IntegrityError

        from budgetapp.services.categorize import Categorizer, apply_carried_state

        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        account = make_account(db)
        csv_row = make_transaction(db, account, source="csv", amount="20.00", notes="csv note")
        make_split(db, csv_row, groceries, "5.00")
        make_split(db, csv_row, home, "15.00")
        plaid_row = make_transaction(db, account, amount="20.00")
        reference = csv_row.csv_reference

        # The tempting order (set the reference, delete, one flush) violates the unique key.
        with (
            pytest.raises(IntegrityError, match="uq_transactions_csv_reference"),
            db.begin_nested(),
        ):
            plaid_row.csv_reference = reference
            db.delete(csv_row)
            db.flush()
        db.expire_all()

        state = userstate.capture(csv_row)  # 1. capture first (the delete removes the splits)
        db.delete(csv_row)  # 2. delete and flush
        db.flush()
        plaid_row.csv_reference = reference  # 3. copy the reference, then move the state
        apply_carried_state(Categorizer.load(db), plaid_row, state)
        db.flush()
        assert plaid_row.csv_reference == reference and plaid_row.notes == "csv note"
        assert _splits(plaid_row) == [(groceries.id, D("5.00")), (home.id, D("15.00"))]


class TestMergeAndJson:
    def test_merge(self):
        stamp1 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        stamp2 = dt.datetime(2026, 10, 2, tzinfo=dt.UTC)
        primary = UserState(category_id=1, notes="a", tags=("x",), user_edited_at=stamp1)
        secondary = UserState(
            category_id=2,
            notes="b",
            tags=("X", "y"),
            splits=(SplitState(3, D("1.00")), SplitState(4, D("2.00"))),
            source_amount=D("3.00"),
            user_edited_at=stamp2,
        )
        merged = userstate.merge(primary, secondary)
        assert merged.category_id == 1
        assert merged.notes == "a\n\nb"
        assert merged.tags == ("x", "y")
        assert merged.splits == secondary.splits and merged.source_amount == D("3.00")
        assert merged.user_edited_at == stamp2
        assert userstate.merge(None, secondary) is secondary
        assert userstate.merge(primary, None) is primary
        assert userstate.merge(UserState(), None) is None
        assert userstate.merge(UserState(notes="same"), UserState(notes="same")).notes == "same"

    def test_json_round_trip_is_exact(self):
        state = UserState(
            category_id=7,
            notes="dinner",
            tags=("work",),
            splits=(SplitState(1, D("0.10"), "a"), SplitState(2, D("12345678.90"))),
            source_amount=D("12345679.00"),
            user_edited_at=dt.datetime(2026, 10, 1, 12, 30, tzinfo=dt.UTC),
        )
        data = userstate.to_json(state)
        assert data["splits"][0]["amount"] == "0.10"
        assert userstate.from_json(data) == state
        assert userstate.to_json(None) is None and userstate.to_json(UserState()) is None
        assert userstate.from_json(None) is None and userstate.from_json({}) is None

    def test_remap_category_ids(self):
        data = userstate.to_json(
            UserState(category_id=5, splits=(SplitState(5, D("1.00")), SplitState(6, D("2.00"))))
        )
        remapped = userstate.remap_category_ids(data, 5, 9)
        assert remapped["category_id"] == 9
        assert [s["category_id"] for s in remapped["splits"]] == [9, 6]
        assert data["category_id"] == 5  # the input is not modified
