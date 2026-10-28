import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from budgetapp.models import Rule
from budgetapp.services import categorize
from budgetapp.services.categorize import (
    Categorizer,
    Decision,
    RuleValidationError,
    amex_category_keys,
    apply_decision,
    create_rule,
    is_payment,
    recategorize,
    rule_matches,
    system_category_id,
)
from budgetapp.services.userstate import UserState
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_category,
    make_rule,
    make_transaction,
    uncategorized,
)


def txn(**overrides):
    """An unsaved transaction-like object for pure decision tests."""
    values = {
        "id": None,
        "account_id": 1,
        "source": "plaid",
        "amount": Decimal("10.00"),
        "name": "SOME MERCHANT",
        "merchant_name": None,
        "original_description": None,
        "transaction_code": None,
        "pfc_primary": None,
        "pfc_detailed": None,
        "external_category": None,
    }
    values.update(overrides)
    if not isinstance(values["amount"], Decimal):
        values["amount"] = Decimal(str(values["amount"]))
    return SimpleNamespace(**values)


@pytest.fixture
def cz(db):
    return Categorizer.load(db)


class TestStepZeroPayments:
    @pytest.mark.parametrize(
        "fields",
        [
            {"transaction_code": "payment"},
            {"pfc_detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"},
            {"pfc_detailed": "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT"},
            {"pfc_primary": "TRANSFER_IN", "pfc_detailed": "TRANSFER_IN_ACCOUNT_TRANSFER"},
            {"name": "AUTOPAY PAYMENT - THANK YOU"},
            {"name": "ONLINE PAYMENT - THANK YOU"},
            {"original_description": "MOBILE PAYMENT RECEIVED"},
            {"name": "autopay"},
            {"source": "csv", "external_category": ""},
            {"source": "csv", "external_category": None},
        ],
    )
    def test_negative_payments(self, cz, db, fields):
        decision = cz.decide(txn(amount="-2500.00", **fields))
        assert decision == Decision(card_payments(db).id, "system", reason="card payment")

    def test_returned_payment_is_a_card_payment(self, cz, db):
        assert (
            cz.decide(txn(amount="2500.00", name="PAYMENT RETURNED")).category_id
            == card_payments(db).id
        )
        assert cz.decide(txn(amount="2500.00", name="REVERSED PAYMENT")).source == "system"

    def test_not_payments(self):
        assert not is_payment(txn(amount="35.00", name="RETURNED PAYMENT FEE"))
        assert not is_payment(
            txn(amount="35.00", name="PAYMENT RETURNED", transaction_code="late fee")
        )
        assert not is_payment(txn(amount="25.00", name="ONLINE PAYMENT - THANK YOU"))
        assert not is_payment(txn(amount="-20.00", name="MERCHANT REFUND"))
        assert not is_payment(
            txn(amount="-20.00", source="csv", external_category="Restaurant-Restaurant")
        )
        assert not is_payment(
            txn(amount="-20.00", pfc_primary="TRANSFER_IN", transaction_code="refund")
        )

    def test_rules_cannot_override_payments(self, cz, db):
        dining = get_category(db, "Dining & Drinks")
        make_rule(db, dining, description_pattern="ONLINE", priority=1)
        decision = Categorizer.load(db).decide(
            txn(amount="-500", name="ONLINE PAYMENT - THANK YOU")
        )
        assert decision.category_id == card_payments(db).id and decision.source == "system"


