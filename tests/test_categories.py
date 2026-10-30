"""Categories (CAT-1): listing with usage, create/update/hide, and merge."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from budgetapp.db import LEDGER_KEY, LOCK_NS_GLOBAL
from budgetapp.models import (
    AlertEvent,
    Budget,
    Category,
    CategoryMapping,
    Rule,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
)
from budgetapp.periods import month_of
from budgetapp.services import categories, spending, userstate
from budgetapp.services.categories import CategoryError, CategoryNotFound
from budgetapp.services.categorize import Categorizer
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_alert_event,
    make_budget,
    make_category,
    make_mapping,
    make_rule,
    make_split,
    make_statement,
    make_tombstone,
    make_transaction,
    set_app_settings,
    uncategorized,
)

TODAY = dt.date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _today(freeze_today):
    freeze_today(TODAY)


def usage(db, name):
    overview = categories.list_categories(db)
    return next(c for c in overview.categories if c.name == name)


# --------------------------------------------------------------------------- listing


class TestListCategories:
    def test_regular_categories_first_then_the_two_system_ones(self, db):
        names = [c.name for c in categories.list_categories(db).categories]
        assert names[-2:] == ["Uncategorized", "Card Payments"]
        assert names[0] == "Groceries"
        assert "Credits & Rewards" in names

    def test_counts_and_spending_this_period(self, db):
        groceries = get_category(db, "Groceries")
        account = make_account(db)
        make_transaction(db, account, amount="40.00", date="2026-10-02", category=groceries)
        make_transaction(db, account, amount="-5.00", date="2026-10-03", category=groceries)
        make_transaction(db, account, amount="60.00", date="2026-09-30", category=groceries)
        row = usage(db, "Groceries")
        assert row.transaction_count == 3
        assert row.period_amount == Decimal("35.00")
        overview = categories.list_categories(db)
        assert overview.period == month_of(TODAY)
        assert overview.period_total == spending.period_spending(db, month_of(TODAY)).total

    def test_split_parts_count_where_they_go_and_suspects_do_not_count(self, db):
        groceries, home = get_category(db, "Groceries"), get_category(db, "Home")
        account = make_account(db)
        costco = make_transaction(
            db, account, amount="100.00", date="2026-10-05", category=groceries
        )
        make_split(db, costco, groceries, "70.00")
        make_split(db, costco, home, "30.00")
        make_transaction(
            db,
            account,
            amount="9.00",
            date="2026-10-06",
            category=home,
            source="csv",
            duplicate_status="suspect",
        )
        assert usage(db, "Home").transaction_count == 1
        assert usage(db, "Home").period_amount == Decimal("30.00")
        assert usage(db, "Groceries").period_amount == Decimal("70.00")

    def test_only_spending_kinds_have_period_spending(self, db):
        account = make_account(db)
        make_transaction(
            db,
            account,
            amount="-900",
            name="AUTOPAY PAYMENT - THANK YOU",
            category=card_payments(db),
            date="2026-10-04",
        )
        credits = get_category(db, "Credits & Rewards")
        make_transaction(db, account, amount="-25", category=credits, date="2026-10-04")
        assert usage(db, "Card Payments").period_amount == 0
        assert usage(db, "Card Payments").transaction_count == 1
        assert usage(db, "Credits & Rewards").period_amount == 0
        assert not usage(db, "Credits & Rewards").counts_as_spending

    def test_hidden_categories_are_listed_and_still_counted(self, db):
        gifts = get_category(db, "Gifts & Donations")
        make_transaction(db, amount="50.00", date="2026-10-08", category=gifts)
        categories.set_hidden(db, gifts.id, True)
        overview = categories.list_categories(db)
        row = next(c for c in overview.categories if c.id == gifts.id)
        assert row.is_hidden and row.period_amount == Decimal("50.00")
        assert overview.hidden_count == 1
        assert spending.period_spending(db, month_of(TODAY)).amount_for(gifts.id) == Decimal(
            "50.00"
        )

    def test_statement_mode_uses_the_budget_cards_cycle(self, db):
        account = make_account(db, statement_closing_day=27)
        make_statement(db, account, "2026-09-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        period = categories.current_period(db)
        assert (period.start, period.end) == (dt.date(2026, 9, 28), dt.date(2026, 10, 27))
        groceries = get_category(db, "Groceries")
        make_transaction(db, account, amount="12.00", date="2026-09-29", category=groceries)
        make_transaction(db, account, amount="99.00", date="2026-09-27", category=groceries)
        assert usage(db, "Groceries").period_amount == Decimal("12.00")

    def test_statement_mode_without_a_card_is_the_calendar_month(self, db):
        set_app_settings(db, budget_period_mode="statement")
        assert categories.current_period(db) == month_of(TODAY)


# --------------------------------------------------------------------------- create / update


class TestCreate:
    def test_creates_last_with_a_palette_colour(self, db):
        pets = categories.create_category(db, "  Pets  ", "expense", None, "🐾")
        assert pets.name == "Pets" and pets.icon == "🐾" and pets.kind == "expense"
        assert pets.color in categories.PALETTE
        others = db.scalar(
            select(func.max(Category.sort_order)).where(
                Category.system_key.is_(None), Category.id != pets.id
            )
        )
        assert pets.sort_order == others + 10
        assert categories.list_categories(db).categories[-3].name == "Pets"

    def test_suggested_colour_is_the_least_used_slot(self, db):
        db.execute(text("UPDATE categories SET color = 'cat-1' WHERE color = 'cat-5'"))
        db.expire_all()
        assert categories.suggest_color(db) == "cat-5"

    def test_colours(self, db):
        assert categories.create_category(db, "A", color="#9F1239").color == "#9f1239"
        assert categories.create_category(db, "B", color=" cat-16 ").color == "cat-16"
        assert categories.create_category(db, "C", color="other").color == "other"
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, "D", color="red")
        assert err.value.errors == {"color": "Choose one of the colours."}

    @pytest.mark.parametrize("name", ["groceries", "GROCERIES", " Groceries ", "groCERIES"])
    def test_duplicate_names_are_rejected_ignoring_case(self, db, name):
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, name)
        assert err.value.errors["name"] == "There is already a category named “Groceries”."

    def test_whitespace_runs_collapse_before_comparing(self, db):
        categories.create_category(db, "Kids  stuff")
        with pytest.raises(CategoryError):
            categories.create_category(db, "kids stuff")

    def test_every_problem_is_reported_at_once(self, db):
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, "", "spending", "blue", "x" * 40)
        assert set(err.value.errors) == {"name", "kind", "color", "icon"}
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, "x" * 61)
        assert "under 60" in err.value.errors["name"]

    def test_a_clash_the_check_missed_is_still_friendly(self, db, monkeypatch):
        monkeypatch.setattr(categories, "_name_taken", lambda *a, **k: None)
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, "groceries")
        assert "already a category" in err.value.errors["name"]
        # the session is still usable (the savepoint was rolled back)
        assert categories.create_category(db, "Garden").id is not None


class TestUpdate:
    def test_rename_kind_colour_icon_and_order(self, db):
        other = get_category(db, "Other")
        categories.update_category(
            db, other.id, name="Misc", kind="income", color="cat-2", icon="", sort_order=5
        )
        db.expire_all()
        misc = db.get(Category, other.id)
        assert (misc.name, misc.kind, misc.color, misc.icon, misc.sort_order) == (
            "Misc",
            "income",
            "cat-2",
            None,
            5,
        )

    def test_only_the_given_fields_change(self, db):
        travel = get_category(db, "Travel")
        categories.update_category(db, travel.id, icon="🧳")
        assert (travel.name, travel.kind, travel.color, travel.icon) == (
            "Travel",
            "expense",
            "cat-1",
            "🧳",
        )

    def test_renaming_to_its_own_name_in_other_case_is_fine(self, db):
        travel = get_category(db, "Travel")
        categories.update_category(db, travel.id, name="TRAVEL")
        assert travel.name == "TRAVEL"

    def test_renaming_onto_another_name_is_rejected(self, db):
        travel = get_category(db, "Travel")
        with pytest.raises(CategoryError) as err:
            categories.update_category(db, travel.id, name="dining & drinks")
        assert err.value.errors["name"] == "There is already a category named “Dining & Drinks”."
        db.expire_all()
        assert db.get(Category, travel.id).name == "Travel"

    def test_system_categories_keep_their_kind_but_can_be_renamed(self, db):
        payments = card_payments(db)
        with pytest.raises(CategoryError) as err:
            categories.update_category(db, payments.id, kind="expense")
        assert "keeps its kind" in err.value.errors["kind"]
        categories.update_category(db, payments.id, name="Payments", kind="transfer")
        assert payments.name == "Payments" and payments.system_key == "card_payments"

    def test_a_kind_change_moves_money_in_or_out_of_spending(self, db):
        other = get_category(db, "Other")
        make_transaction(db, amount="30.00", date="2026-10-02", category=other)
        assert usage(db, "Other").period_amount == Decimal("30.00")
        categories.update_category(db, other.id, kind="transfer")
        assert usage(db, "Other").period_amount == 0
        assert spending.period_spending(db, month_of(TODAY)).amount_for(other.id) == 0

    @pytest.mark.parametrize("value", ["5", 1.5, True, 2_000_000_000])
    def test_bad_sort_order(self, db, value):
        with pytest.raises(CategoryError):
            categories.update_category(db, get_category(db, "Home").id, sort_order=value)

    def test_unknown_category(self, db):
        with pytest.raises(CategoryNotFound):
            categories.update_category(db, 10**9, name="X")


class TestHide:
    def test_hide_and_show(self, db):
        home = get_category(db, "Home")
        assert categories.set_hidden(db, home.id, True).is_hidden
        assert not categories.set_hidden(db, home.id, False).is_hidden

    @pytest.mark.parametrize("key", ["uncategorized", "card_payments"])
    def test_system_categories_cannot_be_hidden(self, db, key):
        category = uncategorized(db) if key == "uncategorized" else card_payments(db)
        with pytest.raises(CategoryError) as err:
            categories.set_hidden(db, category.id, True)
        assert "can't be hidden" in err.value.errors["is_hidden"]
        assert categories.set_hidden(db, category.id, False).is_hidden is False


# --------------------------------------------------------------------------- merge


def references_to(db, category_id):
    """Every row anywhere that still points at ``category_id``."""
    tables = {
        "transactions": Transaction.category_id,
        "splits": TransactionSplit.category_id,
        "rules": Rule.category_id,
        "mappings": CategoryMapping.category_id,
        "budgets": Budget.category_id,
        "alerts": AlertEvent.category_id,
    }
    found = {
        name: db.scalar(select(func.count()).where(column == category_id))
        for name, column in tables.items()
    }
    found["tombstones"] = sum(
        1
        for t in db.scalars(select(TransactionTombstone))
        if categories._state_mentions(t.user_state, category_id)
    )
    return {k: v for k, v in found.items() if v}


@pytest.fixture
def coffee_world(db):
    """Coffee (to be merged into Dining) referenced from every kind of row."""
    coffee = make_category(db, "Coffee", color="cat-4")
    dining = get_category(db, "Dining & Drinks")
    groceries = get_category(db, "Groceries")
    account = make_account(db)
    auto = make_transaction(db, account, amount="5.50", category=coffee, category_source="plaid")
    edited = make_transaction(db, account, amount="7.00", category=coffee, category_source="user")
    split = make_transaction(db, account, amount="20.00", category=groceries)
    make_split(db, split, groceries, "15.00")
    make_split(db, split, coffee, "5.00")
    rule = make_rule(db, coffee, merchant_pattern="BLUE BOTTLE")
    by_rule = make_transaction(
        db, account, amount="6.00", category=coffee, category_source="rule", rule_id=rule.id
    )
    mapping = make_mapping(db, "plaid_pfc", "FOOD_AND_DRINK_TEA", coffee)
    alert = make_alert_event(db, category_id=coffee.id, kind="budget")
    state = userstate.UserState(
        category_id=coffee.id,
        notes="latte",
        splits=(
            userstate.SplitState(coffee.id, Decimal("3.00")),
            userstate.SplitState(groceries.id, Decimal("2.00")),
        ),
        source_amount=Decimal("5.00"),
    )
    tomb = make_tombstone(db, account, user_state=userstate.to_json(state))
    other_tomb = make_tombstone(
        db, account, user_state=userstate.to_json(userstate.UserState(notes="keep me"))
    )
    return {
        "coffee": coffee,
        "dining": dining,
        "groceries": groceries,
        "auto": auto,
        "edited": edited,
        "split": split,
        "rule": rule,
        "by_rule": by_rule,
        "mapping": mapping,
        "alert": alert,
        "tomb": tomb,
        "other_tomb": other_tomb,
    }


class TestMerge:
    def test_moves_every_reference_and_deletes_the_source(self, db, coffee_world):
        w = coffee_world
        coffee_id, dining_id = w["coffee"].id, w["dining"].id
        result = categories.merge_categories(db, coffee_id, dining_id)
        assert result.source.name == "Coffee" and result.target.name == "Dining & Drinks"
        counts = result.counts
        assert (counts.transactions, counts.splits, counts.rules, counts.mappings) == (3, 1, 1, 1)
        assert (counts.alerts, counts.tombstones) == (1, 1)
        assert db.get(Category, coffee_id) is None
        assert references_to(db, coffee_id) == {}
        auto, edited = db.get(Transaction, w["auto"].id), db.get(Transaction, w["edited"].id)
        assert auto.category_id == dining_id and auto.category_source == "plaid"
        assert edited.category_id == dining_id and edited.category_source == "user"
        by_rule = db.get(Transaction, w["by_rule"].id)
        assert by_rule.category_source == "rule" and by_rule.rule_id == w["rule"].id
        assert sorted(s.category_id for s in db.get(Transaction, w["split"].id).splits) == sorted(
            [w["groceries"].id, dining_id]
        )
        assert db.get(Rule, w["rule"].id).category_id == dining_id
        assert db.get(CategoryMapping, w["mapping"].id).category_id == dining_id
        assert db.get(AlertEvent, w["alert"].id).category_id == dining_id
        moved = userstate.from_json(
            db.get(TransactionTombstone, w["tomb"].plaid_transaction_id).user_state
        )
        assert moved.category_id == dining_id
        assert [s.category_id for s in moved.splits] == [dining_id, w["groceries"].id]
        assert moved.notes == "latte"
        untouched = db.get(TransactionTombstone, w["other_tomb"].plaid_transaction_id)
        assert userstate.from_json(untouched.user_state).notes == "keep me"

    def test_preview_counts_what_merge_moves(self, db, coffee_world):
        w = coffee_world
        preview = categories.merge_preview(db, w["coffee"].id, w["dining"].id)
        assert not preview.kind_changes
        result = categories.merge_categories(db, w["coffee"].id, w["dining"].id)
        assert preview.counts == result.counts
        assert db.get(Category, w["coffee"].id) is None

    def test_spending_follows_the_merge(self, db, coffee_world):
        w = coffee_world
        before = spending.period_spending(db, month_of(TODAY))
        total_before = before.amount_for(w["coffee"].id) + before.amount_for(w["dining"].id)
        categories.merge_categories(db, w["coffee"].id, w["dining"].id)
        after = spending.period_spending(db, month_of(TODAY))
        assert after.amount_for(w["dining"].id) == total_before
        assert after.total == before.total

    def test_new_transactions_follow_moved_mappings_and_rules(self, db, coffee_world):
        w = coffee_world
        categories.merge_categories(db, w["coffee"].id, w["dining"].id)
        cz = Categorizer.load(db)
        tea = make_transaction(
            db, amount="4.00", pfc_primary="FOOD_AND_DRINK", pfc_detailed="FOOD_AND_DRINK_TEA"
        )
        assert cz.decide(tea).category_id == w["dining"].id
        blue = make_transaction(db, amount="4.00", name="BLUE BOTTLE #4")
        decision = cz.decide(blue)
        assert decision.category_id == w["dining"].id and decision.source == "rule"

    def test_budgets_target_keeps_its_own(self, db, coffee_world):
        w = coffee_world
        jan = dt.date(2026, 1, 1)
        theirs = make_budget(db, w["dining"], "400.00", effective_from=jan)
        make_budget(db, w["coffee"], "80.00", effective_from=jan)
        make_budget(db, w["coffee"], "90.00", effective_from=dt.date(2026, 3, 1))
        overall = make_budget(db, None, "3000.00", effective_from=jan)
        preview = categories.merge_preview(db, w["coffee"].id, w["dining"].id)
        assert (preview.counts.budgets_moved, preview.counts.budgets_dropped) == (0, 2)
        categories.merge_categories(db, w["coffee"].id, w["dining"].id)
        rows = db.scalars(select(Budget).where(Budget.category_id == w["dining"].id)).all()
        assert [(b.id, b.amount) for b in rows] == [(theirs.id, Decimal("400.00"))]
        assert db.get(Budget, overall.id).scope == "overall"

    def test_budgets_move_when_the_target_has_none(self, db, coffee_world):
        w = coffee_world
        mine = make_budget(db, w["coffee"], "80.00", rollover=True)
        annual = make_budget(db, w["coffee"], None, period="annual")
        result = categories.merge_categories(db, w["coffee"].id, w["dining"].id)
        assert (result.counts.budgets_moved, result.counts.budgets_dropped) == (2, 0)
        moved = db.get(Budget, mine.id)
        assert moved.category_id == w["dining"].id and moved.rollover
        assert db.get(Budget, annual.id).category_id == w["dining"].id

    def test_merging_into_a_system_or_hidden_category_works(self, db):
        stray = make_category(db, "Stray")
        make_transaction(db, amount="3.00", category=stray)
        result = categories.merge_categories(db, stray.id, uncategorized(db).id)
        assert result.counts.transactions == 1
        hidden = make_category(db, "Old", is_hidden=True)
        stray2 = make_category(db, "Stray 2")
        assert categories.merge_categories(db, stray2.id, hidden.id).target.is_hidden

    def test_a_kind_change_is_flagged(self, db):
        refunds = make_category(db, "Refunds", kind="income")
        preview = categories.merge_preview(db, refunds.id, get_category(db, "Shopping").id)
        assert preview.kind_changes

    @pytest.mark.parametrize("key", ["uncategorized", "card_payments"])
    def test_system_categories_cannot_be_merged_away(self, db, key):
        source = uncategorized(db) if key == "uncategorized" else card_payments(db)
        with pytest.raises(CategoryError) as err:
            categories.merge_categories(db, source.id, get_category(db, "Other").id)
        assert "built in" in err.value.errors["target_id"]
        assert db.get(Category, source.id) is not None

    def test_merging_into_itself_is_an_error(self, db):
        home = get_category(db, "Home")
        with pytest.raises(CategoryError) as err:
            categories.merge_categories(db, home.id, home.id)
        assert "into itself" in err.value.errors["target_id"]

    def test_missing_categories(self, db):
        home = get_category(db, "Home")
        with pytest.raises(CategoryError) as err:
            categories.merge_preview(db, home.id, None)
        assert "Choose the category" in err.value.errors["target_id"]
        with pytest.raises(CategoryError):
            categories.merge_categories(db, home.id, 10**9)
        with pytest.raises(CategoryNotFound):
            categories.merge_categories(db, 10**9, home.id)

    def test_an_unused_category_merges_cleanly(self, db):
        empty = make_category(db, "Empty")
        result = categories.merge_categories(db, empty.id, get_category(db, "Home").id)
        assert result.counts.is_empty
        assert db.get(Category, empty.id) is None

    def test_merge_takes_the_ledger_lock(self, db):
        lock = text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND pid = pg_backend_pid() AND classid = :ns AND objid = :key AND objsubid = 2"
        )
        params = {"ns": LOCK_NS_GLOBAL, "key": LEDGER_KEY}
        empty = make_category(db, "Empty")
        assert not db.scalar(lock, params)
        categories.merge_categories(db, empty.id, get_category(db, "Home").id)
        assert db.scalar(lock, params) == 1


def budget_rows(db, category):
    return db.execute(
        select(Budget.period, Budget.amount, Budget.rollover, Budget.effective_from)
        .where(Budget.category_id == category.id)
        .order_by(Budget.effective_from, Budget.period)
    ).all()


class TestMergeBudgetsInEffect:
    """The merged category keeps a budget if either side has one now (ARCHITECTURE §5: the
    amount for a period is its latest row up to the period's end; null means "no budget")."""

    def test_a_target_whose_budget_ended_takes_the_sources_current_budget(self, db):
        coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
        make_budget(db, coffee, "50.00", effective_from=dt.date(2026, 1, 1))
        make_budget(db, coffee, None, effective_from=dt.date(2026, 6, 1))  # removed in June
        make_budget(db, cafes, "80.00", effective_from=dt.date(2026, 1, 1))
        make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 3, 1), rollover=True)
        preview = categories.merge_preview(db, cafes.id, coffee.id)
        plan = preview.counts.budgets
        assert (plan.action, plan.period, plan.amount, plan.rollover, plan.effective_from) == (
            "take_source",
            "monthly",
            Decimal("100.00"),
            True,
            dt.date(2026, 10, 1),
        )
        assert (preview.counts.budgets_moved, preview.counts.budgets_dropped) == (0, 2)
        cafes_id = cafes.id
        result = categories.merge_categories(db, cafes.id, coffee.id)
        assert result.counts == preview.counts
        assert budget_rows(db, coffee) == [
            ("monthly", Decimal("50.00"), False, dt.date(2026, 1, 1)),
            ("monthly", None, False, dt.date(2026, 6, 1)),
            ("monthly", Decimal("100.00"), True, dt.date(2026, 10, 1)),
        ]
        assert not db.scalar(select(func.count()).where(Budget.category_id == cafes_id))

    def test_a_budget_ended_this_period_is_replaced_in_place(self, db):
        coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
        make_budget(db, coffee, "50.00", effective_from=dt.date(2026, 1, 1))
        ended = make_budget(db, coffee, None, effective_from=dt.date(2026, 10, 1))
        make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 1, 1))
        categories.merge_categories(db, cafes.id, coffee.id)
        assert budget_rows(db, coffee) == [
            ("monthly", Decimal("50.00"), False, dt.date(2026, 1, 1)),
            ("monthly", Decimal("100.00"), False, dt.date(2026, 10, 1)),
        ]
        assert db.get(Budget, ended.id).amount == Decimal("100.00")

    def test_an_annual_budget_carries_over_and_ends_the_monthly_one(self, db):
        presents, gifts = make_category(db, "Presents"), make_category(db, "Gift ideas")
        make_budget(db, presents, "50.00", effective_from=dt.date(2026, 1, 1))
        make_budget(db, presents, None, effective_from=dt.date(2026, 10, 15))  # mid-month end
        make_budget(db, gifts, "1200.00", effective_from=dt.date(2026, 1, 1), period="annual")
        categories.merge_categories(db, gifts.id, presents.id)
        assert budget_rows(db, presents) == [
            ("monthly", Decimal("50.00"), False, dt.date(2026, 1, 1)),
            ("annual", Decimal("1200.00"), False, dt.date(2026, 10, 1)),
            ("monthly", None, False, dt.date(2026, 10, 1)),  # one active period type
            ("monthly", None, False, dt.date(2026, 10, 15)),
        ]

    def test_neither_has_a_budget_now(self, db):
        coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
        mine = make_budget(db, coffee, None, effective_from=dt.date(2026, 6, 1))
        make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 1, 1))
        make_budget(db, cafes, None, effective_from=dt.date(2026, 5, 1))
        preview = categories.merge_preview(db, cafes.id, coffee.id)
        assert preview.counts.budgets.action == "drop"
        categories.merge_categories(db, cafes.id, coffee.id)
        assert [
            b.id for b in db.scalars(select(Budget).where(Budget.category_id == coffee.id))
        ] == [mine.id]

    def test_a_target_with_a_budget_now_keeps_it(self, db):
        coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
        make_budget(db, coffee, None, effective_from=dt.date(2026, 1, 1))
        make_budget(db, coffee, "60.00", effective_from=dt.date(2026, 9, 1))  # started again
        make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 1, 1))
        assert categories.merge_preview(db, cafes.id, coffee.id).counts.budgets.action == (
            "keep_target"
        )
        categories.merge_categories(db, cafes.id, coffee.id)
        assert [r.amount for r in budget_rows(db, coffee)] == [None, Decimal("60.00")]

    def test_statement_mode_starts_the_budget_mid_cycle_like_a_budget_edit(self, db):
        """The Budgets page writes a cycle's edit at its middle day (a cycle start moves when a
        closing date is corrected); the merge does the same."""
        account = make_account(db, statement_closing_day=27)
        make_statement(db, account, "2026-09-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
        make_budget(db, coffee, None, effective_from=dt.date(2026, 1, 1))
        make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 1, 1))
        result = categories.merge_categories(db, cafes.id, coffee.id)  # cycle Sep 28 - Oct 27
        assert result.counts.budgets.effective_from == dt.date(2026, 10, 12)
        assert budget_rows(db, coffee)[-1] == (
            "monthly",
            Decimal("100.00"),
            False,
            dt.date(2026, 10, 12),
        )

    def test_an_annual_budget_never_reaches_back_into_last_year(self, db, freeze_today):
        freeze_today(dt.date(2027, 1, 2))
        account = make_account(db, statement_closing_day=9)
        make_statement(db, account, "2026-12-09")  # this cycle: Dec 10, 2026 - Jan 9, 2027
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        presents, gifts = make_category(db, "Presents"), make_category(db, "Gift ideas")
        make_budget(db, presents, None, effective_from=dt.date(2026, 1, 1), period="annual")
        make_budget(db, gifts, "1200.00", effective_from=dt.date(2026, 1, 1), period="annual")
        categories.merge_categories(db, gifts.id, presents.id)
        assert budget_rows(db, presents) == [
            ("annual", None, False, dt.date(2026, 1, 1)),  # 2026 keeps no annual budget
            ("annual", Decimal("1200.00"), False, dt.date(2027, 1, 1)),
        ]


