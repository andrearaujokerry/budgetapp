"""Rules (CAT-3) and re-applying categories (CAT-4): list, summary, changes, order, preview."""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from budgetapp.models import Rule, Transaction
from budgetapp.services import categorize, rules
from budgetapp.services.categorize import RuleValidationError
from budgetapp.services.rules import ReapplyScope, ReapplyScopeError, RuleDraft, RuleNotFound
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_rule,
    make_transaction,
    uncategorized,
)


def draft(category, **kw):
    values = {"category_id": str(category.id)}
    values.update({k: str(v) if not isinstance(v, bool) else v for k, v in kw.items()})
    return RuleDraft(**values)


def state(db, txn):
    db.refresh(txn)
    return txn.category_id, txn.category_source, txn.rule_id


# --------------------------------------------------------------------------- summary & listing


class TestSummary:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            (
                {"merchant_pattern": "UBER", "amount_min": Decimal("50")},
                "Merchant contains UBER · amount ≥ $50",
            ),
            ({"merchant_pattern": "Netflix", "match_type": "equals"}, "Merchant is Netflix"),
            (
                {"description_pattern": "SQ *", "match_type": "starts_with"},
                "Description starts with SQ *",
            ),
            (
                {"description_pattern": r"^TST\*", "match_type": "regex"},
                r"Description matches ^TST\*",
            ),
            ({"amount_min": Decimal("10"), "amount_max": Decimal("49.99")}, "Amount $10 to $49.99"),
            ({"amount_min": Decimal("9.99"), "amount_max": Decimal("9.99")}, "Amount is $9.99"),
            ({"amount_max": Decimal("-5")}, "Amount ≤ −$5"),
            (
                {"merchant_pattern": "A", "description_pattern": "B", "account_id": 7},
                "Merchant contains A · description contains B · card Gold ··7",
            ),
            ({}, "No conditions"),
        ],
    )
    def test_conditions_in_words(self, fields, expected):
        values = {
            "merchant_pattern": None,
            "description_pattern": None,
            "match_type": "contains",
            "amount_min": None,
            "amount_max": None,
            "account_id": None,
        }
        values.update(fields)
        label = "Gold ··7" if values["account_id"] else None
        assert rules.summarize(SimpleNamespace(**values), label) == expected


class TestListRules:
    def test_run_order_positions_counts_and_titles(self, db):
        groceries, travel = get_category(db, "Groceries"), get_category(db, "Travel")
        late = make_rule(db, travel, merchant_pattern="DELTA", priority=200, name="Flights")
        early = make_rule(db, groceries, merchant_pattern="COSTCO", priority=10)
        tie = make_rule(db, groceries, merchant_pattern="SAFEWAY", priority=10)
        account = make_account(db)
        make_transaction(db, account, category=groceries, category_source="rule", rule_id=early.id)
        make_transaction(db, account, category=groceries, category_source="rule", rule_id=early.id)
        listed = rules.list_rules(db)
        assert [r.id for r in listed] == [early.id, tie.id, late.id]
        assert [r.position for r in listed] == [1, 2, 3]
        assert listed[0].transaction_count == 2 and listed[1].transaction_count == 0
        assert listed[0].title == "Merchant contains COSTCO"
        assert listed[2].title == "Flights" and listed[2].category.name == "Travel"
        assert rules.get_row(db, late.id).position == 3

    def test_account_labels_in_the_summary(self, db):
        account = make_account(db, name="Gold Card", mask="1001")
        rule = make_rule(db, get_category(db, "Travel"), account_id=account.id)
        assert rules.get_row(db, rule.id).summary == "Card Gold Card ··1001"


# --------------------------------------------------------------------------- validation