class TestPrecedence:
    def test_carried_user_state_beats_rules(self, db):
        dining, home = get_category(db, "Dining & Drinks"), get_category(db, "Home")
        make_rule(db, dining, merchant_pattern="cafe")
        decision = Categorizer.load(db).decide(
            txn(merchant_name="Cafe Luna"), inherited=UserState(category_id=home.id)
        )
        assert decision.category_id == home.id and decision.source == "user"

    def test_step_zero_ranks_above_carried_state(self, cz, db):
        decision = cz.decide(
            txn(amount="-10", transaction_code="payment"),
            inherited=UserState(category_id=get_category(db, "Home").id),
        )
        assert decision.source == "system"

    def test_rule_beats_mapping(self, db):
        home = get_category(db, "Home")
        rule = make_rule(db, home, merchant_pattern="blue bottle")
        decision = Categorizer.load(db).decide(
            txn(
                merchant_name="Blue Bottle",
                pfc_primary="FOOD_AND_DRINK",
                pfc_detailed="FOOD_AND_DRINK_COFFEE",
            )
        )
        assert decision == Decision(home.id, "rule", rule.id, reason=f"rule {rule.id}")

    def test_lower_priority_number_wins_then_lower_id(self, db):
        home, travel, shopping = (get_category(db, n) for n in ("Home", "Travel", "Shopping"))
        make_rule(db, home, merchant_pattern="acme", priority=50)
        first_at_10 = make_rule(db, travel, merchant_pattern="acme", priority=10)
        make_rule(db, shopping, merchant_pattern="acme", priority=10)
        decision = Categorizer.load(db).decide(txn(merchant_name="ACME"))
        assert decision.rule_id == first_at_10.id and decision.category_id == travel.id

    def test_inactive_rules_are_ignored(self, db):
        make_rule(db, get_category(db, "Home"), merchant_pattern="acme", is_active=False)
        assert Categorizer.load(db).decide(txn(merchant_name="ACME")).source == "default"

    def test_plaid_mapping_detailed_then_primary(self, cz, db):
        coffee = cz.decide(
            txn(pfc_primary="FOOD_AND_DRINK", pfc_detailed="FOOD_AND_DRINK_GROCERIES")
        )
        assert coffee.category_id == get_category(db, "Groceries").id and coffee.source == "plaid"
        future = cz.decide(txn(pfc_primary="TRAVEL", pfc_detailed="TRAVEL_SPACE_FLIGHTS"))
        assert future.category_id == get_category(db, "Travel").id

    def test_amex_mapping_full_key_then_group(self, cz, db):
        bar = cz.decide(txn(source="csv", external_category="Restaurant-Bar & Café"))
        assert bar.category_id == get_category(db, "Dining & Drinks").id and bar.source == "amex"
        spaced = cz.decide(txn(source="csv", external_category="Transportation - Taxis & Coach"))
        assert spaced.category_id == get_category(db, "Transportation").id
        group_only = cz.decide(txn(source="csv", external_category="Travel-Space Station"))
        assert group_only.category_id == get_category(db, "Travel").id
        assert cz.decide(txn(source="csv", external_category="Weird-Thing")).source == "default"

    def test_csv_rows_ignore_pfc_and_plaid_rows_ignore_amex(self, cz):
        assert (
            cz.decide(txn(source="csv", external_category="X", pfc_primary="TRAVEL")).source
            == "default"
        )
        assert cz.decide(txn(external_category="Travel-Lodging")).source == "default"

    def test_default_is_uncategorized(self, cz, db):
        assert cz.decide(txn()) == Decision(
            uncategorized(db).id, "default", reason="no rule or mapping"
        )


class TestRuleMatching:
    @pytest.mark.parametrize(
        ("match_type", "pattern", "merchant", "expected"),
        [
            ("contains", "bottle", "Blue Bottle Coffee", True),
            ("contains", "BLUE  BOTTLE", "blue bottle   coffee", True),
            ("contains", "tea", "Blue Bottle Coffee", False),
            ("equals", "blue bottle coffee", "Blue Bottle Coffee", True),
            ("equals", "blue bottle", "Blue Bottle Coffee", False),
            ("starts_with", "blue", "Blue Bottle Coffee", True),
            ("starts_with", "bottle", "Blue Bottle Coffee", False),
            ("regex", r"^blue\s+bottle", "Blue Bottle Coffee", True),
            ("regex", r"uber\s*(eats)?$", "UBER   EATS", True),
            ("regex", r"^coffee", "Blue Bottle Coffee", False),
            ("regex", r"([unclosed", "Blue Bottle Coffee", False),
            ("contains", "café", "CAFÉ LUNA", True),
        ],
    )
    def test_match_types(self, match_type, pattern, merchant, expected):
        rule = Rule(merchant_pattern=pattern, match_type=match_type, category_id=1)
        assert rule_matches(rule, txn(merchant_name=merchant)) is expected

    def test_merchant_falls_back_to_name(self):
        rule = Rule(merchant_pattern="amazon", match_type="contains", category_id=1)
        assert rule_matches(rule, txn(merchant_name=None, name="AMAZON MKTPLACE"))
        assert not rule_matches(rule, txn(merchant_name="Whole Foods", name="AMAZON MKTPLACE"))

    def test_description_matches_name_or_original(self):
        rule = Rule(description_pattern="aplpay", match_type="contains", category_id=1)
        assert rule_matches(rule, txn(name="APLPAY STARBUCKS"))
        assert rule_matches(
            rule, txn(name="Starbucks", original_description="APLPAY STARBUCKS 123")
        )
        assert not rule_matches(rule, txn(name="Starbucks"))

    def test_amount_bounds_are_inclusive_on_the_signed_amount(self):
        rule = Rule(amount_min=Decimal("50.00"), amount_max=Decimal("100.00"), category_id=1)
        assert rule_matches(rule, txn(amount="50.00"))
        assert rule_matches(rule, txn(amount="100.00"))
        assert not rule_matches(rule, txn(amount="49.99"))
        assert not rule_matches(rule, txn(amount="100.01"))
        assert not rule_matches(rule, txn(amount="-75.00"))
        refunds = Rule(amount_max=Decimal("0"), category_id=1)
        assert rule_matches(refunds, txn(amount="-75.00"))

    def test_all_conditions_must_match(self):
        rule = Rule(
            merchant_pattern="uber",
            description_pattern="eats",
            amount_min=Decimal("10"),
            account_id=7,
            match_type="contains",
            category_id=1,
        )
        good = {"merchant_name": "Uber", "name": "UBER EATS", "amount": "25", "account_id": 7}
        assert rule_matches(rule, txn(**good))
        assert not rule_matches(rule, txn(**{**good, "account_id": 8}))
        assert not rule_matches(rule, txn(**{**good, "amount": "9.99"}))
        assert not rule_matches(rule, txn(**{**good, "name": "UBER TRIP"}))
        assert not rule_matches(rule, txn(**{**good, "merchant_name": "Lyft"}))

    def test_a_rule_without_conditions_matches_nothing(self):
        assert not rule_matches(Rule(category_id=1, match_type="contains"), txn())