class TestIcons:
    @pytest.mark.parametrize(
        "icon",
        [
            "\U0001f469\u200d\U0001f4bb",  # technologist: woman + ZWJ + laptop
            "\U0001f9d1\u200d\U0001f373",  # cook
            "\U0001f3f3\ufe0f\u200d\U0001f308",  # rainbow flag
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # family
            # Scotland: a black flag and tag characters
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
            "\U0001f44d\U0001f3fd",  # thumbs up with a skin tone
            "1\ufe0f\u20e3",  # keycap 1
            "\U0001f43e",  # paw prints
        ],
    )
    def test_joined_and_flag_emoji_are_one_icon(self, db, icon):
        created = categories.create_category(db, f"Icon {len(icon)} {ord(icon[-1])}", icon=icon)
        assert created.icon == icon

    @pytest.mark.parametrize(
        "icon",
        [
            "\u202eevil",  # right-to-left override
            "a\u200bb",  # zero-width space
            "x\u000ay",  # line feed
            "\u0007",  # bell
            "a\u2028b",  # line separator
            "\u200d",  # a joiner alone
            "\ufeffx",  # byte order mark
            "\ufe0f",  # a variation selector alone
        ],
    )
    def test_invisible_controls_are_refused(self, db, icon):
        with pytest.raises(CategoryError) as err:
            categories.create_category(db, "Odd icon", icon=icon)
        assert err.value.errors == {"icon": "Use one emoji or a few characters."}


def test_ids_beyond_bigint_name_no_category(db):
    huge = 2**63
    with pytest.raises(CategoryNotFound):
        categories.get_category(db, huge)
    with pytest.raises(CategoryNotFound):
        categories.merge_categories(db, huge, get_category(db, "Home").id)
    with pytest.raises(CategoryError) as err:
        categories.merge_preview(db, get_category(db, "Home").id, huge)
    assert "no longer exists" in err.value.errors["target_id"]
    with pytest.raises(CategoryNotFound):
        categories.set_hidden(db, -1, True)