class TestCleanRule:
    def test_valid_draft(self, db):
        travel = get_category(db, "Travel")
        clean = rules.clean_rule(
            db,
            draft(travel, merchant_pattern="  delta ", amount_min="25", name="  Air  travel "),
        )
        assert clean.merchant_pattern == "delta" and clean.amount_min == Decimal("25.00")
        assert clean.name == "Air travel" and clean.is_active
        assert clean.priority == 100  # no rules yet

    def test_new_rules_go_last_and_edits_keep_their_priority(self, db):
        travel = get_category(db, "Travel")
        existing = make_rule(db, travel, priority=40)
        assert rules.clean_rule(db, draft(travel, merchant_pattern="X")).priority == 50
        assert rules.next_priority(db) == 50
        edited = rules.clean_rule(db, draft(travel, merchant_pattern="X"), current=existing)
        assert edited.priority == 40
        assert (
            rules.clean_rule(db, draft(travel, merchant_pattern="X", priority="-3")).priority == -3
        )

    def test_invalid_regex_is_a_field_message(self, db):
        with pytest.raises(RuleValidationError) as err:
            rules.clean_rule(
                db, draft(get_category(db, "Travel"), merchant_pattern="(", match_type="regex")
            )
        assert err.value.errors["merchant_pattern"].startswith("Not a valid regular expression")

    def test_a_rule_without_conditions_is_rejected(self, db):
        with pytest.raises(RuleValidationError) as err:
            rules.clean_rule(db, draft(get_category(db, "Travel"), merchant_pattern="   "))
        assert err.value.errors == {
            "conditions": "Add at least one condition (merchant, description, amount or card)."
        }

    def test_every_problem_at_once(self, db):
        bad = RuleDraft(
            category_id="abc",
            account_id="x",
            priority="first",
            amount_min="50",
            amount_max="10",
            match_type="fuzzy",
            name="n" * 81,
            merchant_pattern="A",
        )
        with pytest.raises(RuleValidationError) as err:
            rules.clean_rule(db, bad)
        assert set(err.value.errors) == {
            "category_id",
            "account_id",
            "priority",
            "amount_max",
            "match_type",
            "name",
        }

    @pytest.mark.parametrize("value", ["12.345", "abc", "1e9999"])
    def test_bad_amounts(self, db, value):
        with pytest.raises(RuleValidationError) as err:
            rules.clean_rule(db, draft(get_category(db, "Travel"), amount_min=value))
        assert "amount_min" in err.value.errors

    def test_missing_category_and_card(self, db):
        with pytest.raises(RuleValidationError) as err:
            rules.clean_rule(
                db, RuleDraft(category_id="999999999", account_id="999999999", merchant_pattern="A")
            )
        assert err.value.errors["category_id"] == "Choose a category."
        assert err.value.errors["account_id"] == "That card does not exist."

    def test_draft_round_trips_from_a_rule_and_a_form(self, db):
        rule = make_rule(
            db,
            get_category(db, "Travel"),
            merchant_pattern="DELTA",
            amount_max="500",
            is_active=False,
            priority=30,
        )
        d = RuleDraft.from_rule(rule)
        assert (d.amount_max, d.priority, d.is_active, d.category_id) == (
            "500.00",
            "30",
            False,
            str(rule.category_id),
        )
        form = {"merchant_pattern": " UBER ", "category_id": "4", "is_active": "1"}
        parsed = RuleDraft.from_form(form)
        assert parsed.merchant_pattern == "UBER" and parsed.is_active
        assert not parsed.apply_to_existing and parsed.match_type == "contains"
        assert RuleDraft.from_form({}).is_blank and not RuleDraft.from_form({}).is_active


# --------------------------------------------------------------------------- changes


@pytest.fixture
def uber_world(db):
    """Past rows a "Merchant contains UBER → Transportation" rule would touch."""
    account = make_account(db)
    transportation = get_category(db, "Transportation")
    dining = get_category(db, "Dining & Drinks")
    rows = {
        "ride": make_transaction(
            db, account, amount="23.18", name="UBER *TRIP", merchant_name="Uber"
        ),
        "eats": make_transaction(
            db,
            account,
            amount="31.00",
            name="UBER *EATS",
            merchant_name="Uber Eats",
            category=dining,
            category_source="plaid",
        ),
        "mine": make_transaction(
            db, account, amount="12.00", name="UBER *TRIP", category=dining, category_source="user"
        ),
        "there": make_transaction(
            db,
            account,
            amount="9.00",
            name="UBER *TRIP",
            category=transportation,
            category_source="plaid",
        ),
        "payment": make_transaction(
            db, account, amount="-50.00", name="UBER PAYMENT RECEIVED - THANK YOU"
        ),
        "other": make_transaction(db, account, amount="5.00", name="LYFT RIDE"),
    }
    return SimpleNamespace(account=account, transportation=transportation, dining=dining, **rows)


