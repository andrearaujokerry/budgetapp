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
from tests.bank_factories import make_bank_account
from tests.bounded import run_bounded
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


class TestRegexTimeLimit:
    """A regex rule can't freeze the app. Python's ``re`` has no time limit, and a pattern such as
    ``(.+)+X`` backtracked for minutes holding the GIL, so every request waited."""

    @pytest.mark.parametrize(
        ("pattern", "text"),
        [
            (r"(.+)+X", "UBER *TRIP HELP.UBER.COM CA"),
            (r"^(.*\s*)*Z$", "UBER *TRIP HELP.UBER.COM CA"),
            (r"^(a|aa)+$", "a" * 60 + "!"),  # too slow even for the regex package
        ],
    )
    def test_a_runaway_pattern_gives_up_and_then_matches_nothing(self, pattern, text):
        out = run_bounded(
            f"""
            import time
            from budgetapp.services.categorize import text_matches
            start = time.monotonic()
            first = text_matches({pattern!r}, {text!r}, "regex")
            middle = time.monotonic()
            again = text_matches({pattern!r}, {text!r}, "regex")
            print(first, again, middle - start, time.monotonic() - middle)
            """
        )
        assert out is not None, f"{pattern!r} ran for over 5 s"
        first, again, took, took_again = out.split()
        assert (first, again) == ("False", "False")
        assert float(took) < 1 and float(took_again) < 0.01

    @pytest.mark.parametrize("field", ["merchant_pattern", "description_pattern"])
    def test_saving_refuses_a_pattern_that_runs_too_long(self, db, field):
        with pytest.raises(RuleValidationError) as info:
            categorize.validate_rule(
                db,
                category_id=get_category(db, "Home").id,
                match_type="regex",
                **{field: r"^(a|aa)+$"},
            )
        assert info.value.errors == {field: categorize.REGEX_TOO_SLOW}

    def test_a_quick_pattern_is_still_fine(self, db):
        values = categorize.validate_rule(
            db,
            category_id=get_category(db, "Home").id,
            match_type="regex",
            merchant_pattern=r"^(uber|lyft)\b.*(trip|ride)?",
        )
        assert values["merchant_pattern"] == r"^(uber|lyft)\b.*(trip|ride)?"


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


