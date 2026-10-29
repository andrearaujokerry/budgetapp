"""The Plaid & Amex category mapping editor (CAT-2): listing, counts, changes and re-applying."""

import pytest
from sqlalchemy import func, select

from budgetapp.models import CategoryMapping, Transaction
from budgetapp.services import categories, rules
from budgetapp.services.categories import MappingError
from budgetapp.services.categorize import Categorizer
from tests.factories import get_category, make_account, make_transaction, uncategorized


def row(listing, key):
    return next(r for r in listing.rows if r.key.casefold() == key.casefold())


def group(listing, label):
    return next(g for g in listing.groups if g.label == label)


def plaid(db, account, detailed, primary=None, amount="10.00", **kw):
    return make_transaction(
        db,
        account,
        amount=amount,
        pfc_detailed=detailed,
        pfc_primary=primary or (detailed.split("_AND_")[0] if detailed else None),
        **kw,
    )


def amex(db, account, amex_category, amount="10.00", **kw):
    return make_transaction(
        db, account, amount=amount, source="csv", external_category=amex_category, **kw
    )


class TestPlaidListing:
    def test_every_seeded_code_grouped_by_primary(self, db):
        listing = categories.list_mappings(db, "plaid_pfc")
        assert listing.source_label == "Plaid"
        assert len(listing.groups) == 18
        assert listing.total_rows == db.scalar(
            select(func.count()).where(CategoryMapping.source == "plaid_pfc")
        )
        food = group(listing, "Food and drink")
        assert food.rows[0].key == "FOOD_AND_DRINK" and food.rows[0].is_group
        assert food.rows[0].label == "Food and drink (general)"
        coffee = row(listing, "FOOD_AND_DRINK_COFFEE")
        assert coffee.label == "Coffee" and coffee.group == "FOOD_AND_DRINK"
        assert coffee.category_id == get_category(db, "Dining & Drinks").id
        assert row(listing, "ENTERTAINMENT_TV_AND_MOVIES").label == "TV and movies"
        assert row(listing, "TRANSFER_IN_DEPOSIT").group == "TRANSFER_IN"
        assert row(listing, "OTHER_OTHER").label == "Other"

    def test_counts_go_to_the_most_specific_code(self, db):
        account = make_account(db)
        for _ in range(3):
            plaid(db, account, "FOOD_AND_DRINK_COFFEE", "FOOD_AND_DRINK")
        plaid(db, account, None, "FOOD_AND_DRINK")
        plaid(db, account, "FOOD_AND_DRINK_TEA", "FOOD_AND_DRINK")
        plaid(db, account, "FOOD_AND_DRINK_TEA", "FOOD_AND_DRINK", amount="-2.00")
        amex(db, account, "Restaurant-Restaurant")  # CSV rows never count for Plaid
        listing = categories.list_mappings(db, "plaid_pfc")
        assert row(listing, "FOOD_AND_DRINK_COFFEE").transaction_count == 3
        assert row(listing, "FOOD_AND_DRINK").transaction_count == 1
        tea = row(listing, "FOOD_AND_DRINK_TEA")
        assert tea.transaction_count == 2 and not tea.is_mapped
        assert tea.fallback_category_id == get_category(db, "Dining & Drinks").id
        assert tea.effective_category_id == tea.fallback_category_id
        assert group(listing, "Food and drink").transaction_count == 6
        assert listing.used_rows == 3 and listing.unmapped_in_use == 1

    def test_unknown_codes_get_their_own_group_and_go_to_uncategorized(self, db):
        account = make_account(db)
        plaid(db, account, "PETS_FOOD", "PETS")
        listing = categories.list_mappings(db, "plaid_pfc", only_used=True)
        pets = group(listing, "Pets")
        assert [r.key for r in pets.rows] == ["PETS", "PETS_FOOD"]
        assert pets.rows[0].transaction_count == 0  # kept: the unmapped code falls back to it
        assert all(r.effective_category_id is None for r in pets.rows)

    def test_only_used_keeps_what_transactions_use(self, db):
        account = make_account(db)
        plaid(db, account, "FOOD_AND_DRINK_COFFEE", "FOOD_AND_DRINK")
        listing = categories.list_mappings(db, "plaid_pfc", only_used=True)
        assert [r.key for r in listing.rows] == ["FOOD_AND_DRINK_COFFEE"]
        assert listing.total_rows > 100 and listing.used_rows == 1
        assert categories.list_mappings(db, "plaid_pfc", only_used=True).only_used

    def test_empty_database(self, db):
        listing = categories.list_mappings(db, "amex_csv", only_used=True)
        assert listing.groups == () and listing.used_rows == 0

    def test_unknown_source(self, db):
        with pytest.raises(MappingError):
            categories.list_mappings(db, "mint")


