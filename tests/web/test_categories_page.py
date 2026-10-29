"""Categories page (CAT-1) and its "Plaid & Amex categories" tab (CAT-2): pages, HTMX partials,
dialogs, forms and their edge cases. Services are tested in tests/test_categories*.py."""

from __future__ import annotations

import datetime as dt
import html
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from budgetapp.models import Budget, Category, CategoryMapping, Rule
from budgetapp.services.categorize import Categorizer
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_category,
    make_rule,
    make_transaction,
    uncategorized,
)
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

TODAY = dt.date(2026, 10, 15)


def toasts(text: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', text)]


def row_html(page: str, category_id: int) -> str:
    return re.search(rf'<tr id="category-row-{category_id}".*?</tr>', page, re.S).group(0)


@pytest.fixture
def world(db, freeze_today):
    """A month of spending, a hidden category, and CSV rows with an Amex subgroup nobody mapped."""
    freeze_today(TODAY.isoformat())
    account = make_account(db)
    groceries = get_category(db, "Groceries")
    dining = get_category(db, "Dining & Drinks")
    pets = make_category(db, "Pets", color="cat-4", icon="🐾", sort_order=500, is_hidden=True)
    rows = [
        make_transaction(db, account, amount="80.00", category=groceries, date=TODAY),
        make_transaction(db, account, amount="20.00", category=groceries, date=TODAY),
        make_transaction(db, account, amount="-5.00", category=groceries, date=TODAY),  # refund
        make_transaction(db, account, amount="30.00", category=pets, date=TODAY),
        make_transaction(db, account, amount="12.00", category=dining, date=dt.date(2026, 9, 3)),
    ]
    charity = make_transaction(
        db,
        account,
        amount="25.00",
        source="csv",
        external_category="Other-Charities",
        date=dt.date(2026, 2, 1),
    )
    charity_mine = make_transaction(
        db,
        account,
        amount="40.00",
        source="csv",
        external_category="Other-Charities",
        category=dining,
        category_source="user",
        date=dt.date(2026, 2, 2),
    )
    return SimpleNamespace(
        account=account,
        groceries=groceries,
        dining=dining,
        pets=pets,
        rows=rows,
        charity=charity,
        charity_mine=charity_mine,
    )


# --------------------------------------------------------------------------- the categories tab


class TestCategoriesTab:
    def test_table_with_usage_spending_and_the_kinds_explained(self, client, world):
        response = client.get("/categories")
        assert response.status_code == 200
        assert response.template.name == "categories/index.html"
        overview = response.context["overview"]
        by_id = {c.id: c for c in overview.categories}
        assert by_id[world.groceries.id].transaction_count == 3
        assert by_id[world.groceries.id].period_amount == 95  # refunds reduce spending
        assert by_id[world.pets.id].period_amount == 30  # hidden, still counted
        assert overview.period_total == 125
        assert [c.system_key for c in overview.categories[-2:]] == [
            "uncategorized",
            "card_payments",
        ]
        text = html.unescape(response.text)
        for words in ("Spending", "Credits & income", "Card payments & transfers"):
            assert words in text
        assert "Not spending" in text  # credits and payments have no spending column
        assert "Spent · Oct 2026" in text or "Spent · October 2026" in text
        assert csp_problems(response.text, rendered=True) == []

    def test_hidden_and_built_in_rows(self, client, world):
        page = client.get("/categories").text
        pets = row_html(page, world.pets.id)
        assert 'class="is-muted"' in pets and "checked" in pets
        assert f'hx-post="/categories/{world.pets.id}/hidden"' in pets
        built_in = row_html(page, uncategorized_id(client))
        assert "Built in" in built_in and "disabled" in built_in
        assert "/merge" not in built_in  # system categories can't be merged away

    def test_htmx_tab_switch_returns_the_view_only(self, client, world):
        response = client.get("/categories", headers=HTMX)
        assert response.template.name == "categories/_view.html"
        assert "<html" not in response.text and 'aria-current="page"' in response.text


def uncategorized_id(client) -> int:
    return next(
        c.id
        for c in client.get("/categories").context["overview"].categories
        if c.system_key == "uncategorized"
    )


class TestAddAndEdit:
    def test_add_dialog_and_page(self, client, world):
        response = client.get("/categories/new", headers=HTMX)
        assert response.template.name == "categories/_form_dialog.html"
        assert "data-dialog-autoopen" in response.text and 'hx-post="/categories"' in response.text
        assert csp_problems(response.text, rendered=True) == []
        page = client.get("/categories/new")
        assert page.template.name == "categories/form.html"
        assert "hx-post" not in page.text.split('id="category-form-new"')[1].split(">")[0]
        assert csp_problems(page.text, rendered=True) == []

    def test_htmx_add_swaps_the_table_and_closes_the_dialog(self, client, db, world):
        data = {"name": "  Kids   stuff ", "kind": "expense", "color": "cat-9", "icon": "🧸"}
        response = client.post("/categories", data=data, headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "categories/_table.html"
        assert response.headers["HX-Trigger"] == "dialog:close"
        assert toasts(response.text) == ["Added “Kids stuff”."]
        created = db.scalars(select(Category).where(Category.name == "Kids stuff")).one()
        assert (created.kind, created.color, created.icon) == ("expense", "cat-9", "🧸")
        assert "Kids stuff" in response.text

    def test_duplicate_names_are_refused_ignoring_case(self, client, world):
        data = {"name": "GROCERIES", "kind": "expense", "color": "cat-1"}
        response = client.post("/categories", data=data, headers=HTMX)
        assert response.status_code == 422
        assert response.template.name == "categories/_form.html"
        assert response.headers["HX-Retarget"] == "#category-form-new"
        assert "There is already a category named “Groceries”." in html.unescape(response.text)
        assert 'value="GROCERIES"' in response.text  # what was typed stays
        plain = client.post("/categories", data=data)
        assert plain.status_code == 422 and plain.template.name == "categories/form.html"
        assert csp_problems(plain.text, rendered=True) == []

    def test_plain_add_redirects_with_a_toast(self, client, world):
        response = client.post(
            "/categories", data={"name": "Garden", "kind": "expense"}, follow_redirects=False
        )
        assert response.status_code == 303 and response.headers["location"] == "/categories"
        assert toasts(client.get("/categories").text) == ["Added “Garden”."]

    def test_edit_dialog_rename_recolour_and_kind(self, client, db, world):
        dialog = client.get(f"/categories/{world.pets.id}/edit", headers=HTMX)
        assert dialog.template.name == "categories/_form_dialog.html"
        assert 'value="Pets"' in dialog.text
        data = {"name": "Pet care", "kind": "expense", "color": "#1BAF7A", "icon": ""}
        response = client.post(f"/categories/{world.pets.id}", data=data, headers=HTMX)
        assert response.status_code == 200 and toasts(response.text) == ["Saved “Pet care”."]
        db.refresh(world.pets)
        assert (world.pets.name, world.pets.color, world.pets.icon) == ("Pet care", "#1baf7a", None)
        page = client.get(f"/categories/{world.pets.id}/edit")
        assert page.template.name == "categories/form.html"
        assert "Custom colour #1baf7a" in page.text  # the custom hex stays selectable

    def test_built_in_categories_keep_their_kind(self, client, db, world):
        uid = uncategorized_id(client)
        dialog = client.get(f"/categories/{uid}/edit", headers=HTMX)
        assert re.search(r'<select[^>]*name="kind"[^>]*disabled', dialog.text)
        response = client.post(
            f"/categories/{uid}", data={"name": "To sort", "kind": "income"}, headers=HTMX
        )
        assert response.status_code == 422
        assert "is built in and keeps its kind" in html.unescape(response.text)
        response = client.post(f"/categories/{uid}", data={"name": "To sort"}, headers=HTMX)
        assert response.status_code == 200
        assert uncategorized(db).name == "To sort" and uncategorized(db).kind == "expense"

    def test_bad_input_comes_back_with_messages(self, client, world):
        data = {"name": "", "kind": "savings", "color": "red", "icon": "x" * 40}
        response = client.post(f"/categories/{world.pets.id}", data=data, headers=HTMX)
        assert response.status_code == 422
        assert set(response.context["f"]["errors"]) == {"name", "kind", "color", "icon"}
        assert response.headers["HX-Retarget"] == f"#category-form-{world.pets.id}"


class TestHide:
    def test_htmx_hide_and_show(self, client, db, world):
        groceries = world.groceries.id
        response = client.post(
            f"/categories/{groceries}/hidden", data={"hidden": "1"}, headers=HTMX
        )
        assert response.status_code == 200
        assert response.template.name == "categories/_row.html"
        assert response.text.lstrip().startswith(f'<tr id="category-row-{groceries}"')
        assert toasts(response.text) == [
            "“Groceries” is hidden from pickers and budget lists. Its spending still counts."
        ]
        db.refresh(world.groceries)
        assert world.groceries.is_hidden
        # Gone from pickers (the rule form), still counted in this period's total.
        form = client.get("/rules/new")
        assert not re.search(rf'<option value="{groceries}"', form.text)
        assert client.get("/categories").context["overview"].period_total == 125
        response = client.post(f"/categories/{groceries}/hidden", headers=HTMX)
        assert toasts(response.text) == ["“Groceries” is shown again."]
        assert csp_problems(response.text, rendered=True) == []

    def test_built_in_categories_cannot_be_hidden(self, client, world):
        uid = uncategorized_id(client)
        response = client.post(f"/categories/{uid}/hidden", data={"hidden": "1"}, headers=HTMX)
        assert response.status_code == 409
        plain = client.post(f"/categories/{uid}/hidden", data={"hidden": "1"})
        assert plain.status_code == 200
        assert toasts(plain.text) == ["“Uncategorized” is built in and can't be hidden."]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/categories/{id}/edit"),
        ("post", "/categories/{id}"),
        ("post", "/categories/{id}/hidden"),
        ("get", "/categories/{id}/merge"),
        ("get", "/categories/{id}/merge/preview"),
        ("post", "/categories/{id}/merge"),
    ],
)
def test_a_category_that_is_gone_is_a_404(client, method, path):
    response = getattr(client, method)(path.format(id=10**9), headers=HTMX)
    assert response.status_code == 404