class TestCreate:
    def test_applies_to_past_rows_except_your_edits_and_payments(self, db, uber_world):
        w = uber_world
        result = rules.create(
            db, draft(w.transportation, merchant_pattern="uber", apply_to_existing=True)
        )
        rule = result.rule
        assert result.applied.changed == 3 and result.applied.skipped_user == 1
        assert state(db, w.ride) == (w.transportation.id, "rule", rule.id)
        assert state(db, w.eats) == (w.transportation.id, "rule", rule.id)
        assert state(db, w.mine) == (w.dining.id, "user", None)
        assert state(db, w.payment)[:2] == (card_payments(db).id, "system")
        assert state(db, w.other)[0] == uncategorized(db).id

    def test_without_apply_past_rows_stay(self, db, uber_world):
        w = uber_world
        result = rules.create(db, draft(w.transportation, merchant_pattern="uber"))
        assert result.applied is None
        assert state(db, w.ride)[0] == uncategorized(db).id

    def test_an_inactive_rule_changes_nothing(self, db, uber_world):
        w = uber_world
        result = rules.create(
            db,
            draft(
                w.transportation, merchant_pattern="uber", is_active=False, apply_to_existing=True
            ),
        )
        assert not result.rule.is_active and result.applied is None
        assert state(db, w.ride)[0] == uncategorized(db).id


class TestUpdate:
    def test_apply_redecides_rows_it_no_longer_matches(self, db, uber_world):
        w = uber_world
        rule = rules.create(
            db, draft(w.transportation, merchant_pattern="uber", apply_to_existing=True)
        ).rule
        assert state(db, w.eats)[0] == w.transportation.id
        result = rules.update_rule(
            db,
            rule.id,
            draft(
                w.transportation,
                merchant_pattern="uber",
                match_type="equals",
                apply_to_existing=True,
            ),
        )
        assert result.rule.match_type == "equals" and result.rule.priority == rule.priority
        # Uber Eats is no longer matched: back to its Plaid mapping (here Uncategorized: no PFC)
        assert state(db, w.eats) == (uncategorized(db).id, "default", None)
        assert state(db, w.ride) == (w.transportation.id, "rule", rule.id)
        assert result.applied.skipped_user == 0  # "UBER *TRIP" (yours) isn't "Uber" exactly

    def test_without_apply_nothing_moves(self, db, uber_world):
        w = uber_world
        rule = rules.create(
            db, draft(w.transportation, merchant_pattern="uber", apply_to_existing=True)
        ).rule
        result = rules.update_rule(
            db, rule.id, draft(w.dining, merchant_pattern="uber", name="Rides")
        )
        assert result.applied is None and result.rule.name == "Rides"
        assert state(db, w.ride)[0] == w.transportation.id

    def test_disabling_with_apply_releases_its_rows(self, db, uber_world):
        w = uber_world
        rule = rules.create(
            db, draft(w.transportation, merchant_pattern="uber", apply_to_existing=True)
        ).rule
        rules.update_rule(
            db,
            rule.id,
            draft(
                w.transportation, merchant_pattern="uber", is_active=False, apply_to_existing=True
            ),
        )
        assert state(db, w.ride) == (uncategorized(db).id, "default", None)

    def test_unknown_rule(self, db):
        with pytest.raises(RuleNotFound):
            rules.update_rule(db, 10**9, draft(get_category(db, "Travel"), merchant_pattern="X"))


class TestDeleteAndActive:
    def test_delete_keeps_the_categories_it_set(self, db, uber_world):
        w = uber_world
        rule = rules.create(
            db,
            draft(w.transportation, merchant_pattern="uber", name="Rides", apply_to_existing=True),
        ).rule
        deleted = rules.delete_rule(db, rule.id)
        # ride, eats and "there" (already Transportation, now set by the rule)
        assert deleted.summary == "Rides" and deleted.transaction_count == 3
        assert db.get(Rule, rule.id) is None
        assert state(db, w.ride) == (w.transportation.id, "rule", None)
        with pytest.raises(RuleNotFound):
            rules.delete_rule(db, rule.id)

    def test_inactive_rules_do_not_categorize(self, db, uber_world):
        w = uber_world
        rule = make_rule(db, w.transportation, merchant_pattern="uber")
        rules.set_active(db, rule.id, False)
        assert categorize.Categorizer.load(db).decide(w.ride).category_id == uncategorized(db).id
        assert rules.set_active(db, rule.id, True).is_active
        assert categorize.Categorizer.load(db).decide(w.ride).category_id == w.transportation.id