class TestAmexListing:
    def test_groups_subgroups_and_credit_rows(self, db):
        listing = categories.list_mappings(db, "amex_csv")
        fees = group(listing, "Fees & Adjustments")
        keys = [(r.key, r.for_credits) for r in fees.rows]
        assert keys == [
            ("Fees & Adjustments", False),
            ("Fees & Adjustments|credit", True),
            ("Fees & Adjustments-Fees & Adjustments", False),
            ("Fees & Adjustments-Fees & Adjustments|credit", True),
        ]
        credit = row(listing, "Fees & Adjustments-Fees & Adjustments|credit")
        assert credit.label == "Fees & Adjustments" and credit.code.endswith("Adjustments")
        assert credit.category_id == get_category(db, "Credits & Rewards").id
        assert row(listing, "Restaurant-Bar & Café").label == "Bar & Café"
        assert row(listing, "Travel").label == "Travel (general)"

    def test_unverified_subgroups_found_in_transactions_are_listed_unmapped(self, db):
        account = make_account(db)
        amex(db, account, "Other-Charities", amount="25.00")
        amex(db, account, "Other-Charities", amount="50.00")
        amex(db, account, "Healthcare-Doctors")
        listing = categories.list_mappings(db, "amex_csv", only_used=True)
        charities = row(listing, "Other-Charities")
        assert charities.label == "Charities" and charities.transaction_count == 2
        assert not charities.is_mapped
        assert charities.fallback_category_id == get_category(db, "Other").id
        doctors = group(listing, "Healthcare")
        assert [r.key for r in doctors.rows] == ["Healthcare", "Healthcare-Doctors"]
        assert doctors.rows[1].effective_category_id is None
        assert listing.unmapped_in_use == 2

    def test_spellings_and_case_count_on_the_stored_row(self, db):
        account = make_account(db)
        amex(db, account, "Transportation - Taxis & Coach")
        amex(db, account, "restaurant-restaurant")
        listing = categories.list_mappings(db, "amex_csv", only_used=True)
        assert row(listing, "Transportation-Taxis & Coach").transaction_count == 1
        restaurant = row(listing, "Restaurant-Restaurant")
        assert restaurant.key == "Restaurant-Restaurant" and restaurant.transaction_count == 1

    def test_credits_count_on_the_credit_row_and_fee_reversals_do_not(self, db):
        account = make_account(db)
        key = "Fees & Adjustments-Fees & Adjustments"
        amex(db, account, key, amount="95.00", name="ANNUAL MEMBERSHIP FEE")
        amex(db, account, key, amount="-30.00", name="CASH BACK REWARD")
        amex(db, account, key, amount="-95.00", name="ANNUAL FEE REVERSAL")
        listing = categories.list_mappings(db, "amex_csv", only_used=True)
        assert row(listing, key).transaction_count == 2
        assert row(listing, key + "|credit").transaction_count == 1