class TestCarriedState:
    """``apply_carried_state``: one precedence for every carry path, whatever the call order."""

    def _state(self, db, *, category="Home", splits=(), notes="from the old row"):
        from budgetapp.services.userstate import SplitState

        return UserState(
            category_id=get_category(db, category).id if category else None,
            notes=notes,
            tags=("carried",),
            splits=tuple(SplitState(get_category(db, c).id, Decimal(a)) for c, a in splits),
            source_amount=Decimal("-25.00"),
        )

    def test_step_zero_beats_the_carried_category_and_says_so(self, cz, db):
        posted = make_transaction(db, amount="-25.00", name="AUTOPAY REFUND COMCAST")
        flagged = categorize.apply_carried_state(cz, posted, self._state(db))
        assert flagged and posted.needs_review and "card payment" in posted.review_reason
        assert (posted.category_id, posted.category_source) == (card_payments(db).id, "system")
        assert posted.notes == "from the old row" and posted.tags == ["carried"]

    def test_the_old_documented_call_order_gives_the_same_answer(self, cz, db):
        """decide(inherited=…) + apply_decision + userstate.apply no longer lets the carried
        category override step 0."""
        from budgetapp.services import userstate

        state = self._state(db)
        posted = make_transaction(db, amount="-25.00", name="AUTOPAY REFUND COMCAST")
        apply_decision(posted, cz.decide(posted, inherited=state))
        userstate.apply(posted, state)
        assert posted.category_source == "system" and posted.needs_review

        reverse = make_transaction(db, amount="-25.00", name="AUTOPAY REFUND COMCAST")
        userstate.apply(reverse, state)  # state first, categorize afterwards
        apply_decision(reverse, cz.decide(reverse, inherited=state))
        assert reverse.category_source == "system" and reverse.needs_review

    def test_a_payment_never_takes_carried_splits(self, cz, db):
        state = self._state(db, category=None, splits=(("Groceries", "-60.00"), ("Home", "-40.00")))
        payment = make_transaction(db, amount="-100.00", name="CREDIT", pfc_primary="TRANSFER_IN")
        assert categorize.apply_carried_state(cz, payment, state) is True
        db.flush()
        assert payment.splits == [] and payment.category_source == "system"
        assert "splits" in payment.review_reason

    def test_carried_splits_on_a_payment_never_reduce_spending(self, cz, db):
        from budgetapp.periods import month_period
        from budgetapp.services.spending import period_spending

        groceries = get_category(db, "Groceries")
        make_transaction(db, amount="100.00", category=groceries, date="2026-10-01")
        state = self._state(db, category=None, splits=(("Groceries", "-60.00"), ("Home", "-40.00")))
        payment = make_transaction(
            db, amount="-100.00", name="CREDIT", pfc_primary="TRANSFER_IN", date="2026-10-02"
        )
        categorize.apply_carried_state(cz, payment, state)
        db.flush()
        assert period_spending(db, month_period(2026, 10)).total == Decimal("100.00")

    def test_a_non_payment_takes_the_carried_category_and_splits(self, cz, db):
        state = self._state(db, splits=(("Groceries", "15.00"), ("Home", "10.00")))
        posted = make_transaction(db, amount="25.00", pfc_primary="FOOD_AND_DRINK")
        assert categorize.apply_carried_state(cz, posted, state) is False
        db.flush()
        assert (posted.category_id, posted.category_source) == (get_category(db, "Home").id, "user")
        assert [s.amount for s in posted.splits] == [Decimal("15.00"), Decimal("10.00")]

    def test_a_row_categorized_at_insert_still_inherits_the_users_category(self, cz, db):
        """§6 step 5: an unlinked replacement was categorized when inserted, then matched."""
        row = make_transaction(
            db, amount="30.00", pfc_primary="FOOD_AND_DRINK", pfc_detailed="FOOD_AND_DRINK_COFFEE"
        )
        apply_decision(row, cz.decide(row))
        assert row.category_source == "plaid"
        categorize.apply_carried_state(cz, row, self._state(db))
        assert (row.category_id, row.category_source) == (get_category(db, "Home").id, "user")

    def test_the_targets_own_choice_wins(self, cz, db):
        """CSV pairing: the Plaid row's own category is kept; notes from both are kept."""
        travel = get_category(db, "Travel")
        plaid_row = make_transaction(
            db, amount="25.00", category=travel, category_source="user", notes="mine"
        )
        state = self._state(db, splits=(("Groceries", "15.00"), ("Home", "10.00")))
        assert categorize.apply_carried_state(cz, plaid_row, state) is False
        db.flush()
        assert (plaid_row.category_id, plaid_row.category_source) == (travel.id, "user")
        assert plaid_row.splits == []
        assert plaid_row.notes == "from the old row\n\nmine"

    def test_no_state_just_categorizes(self, cz, db):
        row = make_transaction(db, amount="-500.00", name="ONLINE PAYMENT - THANK YOU")
        assert categorize.apply_carried_state(cz, row, None) is False
        assert row.category_source == "system" and not row.needs_review