# --------------------------------------------------------------------------- merge


class TestMerge:
    def test_dialog_preview_then_merge(self, client, db, world):
        rule = make_rule(db, world.pets, merchant_pattern="PETCO")
        make_budget(db, world.pets, amount="60")
        pets_id, groceries_id = world.pets.id, world.groceries.id
        dialog = client.get(f"/categories/{world.pets.id}/merge", headers=HTMX)
        assert dialog.template.name == "categories/_merge_dialog.html"
        assert "Choose a category to see what moves." in dialog.text
        assert csp_problems(dialog.text, rendered=True) == []
        preview = client.get(
            f"/categories/{world.pets.id}/merge/preview",
            params={"target_id": str(world.groceries.id)},
            headers=HTMX,
        )
        assert preview.template.name == "categories/_merge_preview.html"
        counts = preview.context["m"]["preview"].counts
        assert (counts.transactions, counts.rules, counts.budgets_moved) == (1, 1, 1)
        text = html.unescape(preview.text)
        assert "1 transaction" in text and "1 rule" in text and "1 budget entry" in text

        response = client.post(
            f"/categories/{pets_id}/merge", data={"target_id": str(groceries_id)}, headers=HTMX
        )
        assert response.headers["HX-Redirect"] == "/categories"
        assert toasts(client.get("/categories").text) == [
            "Merged “Pets” into “Groceries”: 1 transaction moved."
        ]
        db.expire_all()
        assert db.get(Category, pets_id) is None
        assert db.get(Rule, rule.id).category_id == groceries_id
        assert db.scalars(select(Budget.category_id)).all() == [groceries_id]

    def test_merging_into_a_category_with_its_own_budget_keeps_that_budget(self, client, db, world):
        make_budget(db, world.pets, amount="60")
        kept = make_budget(db, world.groceries, amount="500")
        preview = client.get(
            f"/categories/{world.pets.id}/merge",
            params={"target_id": str(world.groceries.id)},
        )
        assert preview.template.name == "categories/merge.html"  # the page without JS
        assert "keeps its own budget" in html.unescape(preview.text)
        client.post(f"/categories/{world.pets.id}/merge", data={"target_id": world.groceries.id})
        db.expire_all()
        assert db.scalars(select(Budget.id)).all() == [kept.id]

    def test_a_kind_change_is_called_out(self, client, db, world):
        credits = get_category(db, "Credits & Rewards")
        preview = client.get(
            f"/categories/{world.pets.id}/merge/preview",
            params={"target_id": str(credits.id)},
            headers=HTMX,
        )
        text = html.unescape(preview.text)
        assert "“Credits & Rewards” is a different kind." in text
        assert "Its transactions will count as Credits & income, which isn't spending." in text

    def test_errors_come_back_in_the_form(self, client, world):
        for target, message in (
            ("", "Choose the category to merge into."),
            (str(world.pets.id), "Choose a different category: it can't merge into itself."),
            ("999999999", "That category no longer exists. Choose another one."),
        ):
            response = client.post(
                f"/categories/{world.pets.id}/merge", data={"target_id": target}, headers=HTMX
            )
            assert response.status_code == 422
            assert response.template.name == "categories/_merge_form.html"
            assert message in html.unescape(response.text)
        plain = client.post(f"/categories/{world.pets.id}/merge", data={"target_id": ""})
        assert plain.status_code == 422 and plain.template.name == "categories/merge.html"

    def test_built_in_categories_cannot_be_merged_away(self, client, world):
        uid = uncategorized_id(client)
        assert client.get(f"/categories/{uid}/merge", headers=HTMX).status_code == 409
        response = client.post(
            f"/categories/{uid}/merge", data={"target_id": world.groceries.id}, headers=HTMX
        )
        assert response.status_code == 422
        assert "is built in and can't be merged" in html.unescape(response.text)

    def test_plain_preview_link_goes_to_the_merge_page(self, client, world):
        response = client.get(
            f"/categories/{world.pets.id}/merge/preview",
            params={"target_id": str(world.groceries.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            f"/categories/{world.pets.id}/merge?target_id={world.groceries.id}"
        )


# ------------------------------------------------------------------ Plaid & Amex categories


class TestMappingsTab:
    def test_plaid_tab_lists_codes_in_use(self, client, db, world):
        make_transaction(
            db,
            world.account,
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
        )
        response = client.get("/categories/mappings")
        assert response.status_code == 200
        assert response.template.name == "categories/index.html"
        assert response.context["tab"] == "mappings"
        listing = response.context["listing"]
        assert listing.source == "plaid_pfc" and listing.only_used
        assert [r.key for r in listing.rows] == ["FOOD_AND_DRINK_COFFEE"]
        assert "Coffee" in response.text and "FOOD_AND_DRINK_COFFEE" in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_unmapped_amex_subgroups_are_visible_and_mappable(self, client, world):
        response = client.get("/categories/mappings?source=amex_csv&show=used")
        listing = response.context["listing"]
        assert listing.unmapped_in_use == 1
        text = html.unescape(response.text)
        assert "1 Amex CSV category in your transactions has no mapping of its own." in text
        charities = re.search(
            r"<tr>\s*<td class=\"col-grow\">\s*<span class=\"table__primary\">"
            r"Charities.*?</tr>",
            response.text,
            re.S,
        ).group(0)
        assert '<option value="">Not mapped</option>' in charities
        assert "Uses Other (from its group)" in html.unescape(charities)
        assert csp_problems(response.text, rendered=True) == []

    def test_htmx_filters_swap_the_results_only(self, client, world):
        response = client.get(
            "/categories/mappings?source=amex_csv&show=all",
            headers={**HTMX, "HX-Target": "mappings-results"},
        )
        assert response.template.name == "categories/_mappings_results.html"
        assert response.context["listing"].total_rows >= 31  # every seeded Amex mapping
        tab = client.get("/categories/mappings", headers=HTMX)
        assert tab.template.name == "categories/_view.html"

    def test_unknown_filter_values_fall_back(self, client, world):
        response = client.get("/categories/mappings?source=visa&show=some")
        assert (response.context["source"], response.context["show"]) == ("plaid_pfc", "used")

    def test_mapping_a_subgroup_then_reapplying_moves_csv_rows_but_never_yours(
        self, client, db, world
    ):
        gifts = get_category(db, "Gifts & Donations")
        world.charity.category_id = Categorizer.load(db).decide(world.charity).category_id
        world.charity.category_source = "amex"
        db.flush()
        response = client.post(
            "/categories/mappings",
            data={
                "source": "amex_csv",
                "key": "Other-Charities",
                "show": "used",
                "category_id": str(gifts.id),
            },
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "categories/_mapping_group.html"
        assert response.text.lstrip().startswith('<tbody class="mapping-group">')
        assert toasts(response.text) == [
            "Charities now goes to Gifts & Donations. 2 past transactions keep its category"
            " until you re-apply rules."
        ]
        mapping = db.scalars(
            select(CategoryMapping).where(CategoryMapping.external_key == "Other-Charities")
        ).one()
        assert mapping.category_id == gifts.id
        db.refresh(world.charity)
        assert world.charity.category_id == get_category(db, "Other").id  # not until re-applied

        client.post("/rules/reapply", data={"scope": "all", "action": "apply"})
        db.refresh(world.charity)
        db.refresh(world.charity_mine)
        assert (world.charity.category_id, world.charity.category_source) == (gifts.id, "amex")
        assert world.charity_mine.category_id == world.dining.id  # the user's own choice

    def test_unmapping_falls_back_to_the_group(self, client, db, world):
        make_transaction(
            db, world.account, pfc_primary="FOOD_AND_DRINK", pfc_detailed="FOOD_AND_DRINK_COFFEE"
        )
        response = client.post(
            "/categories/mappings",
            data={"source": "plaid_pfc", "key": "FOOD_AND_DRINK_COFFEE", "category_id": ""},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert toasts(response.text)[0].startswith(
            "Coffee is no longer mapped: it uses its group's category."
        )
        assert "Uses Dining &amp; Drinks (from its group)" in response.text

    def test_a_row_that_left_the_filtered_list_reloads_the_list(self, client, db, world):
        response = client.post(
            "/categories/mappings",
            data={"source": "plaid_pfc", "key": "TRAVEL", "show": "used", "category_id": ""},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "categories/_mappings_results.html"
        assert response.headers["HX-Retarget"] == "#mappings-results"
        assert response.headers["HX-Reswap"] == "innerHTML"

    def test_bad_requests(self, client, world):
        bad_key = {"source": "plaid_pfc", "key": "not a code", "category_id": "1"}
        assert client.post("/categories/mappings", data=bad_key, headers=HTMX).status_code == 409
        plain = client.post("/categories/mappings", data=bad_key)
        assert toasts(plain.text) == ["That isn't a Plaid category code."]
        bad_category = {"source": "plaid_pfc", "key": "TRAVEL", "category_id": "x"}
        response = client.post("/categories/mappings", data=bad_category, headers=HTMX)
        assert response.status_code == 409
        gone = {"source": "plaid_pfc", "key": "TRAVEL", "category_id": "999999999"}
        assert client.post("/categories/mappings", data=gone, headers=HTMX).status_code == 409

    def test_plain_post_redirects_back_to_the_same_filters(self, client, world):
        data = {
            "source": "amex_csv",
            "key": "Other-Charities",
            "show": "all",
            "category_id": str(world.dining.id),
        }
        response = client.post("/categories/mappings", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/categories/mappings?source=amex_csv&show=all"