class TestSetMapping:
    def test_map_an_unmapped_subgroup(self, db):
        account = make_account(db)
        amex(db, account, "Other-Charities")
        gifts = get_category(db, "Gifts & Donations")
        change = categories.set_mapping(db, "amex_csv", "Other - Charities", gifts.id)
        assert (change.key, change.old_category_id, change.new_category_id) == (
            "Other-Charities",
            None,
            gifts.id,
        )
        assert change.label == "Charities" and change.transaction_count == 1
        assert row(categories.list_mappings(db, "amex_csv"), "Other-Charities").category_id == (
            gifts.id
        )
        new = amex(db, account, "Other-Charities")
        assert Categorizer.load(db).decide(new).category_id == gifts.id

    def test_change_and_remove_a_plaid_mapping(self, db):
        coffee = "FOOD_AND_DRINK_COFFEE"
        plaid(db, make_account(db), coffee, "FOOD_AND_DRINK")
        groceries = get_category(db, "Groceries")
        dining = get_category(db, "Dining & Drinks")
        change = categories.set_mapping(db, "plaid_pfc", coffee, groceries.id)
        assert (change.old_category_id, change.new_category_id) == (dining.id, groceries.id)
        assert row(categories.list_mappings(db, "plaid_pfc"), coffee).category_id == groceries.id
        categories.set_mapping(db, "plaid_pfc", coffee, None)
        listed = row(categories.list_mappings(db, "plaid_pfc"), coffee)
        assert listed.category_id is None and listed.fallback_category_id == dining.id
        assert not db.scalar(select(func.count()).where(CategoryMapping.external_key == coffee))

    def test_a_code_with_no_mapping_and_no_transactions_is_not_listed(self, db):
        categories.set_mapping(db, "plaid_pfc", "TRAVEL_FLIGHTS", None)
        keys = {r.key for r in categories.list_mappings(db, "plaid_pfc").rows}
        assert "TRAVEL_FLIGHTS" not in keys and "TRAVEL" in keys

    def test_amex_keys_match_ignoring_case_without_duplicates(self, db):
        shopping = get_category(db, "Shopping")
        categories.set_mapping(db, "amex_csv", "RESTAURANT-RESTAURANT", shopping.id)
        stored = db.scalars(
            select(CategoryMapping).where(
                func.lower(CategoryMapping.external_key) == "restaurant-restaurant"
            )
        ).all()
        assert [(m.external_key, m.category_id) for m in stored] == [
            ("Restaurant-Restaurant", shopping.id)
        ]

    def test_credit_keys_keep_their_suffix(self, db):
        fees = get_category(db, "Fees & Interest")
        change = categories.set_mapping(db, "amex_csv", "Travel-Airline|credit", fees.id)
        assert change.key == "Travel-Airline|credit"
        assert row(categories.list_mappings(db, "amex_csv"), "Travel-Airline|credit").for_credits

    @pytest.mark.parametrize(
        ("source", "key"),
        [("plaid_pfc", "food and drink"), ("plaid_pfc", ""), ("amex_csv", "  "), ("x", "A")],
    )
    def test_invalid_keys(self, db, source, key):
        with pytest.raises(MappingError):
            categories.set_mapping(db, source, key, get_category(db, "Home").id)

    def test_missing_category(self, db):
        with pytest.raises(MappingError):
            categories.set_mapping(db, "plaid_pfc", "TRAVEL", 10**9)


class TestMappingChangesAndReapply:
    """A mapping change affects new and re-applied transactions, never user-edited ones."""

    def test_csv_and_plaid_rows_follow_on_reapply_but_user_edits_never(self, db):
        account = make_account(db)
        other, gifts = get_category(db, "Other"), get_category(db, "Gifts & Donations")
        cz = Categorizer.load(db)
        csv_auto = amex(db, account, "Other-Charities")
        csv_auto.category_id, csv_auto.category_source = cz.decide(csv_auto).category_id, "amex"
        csv_user = amex(db, account, "Other-Charities", category=other, category_source="user")
        tea = plaid(db, account, "FOOD_AND_DRINK_TEA", "FOOD_AND_DRINK")
        tea.category_id, tea.category_source = cz.decide(tea).category_id, "plaid"
        db.flush()
        assert csv_auto.category_id == other.id

        categories.set_mapping(db, "amex_csv", "Other-Charities", gifts.id)
        categories.set_mapping(db, "plaid_pfc", "FOOD_AND_DRINK_TEA", gifts.id)
        db.expire_all()
        assert db.get(Transaction, csv_auto.id).category_id == other.id  # not until re-applied

        result = rules.reapply(db, rules.ReapplyScope(), dry_run=False)
        db.expire_all()
        assert db.get(Transaction, csv_auto.id).category_id == gifts.id
        assert db.get(Transaction, csv_auto.id).category_source == "amex"
        assert db.get(Transaction, tea.id).category_id == gifts.id
        assert db.get(Transaction, csv_user.id).category_id == other.id
        assert result.changed == 2 and result.skipped_user == 1

    def test_unmapping_sends_new_rows_to_the_group_then_uncategorized(self, db):
        account = make_account(db)
        categories.set_mapping(db, "amex_csv", "Travel-Lodging", None)
        cz = Categorizer.load(db)
        hotel = amex(db, account, "Travel-Lodging")
        assert cz.decide(hotel).category_id == get_category(db, "Travel").id
        categories.set_mapping(db, "amex_csv", "Travel", None)
        assert Categorizer.load(db).decide(hotel).category_id == uncategorized(db).id