class TestSignAwareMapping:
    def _csv(self, db, amount, name, category="Fees & Adjustments-Fees & Adjustments"):
        return make_transaction(
            db,
            make_account(db, manual=True),
            source="csv",
            amount=amount,
            name=name,
            external_category=category,
            date="2026-10-06",
        )

    def test_amex_cash_back_is_a_credit_not_negative_fee_spending(self, cz, db):
        from budgetapp.periods import month_period
        from budgetapp.services.spending import period_spending

        fee = self._csv(db, "95.00", "ANNUAL MEMBERSHIP FEE")
        cash_back = self._csv(db, "-150.00", "YOUR CASH BACK THIS PERIOD IS")
        inquiry = self._csv(db, "-20.00", "CREDIT ADJUSTMENT FOR BILLING INQUIRY")
        reversal = self._csv(db, "-95.00", "ANNUAL MEMBERSHIP FEE REVERSAL")
        for row in (fee, cash_back, inquiry, reversal):
            apply_decision(row, cz.decide(row))
        db.flush()
        credits, fees = get_category(db, "Credits & Rewards"), get_category(db, "Fees & Interest")
        assert fee.category_id == fees.id and reversal.category_id == fees.id
        assert cash_back.category_id == credits.id and inquiry.category_id == credits.id
        assert cash_back.category_source == "amex"
        spent = period_spending(db, month_period(2026, 10))
        assert spent.amount_for(fees.id) == Decimal("0.00")  # the fee and its reversal
        assert spent.total == Decimal("0.00")  # credits never count as (negative) spending

    def test_a_negative_row_falls_back_to_the_plain_mapping(self, cz):
        refund = cz.decide(
            txn(source="csv", amount="-12.00", external_category="Restaurant-Restaurant")
        )
        assert refund.source == "amex" and cz.kind_of(refund.category_id) == "expense"

    def test_a_positive_charge_never_lands_in_a_transfer_category(self, cz, db):
        decision = cz.decide(
            txn(
                amount="300.00",
                name="ACME COLLECTIONS",
                pfc_primary="LOAN_PAYMENTS",
                pfc_detailed="LOAN_PAYMENTS_OTHER_PAYMENT",
            )
        )
        assert (decision.category_id, decision.source) == (uncategorized(db).id, "default")
        assert "transfer" in decision.reason

    def test_a_returned_card_payment_stays_a_payment(self, cz, db):
        returned = cz.decide(
            txn(
                amount="500.00",
                name="AUTOPAY RETURN",
                pfc_primary="LOAN_PAYMENTS",
                pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            )
        )
        assert returned.category_id == card_payments(db).id

    def test_negative_transfer_codes_are_still_payments(self, cz, db):
        payment = cz.decide(
            txn(
                amount="-300.00",
                name="ACME",
                pfc_primary="LOAN_PAYMENTS",
                pfc_detailed="LOAN_PAYMENTS_OTHER_PAYMENT",
            )
        )
        assert payment.category_id == card_payments(db).id and payment.source == "plaid"

    def test_recategorize_moves_old_positive_transfer_rows(self, db):
        row = make_transaction(
            db,
            amount="300.00",
            category=card_payments(db),
            category_source="plaid",
            pfc_primary="LOAN_PAYMENTS",
            pfc_detailed="LOAN_PAYMENTS_OTHER_PAYMENT",
        )
        assert recategorize(db).changed >= 1
        assert row.category_id == uncategorized(db).id


# --------------------------------------------------------------------------- bank accounts (C3)