class TestRefundRule:
    def test_refund_mapped_to_income_takes_the_merchants_expense_category(self, db):
        account = make_account(db)
        shopping = get_category(db, "Shopping")
        make_transaction(
            db,
            account,
            amount="80.00",
            merchant_name="Zara",
            category=shopping,
            date=dt.date(2026, 9, 1),
        )
        refund = txn(
            amount="-30.00",
            merchant_name="ZARA",
            transaction_code="refund",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_OTHER",
        )
        decision = Categorizer.load(db).decide(refund)
        assert decision.category_id == shopping.id and decision.source == "default"

    def test_refund_without_history_is_uncategorized(self, cz, db):
        refund = txn(
            amount="-30.00",
            merchant_name="New Shop",
            transaction_code="refund",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_DEPOSIT",
        )
        assert cz.decide(refund).category_id == uncategorized(db).id

    def test_refund_in_an_expense_category_keeps_it(self, cz, db):
        refund = txn(
            amount="-12.00",
            transaction_code="refund",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_RESTAURANT",
        )
        decision = cz.decide(refund)
        assert decision.category_id == get_category(db, "Dining & Drinks").id
        assert decision.source == "plaid"

    def test_refund_rule_also_overrides_a_transfer_rule(self, db):
        make_rule(db, card_payments(db), merchant_pattern="zara")
        make_transaction(
            db, amount="50.00", merchant_name="Zara", category=get_category(db, "Shopping")
        )
        decision = Categorizer.load(db).decide(
            txn(amount="-20.00", merchant_name="Zara", transaction_code="refund")
        )
        assert decision.category_id == get_category(db, "Shopping").id


class TestApplyAndRecategorize:
    def test_apply_decision_never_overrides_the_user(self, db):
        home = get_category(db, "Home")
        row = make_transaction(db, category=home, category_source="user")
        assert apply_decision(row, Decision(uncategorized(db).id, "default")) is False
        assert row.category_id == home.id and row.category_source == "user"

    def test_apply_decision_sets_rule_id_only_for_rules(self, db):
        row = make_transaction(db)
        rule = make_rule(db, get_category(db, "Home"))
        assert apply_decision(row, Decision(rule.category_id, "rule", rule.id)) is True
        assert row.rule_id == rule.id
        apply_decision(row, Decision(uncategorized(db).id, "default", rule.id))
        assert row.rule_id is None and row.category_source == "default"

    def _scenario(self, db):
        account = make_account(db)
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        auto = make_transaction(
            db,
            account,
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
            date=dt.date(2026, 9, 5),
        )
        late = make_transaction(
            db,
            account,
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
            date=dt.date(2026, 10, 5),
            category=home,
            category_source="plaid",
        )
        user = make_transaction(
            db,
            account,
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
            category=home,
            category_source="user",
        )
        payment = make_transaction(db, account, amount="-900", name="AUTOPAY PAYMENT - THANK YOU")
        return auto, late, user, payment, groceries, home

    def test_dry_run_counts_without_writing(self, db):
        auto, late, user, payment, groceries, home = self._scenario(db)
        result = recategorize(db, dry_run=True)
        assert result.dry_run and result.examined == 3 and result.changed == 3
        assert result.skipped_user == 1
        db.refresh(auto)
        assert auto.category_id == uncategorized(db).id
        assert {c.transaction_id for c in result.changes} == {auto.id, late.id, payment.id}

    def test_apply_changes_everything_but_user_edits(self, db):
        auto, late, user, payment, groceries, home = self._scenario(db)
        result = recategorize(db)
        assert result.changed == 3 and result.skipped_user == 1
        db.expire_all()
        assert auto.category_id == groceries.id and auto.category_source == "plaid"
        assert late.category_id == groceries.id
        assert payment.category_id == card_payments(db).id and payment.category_source == "system"
        assert user.category_id == home.id and user.category_source == "user"

    def test_scopes(self, db):
        auto, late, user, payment, groceries, home = self._scenario(db)
        since = recategorize(db, start=dt.date(2026, 10, 1), dry_run=True)
        assert {c.transaction_id for c in since.changes} == {late.id, payment.id}
        only = recategorize(db, only_uncategorized=True, dry_run=True)
        assert {c.transaction_id for c in only.changes} == {auto.id, payment.id}

    def test_system_category_id(self, db):
        assert system_category_id(db, "uncategorized") == uncategorized(db).id
        assert system_category_id(db, "card_payments") == card_payments(db).id
        with pytest.raises(LookupError):
            system_category_id(db, "nope")