class TestMove:
    @pytest.fixture
    def three(self, db):
        travel = get_category(db, "Travel")
        return [make_rule(db, travel, merchant_pattern=f"R{i}") for i in range(3)]  # all 100

    def test_move_up_and_down_renumbers(self, db, three):
        a, b, c = three
        assert rules.move_rule(db, c.id, "up")
        assert [r.id for r in rules.ordered_rules(db)] == [a.id, c.id, b.id]
        assert [r.priority for r in rules.ordered_rules(db)] == [10, 20, 30]
        assert rules.move_rule(db, a.id, "down")
        assert [r.id for r in rules.ordered_rules(db)] == [c.id, a.id, b.id]

    def test_edges_do_not_move(self, db, three):
        a, b, c = three
        assert not rules.move_rule(db, a.id, "up")
        assert not rules.move_rule(db, c.id, "down")
        assert [r.priority for r in rules.ordered_rules(db)] == [100, 100, 100]

    def test_order_decides_which_rule_wins(self, db, three):
        a, b, c = three
        groceries = get_category(db, "Groceries")
        c.merchant_pattern, c.category_id = "R0", groceries.id  # c also matches "R0"
        db.flush()
        txn = make_transaction(db, name="R0 STORE")
        assert categorize.Categorizer.load(db).decide(txn).rule_id == a.id
        rules.move_rule(db, c.id, "up")
        rules.move_rule(db, c.id, "up")
        assert categorize.Categorizer.load(db).decide(txn).rule_id == c.id

    def test_bad_input(self, db, three):
        with pytest.raises(ValueError):
            rules.move_rule(db, three[0].id, "sideways")
        with pytest.raises(RuleNotFound):
            rules.move_rule(db, 10**9, "up")


# --------------------------------------------------------------------------- preview


class TestPreview:
    def test_counts_match_what_saving_with_apply_changes(self, db, uber_world):
        w = uber_world
        earlier = make_rule(db, w.dining, merchant_pattern="EATS", priority=10, name="Food")
        d = draft(w.transportation, description_pattern="uber", apply_to_existing=True)
        preview = rules.preview(db, d)
        assert preview.is_new and preview.is_active
        assert preview.match_count == 5  # ride, eats, mine, there, payment
        assert preview.skipped_user_count == 1
        assert preview.payment_count == 1
        assert preview.earlier_rule_count == 1
        result = rules.create(db, d)
        assert preview.would_change_count == result.applied.changed == 2
        assert preview.skipped_user_count == result.applied.skipped_user
        assert state(db, w.eats)[2] == earlier.id

    def test_nothing_is_saved(self, db, uber_world):
        w = uber_world
        before = db.scalar(select(func.count()).select_from(Rule))
        rules.preview(db, draft(w.transportation, merchant_pattern="uber"))
        assert db.scalar(select(func.count()).select_from(Rule)) == before
        assert state(db, w.ride)[0] == uncategorized(db).id

    def test_sample_says_why(self, db, uber_world):
        w = uber_world
        preview = rules.preview(db, draft(w.transportation, merchant_pattern="uber"))
        by_id = {item.transaction_id: item for item in preview.sample}
        assert by_id[w.ride.id].status == "change"
        assert by_id[w.ride.id].proposed.name == "Transportation"
        assert by_id[w.mine.id].status == "user" and by_id[w.mine.id].proposed is None
        assert by_id[w.there.id].status == "same"
        assert by_id[w.payment.id].status == "payment"
        assert (
            by_id[w.eats.id].description == "Uber Eats" and by_id[w.eats.id].detail == "UBER *EATS"
        )
        assert w.other.id not in by_id

    def test_sample_is_newest_first_and_limited(self, db):
        travel = get_category(db, "Travel")
        account = make_account(db)
        rows = [
            make_transaction(db, account, name="DELTA", date=dt.date(2026, 10, day))
            for day in (1, 9, 5)
        ]
        preview = rules.preview(db, draft(travel, merchant_pattern="delta"), limit=2)
        assert preview.match_count == 3
        assert [i.transaction_id for i in preview.sample] == [rows[1].id, rows[2].id]

    def test_an_inactive_new_rule_would_change_nothing(self, db, uber_world):
        w = uber_world
        d = draft(
            w.transportation, merchant_pattern="uber", is_active=False, apply_to_existing=True
        )
        preview = rules.preview(db, d)
        assert preview.match_count == 5 and preview.would_change_count == 0
        assert {i.status for i in preview.sample} == {"off", "user"}

    def test_edit_preview_matches_update_with_apply(self, db, uber_world):
        w = uber_world
        rule = rules.create(
            db, draft(w.transportation, merchant_pattern="uber", apply_to_existing=True)
        ).rule
        d = draft(w.dining, merchant_pattern="uber", match_type="equals", apply_to_existing=True)
        preview = rules.preview(db, d, rule_id=rule.id)
        assert not preview.is_new and preview.match_count == 1  # the ride (merchant "Uber")
        assert preview.released_count == 2  # Uber Eats and "there" no longer match
        released = next(i for i in preview.sample if i.transaction_id == w.eats.id)
        assert released.status == "released" and released.proposed.name == "Uncategorized"
        result = rules.update_rule(db, rule.id, d)
        assert preview.would_change_count == result.applied.changed == 3
        assert preview.skipped_user_count == result.applied.skipped_user == 0

    def test_an_earlier_tie_wins_over_a_new_rule_of_the_same_priority(self, db, uber_world):
        w = uber_world
        make_rule(db, w.dining, merchant_pattern="uber", priority=100)
        preview = rules.preview(
            db, draft(w.transportation, merchant_pattern="uber", priority="100")
        )
        assert preview.earlier_rule_count == 3  # ride, eats, there (yours and the payment aside)
        assert preview.would_change_count == 3  # ride, there → Dining; the payment → Card Payments
        assert (
            rules.create(
                db,
                draft(
                    w.transportation,
                    merchant_pattern="uber",
                    priority="100",
                    apply_to_existing=True,
                ),
            ).applied.changed
            == preview.would_change_count
        )

    def test_invalid_drafts_raise_like_saving(self, db):
        with pytest.raises(RuleValidationError):
            rules.preview(db, RuleDraft(merchant_pattern="x"))