class TestBankAccounts:
    """MON-1 / W3-PLAN C3: bank money goes to the income, transfer and cash categories."""

    @pytest.fixture
    def checking(self, db):
        return make_bank_account(db)

    @pytest.fixture
    def savings(self, db, checking):
        return make_bank_account(db, checking.item, subtype="savings")

    def _decide(self, db, account, **fields):
        row = make_transaction(db, account, **fields)
        return Categorizer.load(db).decide(row)

    @pytest.mark.parametrize(
        ("primary", "detailed", "amount", "category"),
        [
            ("INCOME", "INCOME_SALARY", "-3412.18", "Paycheck"),
            ("INCOME", "INCOME_WAGES", "-3412.18", "Paycheck"),  # PFC v1's name
            ("INCOME", "INCOME_INTEREST_EARNED", "-48.12", "Interest"),
            ("INCOME", "INCOME_DIVIDENDS", "-12.40", "Interest"),
            ("INCOME", "INCOME_TAX_REFUND", "-812.00", "Other Income"),
            ("INCOME", "INCOME_OTHER", "-40.00", "Other Income"),
            ("INCOME", "INCOME_SOMETHING_NEW", "-40.00", "Other Income"),  # falls to INCOME|bank
            ("INCOME", None, "-40.00", "Other Income"),
            ("TRANSFER_IN", "TRANSFER_IN_SAVINGS", "-500.00", "Transfers"),
            ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER", "-500.00", "Transfers"),
            ("TRANSFER_IN", "TRANSFER_IN_DEPOSIT", "-120.00", "Transfers"),
            ("TRANSFER_OUT", "TRANSFER_OUT_SAVINGS", "500.00", "Transfers"),
            ("TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER", "500.00", "Transfers"),
            ("TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL", "80.00", "Cash & ATM"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "2512.37", "Card Payments"),
            ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", "42.10", "Groceries"),  # as for cards
            ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", "-8.00", "Groceries"),  # a refund
            ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT", "2450.00", "Home"),
            ("BANK_FEES", "BANK_FEES_OVERDRAFT_FEES", "35.00", "Fees & Interest"),
        ],
    )
    def test_bank_rows_follow_the_bank_mappings(
        self, db, checking, primary, detailed, amount, category
    ):
        decision = self._decide(
            db, checking, amount=amount, pfc_primary=primary, pfc_detailed=detailed
        )
        assert (decision.category_id, decision.source) == (get_category(db, category).id, "plaid")
        if detailed is not None and detailed != "INCOME_SOMETHING_NEW":
            assert "(bank accounts)" in decision.reason or category in {
                "Groceries",
                "Home",
                "Fees & Interest",
            }

    def test_the_new_categories_have_the_intended_kinds(self, db):
        kinds = {name: get_category(db, name).kind for name in categorize_kinds()}
        assert kinds == {
            "Paycheck": "income",
            "Interest": "income",
            "Other Income": "income",
            "Transfers": "transfer",
            "Cash & ATM": "expense",
        }

    @pytest.mark.parametrize(
        ("primary", "detailed", "amount", "category", "source"),
        [
            ("INCOME", "INCOME_SALARY", "-3412.18", "Credits & Rewards", "plaid"),
            ("INCOME", "INCOME_INTEREST_EARNED", "-4.12", "Credits & Rewards", "plaid"),
            ("TRANSFER_IN", "TRANSFER_IN_SAVINGS", "-500.00", "Card Payments", "system"),
            ("TRANSFER_OUT", "TRANSFER_OUT_SAVINGS", "500.00", "Other", "plaid"),
            ("TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL", "80.00", "Other", "plaid"),
            (
                "LOAN_PAYMENTS",
                "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                "-2512.37",
                "Card Payments",
                "system",
            ),
        ],
    )
    def test_cards_are_unchanged(self, db, primary, detailed, amount, category, source):
        card = make_account(db)
        decision = self._decide(db, card, amount=amount, pfc_primary=primary, pfc_detailed=detailed)
        assert (decision.category_id, decision.source) == (get_category(db, category).id, source)

    def test_step_zero_is_for_cards_only(self, db, checking):
        """A transfer in from savings that reads like a payment stays a transfer on a bank."""
        fields = {
            "amount": "-500.00",
            "name": "AUTOPAY TRANSFER FROM SAV 7702",
            "transaction_code": "payment",
            "pfc_primary": "TRANSFER_IN",
            "pfc_detailed": "TRANSFER_IN_ACCOUNT_TRANSFER",
        }
        on_bank = self._decide(db, checking, **fields)
        assert (on_bank.category_id, on_bank.source) == (get_category(db, "Transfers").id, "plaid")
        on_card = self._decide(db, make_account(db), **fields)
        assert (on_card.category_id, on_card.source) == (card_payments(db).id, "system")

    def test_a_positive_bank_row_may_be_a_transfer(self, db, checking):
        """The guard that sends a positive row out of a transfer category is for cards only."""
        cz = Categorizer.load(db)
        out = make_transaction(
            db,
            checking,
            amount="300.00",
            pfc_primary="LOAN_PAYMENTS",
            pfc_detailed="LOAN_PAYMENTS_OTHER_PAYMENT",
        )
        assert cz.decide(out).category_id == card_payments(db).id  # the card mapping, kept
        same_on_card = make_transaction(
            db,
            amount="300.00",
            pfc_primary="LOAN_PAYMENTS",
            pfc_detailed="LOAN_PAYMENTS_OTHER_PAYMENT",
        )
        assert cz.decide(same_on_card).category_id == uncategorized(db).id

    def test_rules_come_first_so_a_transfer_can_become_income(self, db, savings):
        other_income = get_category(db, "Other Income")
        rule = make_rule(db, other_income, description_pattern="VENMO CASHOUT")
        decision = self._decide(
            db,
            savings,
            amount="-75.00",
            name="VENMO CASHOUT",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_TRANSFER_IN_FROM_APPS",
        )
        assert decision == Decision(other_income.id, "rule", rule.id, reason=f"rule {rule.id}")

    def test_the_refund_rule_still_applies_on_a_bank(self, db, checking):
        groceries = get_category(db, "Groceries")
        make_transaction(
            db, checking, amount="40.00", merchant_name="Rainbow Grocery", category=groceries
        )
        decision = self._decide(
            db,
            checking,
            amount="-12.00",
            merchant_name="Rainbow Grocery",
            transaction_code="refund",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_DEPOSIT",
        )
        assert (decision.category_id, decision.source) == (groceries.id, "default")

    def test_an_account_added_after_loading_is_still_a_bank(self, db):
        cz = Categorizer.load(db)
        checking = make_bank_account(db)  # e.g. stored by the sync that brought the row
        row = make_transaction(
            db, checking, amount="-3412.18", pfc_primary="INCOME", pfc_detailed="INCOME_SALARY"
        )
        assert cz.on_bank(row) and cz.decide(row).category_id == get_category(db, "Paycheck").id

    def test_an_untyped_or_manual_account_counts_as_a_card(self, db):
        untyped = make_account(db, type=None, subtype=None)
        manual = make_account(db, manual=True)
        cz = Categorizer.load(db)
        for account in (untyped, manual):
            row = make_transaction(db, account, amount="-500.00", pfc_primary="TRANSFER_IN")
            assert not cz.on_bank(row) and cz.decide(row).source == "system"

    def test_is_payment_reads_the_rows_account(self, db, checking):
        fields = {"amount": "-500.00", "name": "ONLINE PAYMENT - THANK YOU"}
        assert not is_payment(make_transaction(db, checking, **fields))
        assert is_payment(make_transaction(db, **fields))
        assert is_payment(txn(**fields))  # not a stored row: a card unless told otherwise
        assert not is_payment(txn(**fields), bank=True)
        loose = categorize_transaction(account=checking, **fields)  # not in a session yet
        assert not is_payment(loose)

    def test_a_carried_category_lands_on_a_bank_transfer(self, db, savings):
        """A pending transfer the owner called income keeps that category when it posts (step 0
        would drop it on a card) and is not flagged."""
        other_income = get_category(db, "Other Income")
        state = UserState(category_id=other_income.id, notes=None, tags=(), splits=())
        posted = make_transaction(
            db,
            savings,
            amount="-75.00",
            name="ZELLE FROM SAM RIVERA",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
        )
        assert categorize.apply_carried_state(Categorizer.load(db), posted, state) is False
        assert (posted.category_id, posted.category_source) == (other_income.id, "user")
        assert not posted.needs_review

    def test_recategorize_moves_bank_rows_to_the_bank_mappings(self, db, checking):
        credits = get_category(db, "Credits & Rewards")
        paycheck = make_transaction(
            db,
            checking,
            amount="-3412.18",
            category=credits,
            category_source="plaid",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_SALARY",
        )
        card_credit = make_transaction(
            db,
            amount="-25.00",
            category=credits,
            category_source="plaid",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_OTHER",
        )
        result = recategorize(db, transaction_ids=[paycheck.id, card_credit.id])
        assert result.changed == 1
        assert paycheck.category_id == get_category(db, "Paycheck").id
        assert card_credit.category_id == credits.id

    def test_editing_a_bank_mapping_leaves_cards_alone(self, db, checking):
        from budgetapp.services import categories

        salary = make_category(db, "Salary", kind="income")
        change = categories.set_mapping(db, "plaid_pfc", "INCOME_SALARY|bank", salary.id)
        assert change.label == "Salary (bank accounts)" and change.key == "INCOME_SALARY|bank"
        on_bank = self._decide(
            db, checking, amount="-3412.18", pfc_primary="INCOME", pfc_detailed="INCOME_SALARY"
        )
        on_card = self._decide(
            db,
            make_account(db),
            amount="-30.00",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_SALARY",
        )
        assert on_bank.category_id == salary.id
        assert on_card.category_id == get_category(db, "Credits & Rewards").id

    def test_amex_csv_rows_never_use_bank_keys(self, db):
        manual = make_account(db, manual=True)
        row = make_transaction(
            db,
            manual,
            source="csv",
            amount="12.00",
            external_category="Restaurant-Restaurant",
        )
        assert (
            Categorizer.load(db).decide(row).category_id == get_category(db, "Dining & Drinks").id
        )


def categorize_kinds() -> tuple[str, ...]:
    return ("Paycheck", "Interest", "Other Income", "Transfers", "Cash & ATM")


def categorize_transaction(**fields):
    """A ``Transaction`` that is in no session (its account given as an object)."""
    from budgetapp.models import Transaction

    amount = fields.pop("amount")
    return Transaction(source="plaid", amount=Decimal(amount), **fields)