class TestCreateRule:
    def test_validation(self, db):
        home = get_category(db, "Home")
        cases = [
            ({"category_id": home.id}, "conditions"),
            ({"category_id": home.id, "merchant_pattern": "   "}, "conditions"),
            (
                {"category_id": home.id, "merchant_pattern": "(", "match_type": "regex"},
                "merchant_pattern",
            ),
            ({"category_id": home.id, "amount_min": "10", "amount_max": "5"}, "amount_max"),
            ({"category_id": home.id, "amount_min": "ten"}, "amount_min"),
            (
                {"category_id": home.id, "merchant_pattern": "x", "match_type": "fuzzy"},
                "match_type",
            ),
            ({"category_id": 999999, "merchant_pattern": "x"}, "category_id"),
            ({"category_id": home.id, "account_id": 999999}, "account_id"),
            ({"category_id": home.id, "description_pattern": "x" * 301}, "description_pattern"),
        ]
        for kwargs, field in cases:
            with pytest.raises(RuleValidationError) as info:
                create_rule(db, **kwargs)
            assert field in info.value.errors, (kwargs, info.value.errors)

    def test_creates_a_clean_rule(self, db):
        home = get_category(db, "Home")
        result = create_rule(
            db,
            category_id=home.id,
            merchant_pattern="  IKEA  ",
            amount_min="$20",
            name=" Furniture ",
        )
        rule = result.rule
        assert rule.id is not None and result.applied is None
        assert (rule.merchant_pattern, rule.amount_min, rule.name) == (
            "IKEA",
            Decimal("20.00"),
            "Furniture",
        )
        assert db.scalars(select(Rule).where(Rule.id == rule.id)).one()

    def test_apply_to_existing_skips_user_edits_and_respects_precedence(self, db):
        home, shopping = get_category(db, "Home"), get_category(db, "Shopping")
        account = make_account(db)
        plain = make_transaction(db, account, merchant_name="IKEA")
        edited = make_transaction(
            db, account, merchant_name="IKEA", category=shopping, category_source="user"
        )
        payment = make_transaction(db, account, amount="-50", name="IKEA PAYMENT THANK YOU")
        other = make_transaction(db, account, merchant_name="Target")
        # merchant_pattern falls back to the name for the payment row (no merchant_name)
        result = create_rule(
            db, category_id=home.id, merchant_pattern="ikea", apply_to_existing=True
        )
        assert result.applied.changed == 2  # plain → Home; payment → Card Payments (step 0)
        assert result.applied.skipped_user == 1
        db.expire_all()
        assert plain.category_id == home.id and plain.rule_id == result.rule.id
        assert edited.category_id == shopping.id
        assert payment.category_source == "system"
        assert other.category_id == uncategorized(db).id


def test_amex_category_keys():
    assert amex_category_keys("Restaurant-Bar & Café") == ("Restaurant-Bar & Café", "Restaurant")
    assert amex_category_keys("Transportation - Taxis & Coach") == (
        "Transportation-Taxis & Coach",
        "Transportation",
    )
    assert amex_category_keys("Restaurant – Restaurant") == ("Restaurant-Restaurant", "Restaurant")
    assert amex_category_keys("Merchandise & Supplies > Groceries") == (
        "Merchandise & Supplies-Groceries",
        "Merchandise & Supplies",
    )
    assert amex_category_keys("Other") == ("Other",)
    assert amex_category_keys("  ") == () and amex_category_keys(None) == ()


def test_categorizer_sees_new_categories(db):
    custom = make_category(db, "Pets", kind="expense")
    cz = Categorizer.load(db)
    assert cz.kind_of(custom.id) == "expense"
    late = make_category(db, "Late", kind="income")
    assert cz.kind_of(late.id) == "income"


def test_text_matches_handles_none():
    assert not categorize.text_matches("x", None, "contains")
    assert not categorize.text_matches("", "x", "contains")
