"""services.transactions: edits (TXN-2), splits (TXN-4), one-click rules (CAT-3) and details."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from budgetapp import periods
from budgetapp.db import LEDGER_KEY, LOCK_NS_GLOBAL
from budgetapp.models import Rule, Transaction
from budgetapp.services import categorize, spending, userstate
from budgetapp.services import transactions as txns
from budgetapp.services.transactions import TransactionEditError
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_csv_import,
    make_rule,
    make_split,
    make_tombstone,
    make_transaction,
    uncategorized,
)

NOW = dt.datetime(2026, 10, 29, 17, 30, tzinfo=dt.UTC)
OCTOBER = periods.month_period(2026, 10)


@pytest.fixture
def card(db):
    return make_account(db)


@pytest.fixture
def cats(db):
    return {
        "groceries": get_category(db, "Groceries"),
        "dining": get_category(db, "Dining & Drinks"),
        "shopping": get_category(db, "Shopping"),
        "home": get_category(db, "Home"),
        "personal": get_category(db, "Personal Care"),
        "payments": card_payments(db),
        "uncategorized": uncategorized(db),
    }


@pytest.fixture
def at_now(freeze_today):
    freeze_today(now=NOW)
    return NOW


def holds_ledger_lock(db) -> bool:
    count = db.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid::bigint = :ns AND objid::bigint = :key "
            "AND objsubid = 2"
        ),
        {"ns": LOCK_NS_GLOBAL, "key": LEDGER_KEY},
    )
    return bool(count)


def reload(db, txn: Transaction) -> Transaction:
    db.expire_all()
    return db.get(Transaction, txn.id)


# --------------------------------------------------------------------------- update (TXN-2)


class TestCategoryEdit:
    def test_a_category_choice_is_the_users(self, db, card, cats, at_now):
        rule = make_rule(db, cats["dining"], merchant_pattern="BLUE")
        txn = make_transaction(
            db,
            card,
            category=cats["dining"],
            category_source="rule",
            rule_id=rule.id,
            needs_review=True,
            review_reason="Check the category",
        )
        row = txns.update(db, txn.id, category_id=cats["groceries"].id)
        assert (row.category.id, row.category_source, row.rule_id) == (
            cats["groceries"].id,
            "user",
            None,
        )
        assert row.user_edited_at == NOW and not row.needs_review and row.review_reason is None
        stored = reload(db, txn)
        assert (stored.category_id, stored.category_source) == (cats["groceries"].id, "user")

    def test_the_category_can_arrive_as_form_text(self, db, card, cats):
        txn = make_transaction(db, card)
        assert txns.update(db, txn.id, category_id=f" {cats['home'].id} ").category.name == "Home"

    def test_confirming_the_same_category_makes_it_the_users(self, db, card, cats, at_now):
        txn = make_transaction(db, card, category=cats["dining"], category_source="plaid")
        row = txns.update(db, txn.id, category_id=cats["dining"].id)
        assert row.category_source == "user" and row.user_edited_at == NOW

    def test_nothing_changes_nothing_is_stamped(self, db, card, cats):
        txn = make_transaction(db, card, category=cats["dining"], category_source="user")
        row = txns.update(db, txn.id, category_id=cats["dining"].id, notes=None, tags=[])
        assert row.user_edited_at is None

    def test_a_card_payment_can_be_overridden(self, db, card, cats):
        txn = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-500.00",
            category=cats["payments"],
            category_source="system",
        )
        row = txns.update(db, txn.id, category_id=cats["shopping"].id)
        assert row.category_source == "user" and row.category.name == "Shopping"

    @pytest.mark.parametrize("value", ["", "abc", "0", "-3", "999999999", "²", "\x00"])
    def test_bad_categories_are_refused(self, db, card, cats, value):
        txn = make_transaction(db, card, category=cats["dining"])
        with pytest.raises(TransactionEditError) as err:
            txns.update(db, txn.id, category_id=value, notes="kept?")
        assert set(err.value.errors) == {"category_id"}
        stored = reload(db, txn)
        assert stored.category_id == cats["dining"].id and stored.notes is None

    def test_a_split_row_keeps_its_review_flag(self, db, card, cats):
        txn = make_transaction(
            db, card, amount="10.00", needs_review=True, review_reason="Splits rescaled"
        )
        make_split(db, txn, cats["groceries"], "5.00")
        make_split(db, txn, cats["home"], "5.00")
        assert txns.update(db, txn.id, category_id=cats["dining"].id).needs_review

    def test_missing_transaction(self, db):
        with pytest.raises(txns.TransactionNotFound):
            txns.update(db, 987654321, notes="x")

    @pytest.mark.parametrize(
        "edit", ["category", "notes", "reset", "clear_review", "set_splits", "clear_splits"]
    )
    def test_every_edit_takes_the_ledger_lock(self, db, card, cats, edit):
        txn = make_transaction(
            db, card, amount="20.00", category=cats["groceries"], category_source="user"
        )
        assert not holds_ledger_lock(db)
        if edit == "category":
            txns.update(db, txn.id, category_id=cats["home"].id)
        elif edit == "notes":
            txns.update(db, txn.id, notes="hello")
        elif edit == "reset":
            txns.reset_to_automatic(db, txn.id)
        elif edit == "clear_review":
            txns.clear_review(db, txn.id)
        elif edit == "set_splits":
            txns.set_splits(
                db,
                txn.id,
                [
                    (cats["groceries"].id, Decimal("5.00"), None),
                    (cats["home"].id, Decimal("15.00"), None),
                ],
            )
        else:
            txns.clear_splits(db, txn.id)
        assert holds_ledger_lock(db)


class TestEditsCarry:
    """Edits made here are user state (ARCHITECTURE §6): they follow a charge to its posted row,
    and a split stays whole when Plaid changes the amount."""

    def test_edits_on_a_pending_charge_move_to_the_posted_one(self, db, card, cats):
        pending = make_transaction(db, card, amount="30.00", pending=True)
        txns.update(db, pending.id, category_id=cats["dining"].id, notes="lunch", tags=["Work"])
        state = userstate.capture(db.get(Transaction, pending.id))
        assert state is not None and state.category_id == cats["dining"].id
        posted = make_transaction(db, card, amount="31.50", category=cats["uncategorized"])
        categorize.apply_carried_state(categorize.Categorizer.load(db), posted, state)
        assert (posted.category_id, posted.category_source) == (cats["dining"].id, "user")
        assert posted.notes == "lunch" and list(posted.tags) == ["Work"]

    def test_a_split_rescaled_after_an_amount_change_needs_review(self, db, card, cats):
        txn = make_transaction(db, card, amount="100.00", category=cats["groceries"])
        txns.set_splits(
            db, txn.id, parts((cats["groceries"], "60.00", None), (cats["home"], "40.00", None))
        )
        stored = db.get(Transaction, txn.id)
        stored.amount = Decimal("100.01")
        db.flush()
        assert userstate.fit_splits_to_amount(stored)
        db.flush()
        row = txns.get_row(db, txn.id)
        assert row.is_split and row.splits_total == Decimal("100.01") and row.needs_review


class TestNotesAndTags:
    def test_notes_and_tags_are_cleaned(self, db, card, cats, at_now):
        txn = make_transaction(db, card, category=cats["dining"], category_source="plaid")
        row = txns.update(
            db,
            txn.id,
            notes="  Team dinner\r\nreport #42  ",
            tags=[" work ", "Work", "", "Travel  plans"],
        )
        assert row.notes == "Team dinner\nreport #42"
        assert row.tags == ("work", "Travel plans")
        assert row.user_edited_at == NOW
        assert row.category_source == "plaid"  # notes and tags never touch the category

    def test_empty_notes_and_no_tags_clear_them(self, db, card):
        txn = make_transaction(db, card, notes="old", tags=["a"])
        row = txns.update(db, txn.id, notes="   ", tags=[])
        assert row.notes is None and row.tags == ()

    def test_tags_are_case_preserving(self, db, card):
        txn = make_transaction(db, card)
        assert txns.update(db, txn.id, tags=["NYC conference", "nyc CONFERENCE"]).tags == (
            "NYC conference",
        )

    @pytest.mark.parametrize(
        ("tags", "message"),
        [
            (["a,b"], "commas"),
            (["x" * 41], "under 40"),
            ([f"t{i}" for i in range(21)], "at most 20"),
        ],
    )
    def test_tag_limits(self, db, card, tags, message):
        txn = make_transaction(db, card)
        with pytest.raises(TransactionEditError) as err:
            txns.update(db, txn.id, tags=tags)
        assert message in err.value.errors["tags"]

    def test_notes_limit(self, db, card):
        txn = make_transaction(db, card)
        with pytest.raises(TransactionEditError) as err:
            txns.update(db, txn.id, notes="x" * 2001)
        assert "notes" in err.value.errors

    def test_control_characters_are_dropped(self, db, card):
        # PostgreSQL refuses NUL in text: a pasted "\x00" used to fail the save with a 500.
        txn = make_transaction(db, card)
        row = txns.update(
            db, txn.id, notes="Lunch\x00 with\r\nSam\x07\tok", tags=["wo\x00rk", "\x1bred"]
        )
        assert row.notes == "Lunch with\nSam\tok" and row.tags == ("work", "red")
        assert reload(db, txn).notes == "Lunch with\nSam\tok"


class TestResetAndReview:
    def test_reset_categorizes_again(self, db, card, cats):
        rule = make_rule(db, cats["home"], merchant_pattern="IKEA")
        txn = make_transaction(
            db,
            card,
            name="IKEA EAST PALO ALTO",
            category=cats["shopping"],
            category_source="user",
            needs_review=True,
            review_reason="x",
        )
        row = txns.reset_to_automatic(db, txn.id)
        assert (row.category.id, row.category_source, row.rule_id) == (
            cats["home"].id,
            "rule",
            rule.id,
        )
        assert not row.needs_review
        assert holds_ledger_lock(db)

    def test_reset_puts_a_payment_back_in_card_payments(self, db, card, cats):
        txn = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-500.00",
            transaction_code="payment",
            category=cats["shopping"],
            category_source="user",
        )
        row = txns.reset_to_automatic(db, txn.id)
        assert (row.category.id, row.category_source) == (cats["payments"].id, "system")

    def test_reset_without_rule_or_mapping_is_uncategorized(self, db, card, cats):
        txn = make_transaction(
            db,
            card,
            name="POP-UP STALL",
            pfc_primary=None,
            pfc_detailed=None,
            category=cats["dining"],
            category_source="user",
        )
        row = txns.reset_to_automatic(db, txn.id)
        assert (row.category.id, row.category_source) == (cats["uncategorized"].id, "default")

    def test_clear_review(self, db, card):
        txn = make_transaction(db, card, needs_review=True, review_reason="Please check")
        row = txns.clear_review(db, txn.id)
        assert not row.needs_review and row.review_reason is None
        assert not reload(db, txn).needs_review


# --------------------------------------------------------------------------- splits (TXN-4)


def parts(*items):
    return [(c.id, Decimal(a), n) for c, a, n in items]


class TestSplits:
    def test_a_split_feeds_spending(self, db, card, cats, at_now):
        txn = make_transaction(
            db, card, amount="213.77", category=cats["shopping"], needs_review=True
        )
        before = spending.period_spending(db, OCTOBER)
        assert before.amount_for(cats["shopping"].id) == Decimal("213.77")
        row = txns.set_splits(
            db,
            txn.id,
            parts((cats["groceries"], "150.00", " bulk  food "), (cats["home"], "63.77", None)),
        )
        assert row.is_split and not row.splits_mismatch and not row.needs_review
        assert [(s.category.name, s.amount, s.note) for s in row.splits] == [
            ("Groceries", Decimal("150.00"), "bulk food"),
            ("Home", Decimal("63.77"), None),
        ]
        assert row.user_edited_at == NOW and row.category.id == cats["shopping"].id
        after = spending.period_spending(db, OCTOBER)
        assert after.amount_for(cats["groceries"].id) == Decimal("150.00")
        assert after.amount_for(cats["home"].id) == Decimal("63.77")
        assert after.amount_for(cats["shopping"].id) == Decimal("0")
        assert after.total == before.total
        assert holds_ledger_lock(db)

    def test_a_refund_splits_into_credits(self, db, card, cats):
        txn = make_transaction(db, card, amount="-54.99", category=cats["shopping"])
        txns.set_splits(
            db, txn.id, parts((cats["shopping"], "-30.00", None), (cats["home"], "-24.99", None))
        )
        result = spending.period_spending(db, OCTOBER)
        assert result.amount_for(cats["home"].id) == Decimal("-24.99")

    @pytest.mark.parametrize(
        ("second", "phrase"), [("63.76", "$0.01 left to assign"), ("63.78", "$0.01 too much")]
    )
    def test_one_cent_off_is_refused(self, db, card, cats, second, phrase):
        txn = make_transaction(db, card, amount="213.77")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db, txn.id, parts((cats["groceries"], "150.00", None), (cats["home"], second, None))
            )
        assert phrase in err.value.errors["parts"]
        assert reload(db, txn).splits == []

    def test_one_cent_off_on_a_credit(self, db, card, cats):
        txn = make_transaction(db, card, amount="-10.00")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db, txn.id, parts((cats["groceries"], "-5.00", None), (cats["home"], "-4.99", None))
            )
        assert (
            "$9.99" in err.value.errors["parts"]
            and "$0.01 left to assign" in err.value.errors["parts"]
        )

    def test_part_notes_lose_control_characters(self, db, card, cats):
        txn = make_transaction(db, card, amount="10.00", category=cats["groceries"])
        row = txns.set_splits(
            db, txn.id, parts((cats["groceries"], "4.00", "milk\x00"), (cats["home"], "6.00", None))
        )
        assert [s.note for s in row.splits] == ["milk", None]

    def test_pending_charges_cannot_be_split(self, db, card, cats):
        txn = make_transaction(db, card, amount="20.00", pending=True)
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db, txn.id, parts((cats["groceries"], "10.00", None), (cats["home"], "10.00", None))
            )
        assert "pending" in err.value.errors["parts"]

    def test_card_payments_cannot_be_split(self, db, card, cats):
        # Its parts would move a payment into spending categories (ARCHITECTURE §6 never gives
        # a payment splits), whether the system or the user put it in Card Payments.
        two = parts((cats["groceries"], "-300.00", None), (cats["home"], "-200.00", None))
        system = make_transaction(
            db, card, amount="-500.00", category=cats["payments"], category_source="system"
        )
        chosen = make_transaction(
            db, card, amount="-500.00", category=cats["payments"], category_source="user"
        )
        for txn in (system, chosen):
            with pytest.raises(TransactionEditError) as err:
                txns.set_splits(db, txn.id, two)
            assert "card payment can't be split" in err.value.errors["parts"]
            assert not txns.get_row(db, txn.id).can_split
        # Once the user says it isn't a payment, it can be split like any credit.
        row = txns.update(db, system.id, category_id=cats["shopping"].id)
        assert row.can_split
        assert txns.set_splits(db, system.id, two).is_split

    def test_zero_amount_transactions_cannot_be_split(self, db, card, cats):
        txn = make_transaction(db, card, amount="0.00")
        with pytest.raises(TransactionEditError):
            txns.set_splits(
                db, txn.id, parts((cats["groceries"], "0.00", None), (cats["home"], "0.00", None))
            )

    def test_part_count_limits(self, db, card, cats):
        txn = make_transaction(db, card, amount="20.00")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(db, txn.id, parts((cats["groceries"], "20.00", None)))
        assert "at least two" in err.value.errors["parts"]
        many = [(cats["groceries"].id, Decimal("1.00"), None)] * 13
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(db, txn.id, many)
        assert "at most 12" in err.value.errors["parts"]

    def test_sign_rules(self, db, card, cats):
        charge = make_transaction(db, card, amount="20.00")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db,
                charge.id,
                parts((cats["groceries"], "30.00", None), (cats["home"], "-10.00", None)),
            )
        assert "every part must be a charge" in err.value.errors["part-1-amount"]
        credit = make_transaction(db, card, amount="-20.00")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db,
                credit.id,
                parts((cats["groceries"], "-30.00", None), (cats["home"], "10.00", None)),
            )
        assert "every part must be a credit" in err.value.errors["part-1-amount"]

    def test_parts_must_be_non_zero_cents(self, db, card, cats):
        txn = make_transaction(db, card, amount="20.00")
        bad = [
            (cats["groceries"].id, Decimal("0.00"), None),
            (cats["home"].id, Decimal("10.005"), None),
            (cats["dining"].id, 9.995, None),
            (cats["personal"].id, "10.00", None),
        ]
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(db, txn.id, bad)
        assert set(err.value.errors) == {
            "part-0-amount",
            "part-1-amount",
            "part-2-amount",
            "part-3-amount",
        }
        assert "can't be $0.00" in err.value.errors["part-0-amount"]

    def test_categories_must_exist_and_differ(self, db, card, cats):
        txn = make_transaction(db, card, amount="20.00")
        with pytest.raises(TransactionEditError) as err:
            txns.set_splits(
                db,
                txn.id,
                [
                    (cats["groceries"].id, Decimal("10.00"), None),
                    (cats["groceries"].id, Decimal("5.00"), None),
                    (987654321, Decimal("5.00"), None),
                ],
            )
        assert set(err.value.errors) == {"part-1-category", "part-2-category"}

    def test_a_new_split_replaces_the_old_one(self, db, card, cats):
        txn = make_transaction(db, card, amount="30.00")
        txns.set_splits(
            db, txn.id, parts((cats["groceries"], "10.00", None), (cats["home"], "20.00", None))
        )
        row = txns.set_splits(
            db,
            txn.id,
            parts(
                (cats["dining"], "5.00", None),
                (cats["home"], "5.00", None),
                (cats["personal"], "20.00", "gift"),
            ),
        )
        assert [s.category.name for s in row.splits] == ["Dining & Drinks", "Home", "Personal Care"]
        assert len(reload(db, txn).splits) == 3

    def test_clearing_a_split_puts_the_amount_back(self, db, card, cats, at_now):
        txn = make_transaction(db, card, amount="30.00", category=cats["shopping"])
        txns.set_splits(
            db, txn.id, parts((cats["groceries"], "10.00", None), (cats["home"], "20.00", None))
        )
        row = txns.clear_splits(db, txn.id)
        assert row.splits == () and row.category.id == cats["shopping"].id
        assert spending.period_spending(db, OCTOBER).amount_for(cats["shopping"].id) == Decimal(
            "30.00"
        )
        unsplit = make_transaction(db, card)
        assert txns.clear_splits(db, unsplit.id).user_edited_at is None


# --------------------------------------------------------------------------- one-click rules


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("WHOLEFDS MKT 10234 SAN FRANCISCO CA", "WHOLEFDS MKT"),
        ("SQ *DELFINA SAN FRANCISCO CA", "DELFINA SAN FRANCISCO"),
        ("TST* TARTINE BAKERY", "TARTINE BAKERY"),
        ("UBER *TRIP HELP.UBER.COM CA", "UBER"),
        ("TM *TICKETMASTER 800-653-8000 CA", "TICKETMASTER"),
        ("CLIPPER SERVICE 877-878-8883 CA", "CLIPPER SERVICE"),
        ("TRADER JOE S #236", "TRADER JOE S"),
        ("7-ELEVEN 12345", "7-ELEVEN"),
        ("AMAZON.COM*AB12CD", "AMAZON.COM"),
        ("BLUE BOTTLE COFFEE", "BLUE BOTTLE COFFEE"),
        ("  NETFLIX.COM  ", "NETFLIX.COM"),
        ("*", "*"),
        ("", ""),
        # Amex CSV descriptions pad the merchant before the city: the padding ends the merchant.
        ("BLUE BOTTLE COFFEE        SAN FRANCISCO CA", "BLUE BOTTLE COFFEE"),
        ("SFMTA PAYBYPHONE          SAN FRANCISCO CA", "SFMTA PAYBYPHONE"),
        ("SQ *DELFINA               SAN FRANCISCO CA", "DELFINA"),
        ("WHOLEFDS MKT 10234        SAN FRANCISCO CA", "WHOLEFDS MKT"),
        ("  LYFT   *RIDE TUE 5PM", "LYFT"),
        ("NETFLIX.COM\x00", "NETFLIX.COM"),
    ],
)
def test_clean_description(raw, cleaned):
    assert txns.clean_description(raw) == cleaned


class TestOneClickDraft:
    def test_prefers_the_merchant_and_keeps_the_category(self, db, card, cats):
        txn = make_transaction(
            db, card, name="UBER *TRIP HELP.UBER.COM", merchant_name="Uber", category=cats["dining"]
        )
        draft = txns.one_click_draft(db, txn.id)
        assert (draft.merchant_pattern, draft.category_id) == ("Uber", cats["dining"].id)
        assert draft.priority == 100 and draft.runs_before is None and not draft.is_payment

    def test_cleans_the_description_and_leaves_uncategorized_unchosen(self, db, card):
        txn = make_transaction(db, card, name="SQ *DELFINA SAN FRANCISCO CA", source="csv")
        draft = txns.one_click_draft(db, txn.id)
        assert (draft.merchant_pattern, draft.category_id) == ("DELFINA SAN FRANCISCO", None)

    def test_a_csv_row_drafts_a_pattern_that_matches_new_plaid_charges(self, db, card, cats):
        # Plaid rows match on merchant_name ("Blue Bottle Coffee"): a draft that kept the city
        # from the CSV Description would never match a new charge.
        csv_row = make_transaction(
            db,
            card,
            source="csv",
            name="BLUE BOTTLE COFFEE        SAN FRANCISCO CA",
            category=cats["dining"],
            category_source="amex",
        )
        plaid_row = make_transaction(
            db, card, name="BLUE BOTTLE COFFEE", merchant_name="Blue Bottle Coffee"
        )
        draft = txns.one_click_draft(db, csv_row.id)
        assert (draft.merchant_pattern, draft.category_id) == (
            "BLUE BOTTLE COFFEE",
            cats["dining"].id,
        )
        rule = Rule(merchant_pattern=draft.merchant_pattern, match_type="contains")
        assert categorize.rule_matches(rule, plaid_row) and categorize.rule_matches(rule, csv_row)

    def test_runs_before_the_rule_that_matched(self, db, card, cats):
        older = make_rule(db, cats["shopping"], merchant_pattern="COSTCO", priority=40)
        make_rule(db, cats["home"], merchant_pattern="COSTCO", priority=60)
        txn = make_transaction(
            db,
            card,
            name="COSTCO WHSE #0144",
            category=cats["shopping"],
            category_source="rule",
            rule_id=older.id,
        )
        draft = txns.one_click_draft(db, txn.id)
        assert draft.priority == 39 and draft.runs_before == "Merchant contains “COSTCO”"

    def test_payments_are_flagged(self, db, card, cats):
        txn = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-100.00",
            category=cats["payments"],
            category_source="system",
        )
        assert txns.one_click_draft(db, txn.id).is_payment

    def test_missing(self, db):
        with pytest.raises(txns.TransactionNotFound):
            txns.one_click_draft(db, 987654321)


class TestOneClickRule:
    @pytest.fixture
    def coffee(self, db, card, cats):
        rows = {
            "source": make_transaction(
                db,
                card,
                name="BLUE BOTTLE COFFEE",
                merchant_name="Blue Bottle Coffee",
                category=cats["dining"],
                category_source="plaid",
            ),
            "past": make_transaction(
                db,
                card,
                name="BLUE BOTTLE OAKLAND",
                merchant_name="Blue Bottle Coffee",
                category=cats["dining"],
                category_source="plaid",
                date="2025-03-01",
            ),
            "csv": make_transaction(
                db,
                card,
                name="BLUE BOTTLE COFFEE SF",
                source="csv",
                category=cats["dining"],
                category_source="amex",
            ),
            "user": make_transaction(
                db,
                card,
                name="BLUE BOTTLE COFFEE",
                merchant_name="Blue Bottle Coffee",
                category=cats["personal"],
                category_source="user",
            ),
            "refund": make_transaction(
                db,
                card,
                name="BLUE BOTTLE REFUND",
                merchant_name="Blue Bottle Coffee",
                amount="-4.00",
                category=cats["dining"],
                category_source="plaid",
            ),
            "other": make_transaction(
                db,
                card,
                name="PHILZ COFFEE",
                merchant_name="Philz Coffee",
                category=cats["dining"],
                category_source="plaid",
            ),
            "dupe": make_transaction(
                db,
                card,
                name="BLUE BOTTLE COFFEE",
                source="csv",
                duplicate_status="suspect",
                category=cats["dining"],
                category_source="amex",
            ),
        }
        return rows

    def test_creates_the_rule_and_applies_it_to_past_rows_only_when_asked(self, db, cats, coffee):
        source = coffee["source"]
        preview = txns.preview_one_click_rule(
            db, source.id, merchant_pattern="blue bottle", category_id=cats["groceries"].id
        )
        assert preview == txns.RulePreview(matches=6, will_change=5, user_edited=1, payments=0)
        result = txns.create_one_click_rule(
            db,
            source.id,
            merchant_pattern="  blue   bottle ",
            category_id=str(cats["groceries"].id),
            apply_to_past=True,
        )
        assert (result.applied, result.changed, result.skipped_user) == (True, 5, 1)
        rule = db.get(Rule, result.rule_id)
        assert (rule.merchant_pattern, rule.match_type, rule.category_id) == (
            "blue bottle",
            "contains",
            cats["groceries"].id,
        )
        assert (rule.source_transaction_id, rule.priority, rule.is_active) == (source.id, 100, True)
        assert rule.description_pattern is None and rule.amount_min is None
        db.expire_all()
        moved = {k: (t.category_id, t.category_source) for k, t in coffee.items()}
        assert moved["source"] == (cats["groceries"].id, "rule")
        assert moved["past"] == (cats["groceries"].id, "rule")
        assert moved["csv"] == (cats["groceries"].id, "rule")
        assert moved["refund"] == (cats["groceries"].id, "rule")
        assert moved["user"] == (cats["personal"].id, "user")
        assert moved["other"] == (cats["dining"].id, "plaid")
        assert moved["dupe"] == (cats["groceries"].id, "rule")  # still a row, never counted
        assert holds_ledger_lock(db)

    def test_without_applying_only_this_transaction_follows_the_rule(self, db, cats, coffee):
        # "Always categorize like this" acts on the row it came from at once; "Also apply to past
        # transactions" decides about the others.
        result = txns.create_one_click_rule(
            db,
            coffee["source"].id,
            merchant_pattern="Blue Bottle",
            category_id=cats["groceries"].id,
            apply_to_past=False,
        )
        assert (result.applied, result.changed, result.source_changed) == (False, 0, True)
        db.expire_all()
        source = coffee["source"]
        assert (source.category_id, source.category_source, source.rule_id) == (
            cats["groceries"].id,
            "rule",
            result.rule_id,
        )
        assert coffee["past"].category_id == coffee["csv"].category_id == cats["dining"].id
        future = make_transaction(db, None, name="BLUE BOTTLE", merchant_name="Blue Bottle Coffee")
        decision = categorize.Categorizer.load(db).decide(future)
        assert (decision.category_id, decision.rule_id) == (cats["groceries"].id, result.rule_id)
        assert holds_ledger_lock(db)

    def test_a_transaction_categorized_by_hand_keeps_its_category(self, db, cats, coffee):
        result = txns.create_one_click_rule(
            db,
            coffee["user"].id,
            merchant_pattern="Blue Bottle",
            category_id=cats["groceries"].id,
            apply_to_past=False,
        )
        assert not result.source_changed
        db.expire_all()
        assert (coffee["user"].category_id, coffee["user"].category_source) == (
            cats["personal"].id,
            "user",
        )

    def test_a_pattern_that_misses_this_transaction_leaves_it_alone(self, db, cats, coffee):
        result = txns.create_one_click_rule(
            db,
            coffee["source"].id,
            merchant_pattern="Philz",
            category_id=cats["groceries"].id,
            apply_to_past=False,
        )
        assert not result.source_changed
        db.expire_all()
        assert coffee["source"].category_id == coffee["other"].category_id == cats["dining"].id

    def test_applied_to_the_past_the_result_says_whether_this_one_moved(self, db, cats, coffee):
        result = txns.create_one_click_rule(
            db,
            coffee["source"].id,
            merchant_pattern="Blue Bottle",
            category_id=cats["dining"].id,
            apply_to_past=True,
        )
        assert not result.source_changed  # it was already in Dining & Drinks
        db.expire_all()
        assert (coffee["source"].category_source, coffee["source"].rule_id) == (
            "rule",
            result.rule_id,
        )

    def test_the_preview_says_when_only_csv_rows_match(self, db, card, cats):
        csv_row = make_transaction(
            db, card, source="csv", name="BLUE BOTTLE COFFEE        SAN FRANCISCO CA"
        )
        make_transaction(db, card, name="BLUE BOTTLE COFFEE", merchant_name="Blue Bottle Coffee")

        def preview(pattern):
            return txns.preview_one_click_rule(
                db, csv_row.id, merchant_pattern=pattern, category_id=cats["dining"].id
            )

        city = preview("BLUE BOTTLE COFFEE SAN FRANCISCO")
        assert (city.matches, city.csv_only, city.plaid_merchant) == (1, True, None)
        merchant = preview("BLUE BOTTLE COFFEE")
        assert (merchant.matches, merchant.csv_only) == (2, False)

    def test_the_preview_names_the_merchant_plaid_uses(self, db, card, cats):
        # Plaid's description has the text, but the rule reads Plaid's merchant name ("SFMTA").
        csv_row = make_transaction(
            db, card, source="csv", name="SFMTA PAYBYPHONE          SAN FRANCISCO CA"
        )
        for _ in range(2):
            make_transaction(db, card, name="SFMTA PAYBYPHONE", merchant_name="SFMTA")
        make_transaction(db, card, name="SFMTA PAYBYPHONE MUNI", merchant_name="Muni")

        def preview(pattern):
            return txns.preview_one_click_rule(
                db, csv_row.id, merchant_pattern=pattern, category_id=cats["home"].id
            )

        draft = txns.one_click_draft(db, csv_row.id).merchant_pattern
        assert draft == "SFMTA PAYBYPHONE"
        missed = preview(draft)
        assert (missed.matches, missed.csv_only, missed.plaid_merchant) == (1, True, "SFMTA")
        both = preview("SFMTA")
        assert (both.matches, both.csv_only, both.plaid_merchant) == (3, False, None)

    def test_before_linking_csv_rows_are_all_there_is(self, db, cats):
        manual = make_account(db, manual=True)
        row = make_transaction(
            db, manual, source="csv", name="BLUE BOTTLE COFFEE        SAN FRANCISCO CA"
        )
        preview = txns.preview_one_click_rule(
            db,
            row.id,
            merchant_pattern="BLUE BOTTLE COFFEE SAN FRANCISCO",
            category_id=cats["dining"].id,
        )
        assert (preview.matches, preview.csv_only) == (1, False)

    def test_control_characters_never_reach_the_rule(self, db, cats, coffee):
        preview = txns.preview_one_click_rule(
            db, coffee["source"].id, merchant_pattern="Blue\x00", category_id=cats["home"].id
        )
        assert preview.matches == 6
        result = txns.create_one_click_rule(
            db,
            coffee["source"].id,
            merchant_pattern="Blue\x00 Bottle\x07",
            category_id=cats["home"].id,
            apply_to_past=False,
        )
        assert db.get(Rule, result.rule_id).merchant_pattern == "Blue Bottle"

    def test_the_new_rule_outranks_the_one_that_matched(self, db, card, cats):
        costco = make_rule(db, cats["groceries"], merchant_pattern="COSTCO")
        txn = make_transaction(
            db,
            card,
            name="COSTCO WHSE #0144",
            category=cats["groceries"],
            category_source="rule",
            rule_id=costco.id,
        )
        result = txns.create_one_click_rule(
            db,
            txn.id,
            merchant_pattern="COSTCO WHSE",
            category_id=cats["home"].id,
            apply_to_past=True,
        )
        assert result.changed == 1
        assert db.get(Rule, result.rule_id).priority == costco.priority - 1
        db.expire_all()
        assert (txn.category_id, txn.rule_id) == (cats["home"].id, result.rule_id)

    def test_payments_never_move(self, db, card, cats):
        payment = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-900.00",
            transaction_code="payment",
            category=cats["payments"],
            category_source="system",
        )
        preview = txns.preview_one_click_rule(
            db, payment.id, merchant_pattern="AUTOPAY", category_id=cats["shopping"].id
        )
        assert preview == txns.RulePreview(matches=1, will_change=0, user_edited=0, payments=1)
        result = txns.create_one_click_rule(
            db,
            payment.id,
            merchant_pattern="AUTOPAY",
            category_id=cats["shopping"].id,
            apply_to_past=True,
        )
        assert result.changed == 0
        db.expire_all()
        assert payment.category_id == cats["payments"].id

    @pytest.mark.parametrize(
        ("pattern", "category"), [("", "home"), ("x" * 301, "home"), ("Blue", None)]
    )
    def test_preview_needs_a_pattern_and_a_category(self, db, cats, coffee, pattern, category):
        category_id = cats[category].id if category else ""
        assert (
            txns.preview_one_click_rule(
                db, coffee["source"].id, merchant_pattern=pattern, category_id=category_id
            )
            is None
        )

    def test_validation(self, db, cats, coffee):
        with pytest.raises(categorize.RuleValidationError) as err:
            txns.create_one_click_rule(
                db,
                coffee["source"].id,
                merchant_pattern="   ",
                category_id=cats["home"].id,
                apply_to_past=True,
            )
        assert "conditions" in err.value.errors
        with pytest.raises(categorize.RuleValidationError) as err:
            txns.create_one_click_rule(
                db, coffee["source"].id, merchant_pattern="Blue", category_id="", apply_to_past=True
            )
        assert "category_id" in err.value.errors
        assert (
            db.scalar(select(Rule).where(Rule.source_transaction_id == coffee["source"].id)) is None
        )


# --------------------------------------------------------------------------- details


class TestDetail:
    def test_every_stored_field_and_where_it_came_from(self, db, card, cats):
        rule = make_rule(db, cats["dining"], merchant_pattern="BLUE", name="Coffee")
        make_tombstone(db, card, plaid_transaction_id="pend-1", amount="45.00", name="BLUE BOTTLE")
        txn = make_transaction(
            db,
            card,
            name="BLUE BOTTLE COFFEE",
            merchant_name="Blue Bottle",
            amount="50.00",
            category=cats["dining"],
            category_source="rule",
            rule_id=rule.id,
            pending_transaction_id="pend-1",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
            pfc_confidence="VERY_HIGH",
            pfc_version="v2",
            merchant_category_code="5814",
            merchant_entity_id="ent-1",
            payment_channel="in store",
            website="bluebottlecoffee.com",
            logo_url="https://plaid-merchant-logos/x.png",
            location={
                "address": "1 Ferry Building",
                "city": "San Francisco",
                "region": "CA",
                "postal_code": "94111",
                "country": "US",
                "lat": Decimal("37.79"),
                "lon": Decimal("-122.39"),
                "store_number": None,
            },
            transaction_code="purchase",
            account_owner="SAM RIVERA",
        )
        make_tombstone(
            db,
            card,
            plaid_transaction_id="gone-9",
            amount="49.00",
            consumed_by_transaction_id=txn.id,
        )
        made = make_rule(db, cats["groceries"], merchant_pattern="X", source_transaction_id=txn.id)
        detail = txns.detail(db, txn.id)
        assert detail.row.id == txn.id and detail.account_name == card.display_name
        assert detail.rule == txns.RuleRef(rule.id, "Coffee", True)
        assert detail.replaced_pending == txns.PendingLink(
            "pend-1", Decimal("45.00"), dt.date(2026, 10, 15), "BLUE BOTTLE", carried_edits=False
        )
        assert [i.plaid_transaction_id for i in detail.inherited_from] == ["gone-9"]
        assert [r.id for r in detail.rules_made_here] == [made.id]
        assert detail.location_text == "1 Ferry Building, San Francisco, CA 94111"
        assert (detail.pfc_detailed, detail.merchant_category_code, detail.website) == (
            "FOOD_AND_DRINK_COFFEE",
            "5814",
            "bluebottlecoffee.com",
        )
        assert detail.plaid_transaction_id == txn.plaid_transaction_id

    def test_a_csv_row_and_a_pending_link_without_tombstone(self, db, cats):
        manual = make_account(db, manual=True, name="Amex CSV")
        upload = make_csv_import(db, manual, filename="activity.csv")
        txn = make_transaction(
            db,
            manual,
            source="csv",
            csv_import_id=upload.id,
            external_category="Restaurant-Bar & Café",
            pending_transaction_id="pend-x",
            location=None,
        )
        detail = txns.detail(db, txn.id)
        assert detail.csv_import_filename == "activity.csv" and detail.account_is_manual
        assert detail.replaced_pending == txns.PendingLink("pend-x", None, None, None)
        assert detail.location_text is None and detail.external_category == "Restaurant-Bar & Café"

    def test_a_pending_link_says_whether_edits_moved(self, db, card):
        state = userstate.to_json(userstate.UserState(notes="Lunch with Sam", tags=("work",)))
        make_tombstone(db, card, plaid_transaction_id="pend-2", user_state=state)
        txn = make_transaction(db, card, pending_transaction_id="pend-2", notes="Lunch with Sam")
        assert txns.detail(db, txn.id).replaced_pending.carried_edits is True

    def test_missing(self, db):
        with pytest.raises(txns.TransactionNotFound):
            txns.detail(db, 987654321)