# --------------------------------------------------------------------------- re-apply


@pytest.fixture
def stale(db):
    """Rows categorized before a rule existed, a user edit and a stale payment."""
    account = make_account(db)
    travel = get_category(db, "Travel")
    make_rule(db, travel, merchant_pattern="DELTA")
    return SimpleNamespace(
        travel=travel,
        old=make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 3, 1)),
        new=make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 10, 1)),
        mine=make_transaction(
            db,
            account,
            name="DELTA AIR",
            category=get_category(db, "Shopping"),
            category_source="user",
            date=dt.date(2026, 10, 2),
        ),
        payment=make_transaction(
            db,
            account,
            amount="-100",
            name="PAYMENT RECEIVED - THANK YOU",
            category=get_category(db, "Other"),
            category_source="plaid",
            date=dt.date(2026, 10, 3),
        ),
    )


class TestReapply:
    def test_dry_run_counts_exactly_what_apply_changes(self, db, stale):
        dry = rules.reapply(db, ReapplyScope(), dry_run=True)
        assert state(db, stale.old)[0] == uncategorized(db).id
        real = rules.reapply(db, ReapplyScope(), dry_run=False)
        assert (dry.examined, dry.changed, dry.skipped_user) == (
            real.examined,
            real.changed,
            real.skipped_user,
        )
        assert real.changed == 3 and real.skipped_user == 1 and not real.dry_run
        assert state(db, stale.old)[0] == stale.travel.id
        assert state(db, stale.payment)[:2] == (card_payments(db).id, "system")
        assert state(db, stale.mine)[:2] == (get_category(db, "Shopping").id, "user")

    def test_scopes(self, db, stale):
        since = rules.reapply(db, rules.parse_scope("since", "2026-09-01"))
        assert since.changed == 2 and since.skipped_user == 1
        assert {i.transaction_id for i in since.sample} == {stale.new.id, stale.payment.id}
        only = rules.reapply(db, rules.parse_scope("uncategorized"))
        assert only.changed == 2 and only.skipped_user == 0
        assert rules.reapply(db, rules.parse_scope("all")).changed == 3

    def test_sample_rows_name_the_categories(self, db, stale):
        result = rules.reapply(db, ReapplyScope(), limit=1)
        assert len(result.sample) == 1
        item = result.sample[0]
        assert item.description == "DELTA AIR" and item.new.name == "Travel"
        assert item.old.name == "Uncategorized"

    def test_scope_parsing_and_wording(self):
        assert rules.parse_scope("all") == ReapplyScope()
        assert rules.parse_scope("uncategorized").only_uncategorized
        assert rules.parse_scope("since", "2026-01-31").since == dt.date(2026, 1, 31)
        assert ReapplyScope().description == "all past transactions"
        assert ReapplyScope(since=dt.date(2026, 1, 31)).description == (
            "transactions since Jan 31, 2026"
        )
        assert ReapplyScope(only_uncategorized=True).description == "Uncategorized transactions"
        with pytest.raises(ReapplyScopeError) as err:
            rules.parse_scope("since", "31/01/2026")
        assert "since" in err.value.errors
        with pytest.raises(ReapplyScopeError):
            rules.parse_scope("everything")

    def test_nothing_to_do(self, db):
        result = rules.reapply(db, ReapplyScope(), dry_run=False)
        assert (result.examined, result.changed, result.skipped_user) == (0, 0, 0)
        assert db.scalar(select(func.count()).select_from(Transaction)) == 0
