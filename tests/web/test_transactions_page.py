"""The transactions page (TXN-1..5): full pages vs HTMX partials, row edits, dialogs, export."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from markupsafe import Markup
from sqlalchemy import select

from budgetapp.models import Rule, Transaction
from budgetapp.services import alerts
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_rule,
    make_split,
    make_transaction,
)
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

TODAY = "2026-10-29"


@pytest.fixture(autouse=True)
def _today(freeze_today):
    freeze_today(TODAY)


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
        "payments": card_payments(db),
    }


@pytest.fixture
def budget_calls(monkeypatch):
    calls: list[int] = []

    def record(session, notifier, *, today=None):
        calls.append(1)
        return []

    monkeypatch.setattr(alerts, "evaluate_budgets", record)
    return calls


def clean(response) -> None:
    assert csp_problems(response.text, rendered=True) == []


def toasts(html: str) -> list[str]:
    return [
        Markup(t).unescape() for t in re.findall(r'<div class="toast__text">([^<]*)</div>', html)
    ]


def row_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'<tr id="txn-(\d+)"', html)]


def reload(db, txn: Transaction) -> Transaction:
    db.expire_all()
    return db.get(Transaction, txn.id)


# --------------------------------------------------------------------------- the list


class TestListPage:
    def test_empty_database(self, client):
        response = client.get("/transactions")
        assert response.status_code == 200
        assert response.template.name == "transactions/index.html"
        html = response.text
        assert "No transactions yet" in html
        assert 'href="/connection"' in html and 'href="/import"' in html
        assert html.count('<h1 class="page-header__title">') == 1
        assert '<script src="/static/js/transactions.js" defer></script>' in html
        assert 'href="/static/css/pages/transactions.css"' in html
        clean(response)

    def test_rows_summary_and_export_link(self, client, db, card, cats):
        make_transaction(
            db,
            card,
            name="WHOLEFDS MKT",
            merchant_name="Whole Foods",
            amount="86.42",
            date="2026-10-27",
            category=cats["groceries"],
        )
        make_transaction(
            db,
            card,
            name="REI REFUND",
            amount="-54.99",
            date="2026-10-25",
            category=cats["shopping"],
        )
        make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-500.00",
            date="2026-10-22",
            category=cats["payments"],
            category_source="system",
        )
        response = client.get("/transactions?q=&range=all&status=all")
        assert response.status_code == 200
        html = response.text
        assert len(row_ids(html)) == 3
        assert "<strong>3</strong> transactions" in html
        assert "$86.42" in html and "−$554.99" in html  # charges, then credits & payments
        assert 'href="/transactions/export.csv"' in html
        assert "Whole Foods" in html and "WHOLEFDS MKT" in html
        clean(response)

    def test_payment_refund_and_pending_rows_show_what_they_are(self, client, db, card, cats):
        payment = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-500.00",
            category=cats["payments"],
            category_source="system",
        )
        refund = make_transaction(db, card, name="REI", amount="-54.99", category=cats["shopping"])
        pending = make_transaction(db, card, name="UBER", amount="23.18", pending=True)
        html = client.get("/transactions").text
        rows = {int(i): body for i, body in re.findall(r'<tr id="txn-(\d+)"(.*?)</tr>', html, re.S)}
        assert 'class="txn-row is-muted' in f'<tr id="txn-{payment.id}"{rows[payment.id]}'
        assert ">Payment<" in rows[payment.id] and "amount--muted" in rows[payment.id]
        assert "/rule" not in rows[payment.id]  # rules never change a payment
        assert ">Refund<" in rows[refund.id] and "amount--credit" in rows[refund.id]
        assert ">Pending<" in rows[pending.id] and "amount--pending" in rows[pending.id]
        assert "/split" not in rows[pending.id] and "/split" in rows[refund.id]

    def test_htmx_gets_only_the_results_with_a_clean_url(self, client, db, card):
        make_transaction(db, card, name="BLUE BOTTLE")
        make_transaction(db, card, name="PHILZ")
        headers = HTMX | {"HX-Trigger": "txn-filters"}
        response = client.get(
            "/transactions?q=blue&range=all&status=all&merchant=&min=", headers=headers
        )
        assert response.template.name == "transactions/_results.html"
        assert "<html" not in response.text and "txn-filters" not in response.text
        assert response.headers["HX-Replace-Url"] == "/transactions?q=blue"
        assert "HX-Request" in response.headers["vary"]
        assert [r.name for r in response.context["page"].rows] == ["BLUE BOTTLE"]
        clean(response)

    def test_sort_and_page_links_carry_the_filters(self, client, db, card):
        for i in range(55):
            make_transaction(db, card, name=f"T{i}", tags=["work"], date=dt.date(2026, 10, 1))
        response = client.get("/transactions?tag=work&sort=amount", headers=HTMX)
        assert "HX-Replace-Url" not in response.headers  # pagination and sort links push instead
        html = response.text
        assert 'hx-get="/transactions?tag=work&amp;sort=amount&amp;page=2"' in html
        assert 'href="/transactions?tag=work&amp;sort=-amount"' in html  # flip the active sort
        assert 'href="/transactions/export.csv?tag=work&amp;sort=amount"' in html
        assert 'data-sort="amount"' in html
        last = client.get("/transactions?tag=work&page=99")
        assert last.context["page"].page == 2 and len(row_ids(last.text)) == 5

    def test_filter_bar_reflects_the_url(self, client, db, card, cats):
        make_transaction(db, card, account_owner="SAM RIVERA", tags=["work"], merchant_name="Uber")
        url = (
            f"/transactions?q=uber&range=custom&from=2026-10-01&category={cats['groceries'].id}"
            "&category=uncategorized&min=5&status=pending&tag=work&member=SAM+RIVERA&credits=1"
        )
        html = client.get(url).text
        assert 'id="f-q"' in html and 'value="uber"' in html
        assert re.search(r'<option value="custom" selected>', html)
        assert 'value="2026-10-01"' in html
        assert re.search(rf'id="f-cat-{cats["groceries"].id}"[^>]*checked', html)
        assert re.search(r'value="uncategorized"[^>]*checked', html)
        assert "Groceries +1" in html
        assert re.search(r'<details class="txn-more" open>', html)
        assert re.search(r'<option value="SAM RIVERA" selected>', html)
        assert re.search(r'id="f-credits"[^>]*checked', html)
        assert "Card member" in html  # the column shows once a row names a member

    def test_bad_parameters_are_explained_not_fatal(self, client):
        response = client.get("/transactions?from=2026-02-30&min=lots&page=abc&sort=x")
        assert response.status_code == 200
        assert (
            "Ignored the start date" in response.text
            and "Ignored the minimum amount" in response.text
        )

    def test_no_results_for_the_filters(self, client, db, card):
        make_transaction(db, card, name="SOMETHING")
        html = client.get("/transactions?q=nothing-like-this").text
        assert (
            "No matches" in html and "Clear filters" in html and "No transactions yet" not in html
        )

    def test_duplicates_and_review_views(self, client, db, card):
        make_transaction(db, card, name="REAL")
        make_transaction(db, card, name="DUPE", source="csv", duplicate_status="suspect")
        make_transaction(
            db, card, name="FLAGGED", needs_review=True, review_reason="Amounts changed."
        )
        html = client.get("/transactions").text
        assert "DUPE" not in html and "Possible duplicates" in html and "Needs review" in html
        assert "Amounts changed." in html and "Mark as reviewed" in html
        dupes = client.get("/transactions?show=duplicates")
        assert "DUPE" in dupes.text and "REAL" not in dupes.text
        assert 'href="/import"' in dupes.text and "not counted in any total" in dupes.text
        assert '<input type="hidden" name="show" value="duplicates">' in dupes.text
        review = client.get("/transactions?show=review")
        assert [r.name for r in review.context["page"].rows] == ["FLAGGED"]
        clean(dupes)

    def test_split_rows_list_their_parts(self, client, db, card, cats):
        txn = make_transaction(db, card, amount="213.77", category=cats["shopping"])
        make_split(db, txn, cats["groceries"], "150.00")
        make_split(db, txn, cats["home"], "63.77")
        html = client.get("/transactions").text
        assert "Split into 2 parts" in html and "$63.77" in html
        assert f'id="cat-{txn.id}"' not in html  # no inline select for a split row
        assert "Edit split" in html


# --------------------------------------------------------------------------- inline category


class TestCategoryEdit:
    def test_htmx_change_returns_the_row_and_a_toast(self, client, db, card, cats, budget_calls):
        txn = make_transaction(db, card, name="UBER", merchant_name="Uber")
        response = client.post(
            f"/transactions/{txn.id}/category",
            data={"category_id": cats["dining"].id},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "transactions/_row.html"
        assert row_ids(response.text) == [txn.id]
        assert toasts(response.text) == ["Saved: Uber is in Dining & Drinks."]
        assert 'hx-swap-oob="beforeend:#flash"' in response.text
        stored = reload(db, txn)
        assert (stored.category_id, stored.category_source) == (cats["dining"].id, "user")
        assert budget_calls == [1]
        clean(response)

    def test_a_bad_category_puts_the_row_back(self, client, db, card, cats):
        txn = make_transaction(db, card, category=cats["home"])
        response = client.post(
            f"/transactions/{txn.id}/category", data={"category_id": "999999"}, headers=HTMX
        )
        assert response.status_code == 200 and row_ids(response.text) == [txn.id]
        assert "no longer exists" in toasts(response.text)[0]
        assert 'class="toast toast--over"' in response.text
        assert reload(db, txn).category_id == cats["home"].id

    def test_the_phone_dialog(self, client, db, card, cats):
        txn = make_transaction(db, card, category=cats["home"])
        dialog = client.get(f"/transactions/{txn.id}/category", headers=HTMX)
        assert dialog.template.name == "transactions/_dialog.html"
        assert "data-dialog-autoopen" in dialog.text and 'name="dialog" value="1"' in dialog.text
        response = client.post(
            f"/transactions/{txn.id}/category",
            data={"category_id": cats["dining"].id, "dialog": "1"},
            headers=HTMX,
        )
        assert response.headers["HX-Trigger"] == "dialog:close"
        bad = client.post(
            f"/transactions/{txn.id}/category",
            data={"category_id": "", "dialog": "1"},
            headers=HTMX,
        )
        assert bad.status_code == 422 and bad.headers["HX-Retarget"] == "#txn-category-form"
        assert "Choose a category." in bad.text

    def test_a_plain_post_redirects_back(self, client, db, card, cats):
        txn = make_transaction(db, card)
        response = client.post(
            f"/transactions/{txn.id}/category",
            data={"category_id": cats["dining"].id, "next": "/transactions?q=x"},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/transactions?q=x"

    @pytest.mark.parametrize(
        "next_url", ["//evil.example/x", "https://evil.example/", "/rules", "/\\evil"]
    )
    def test_next_must_be_a_transactions_page(self, client, db, card, cats, next_url):
        txn = make_transaction(db, card)
        response = client.post(
            f"/transactions/{txn.id}/category",
            data={"category_id": cats["dining"].id, "next": next_url},
            follow_redirects=False,
        )
        assert response.headers["location"] == f"/transactions/{txn.id}"

    def test_reset_and_reviewed(self, client, db, card, cats, budget_calls):
        make_rule(db, cats["home"], merchant_pattern="IKEA")
        txn = make_transaction(
            db,
            card,
            name="IKEA",
            category=cats["dining"],
            category_source="user",
            needs_review=True,
            review_reason="Check",
        )
        response = client.post(f"/transactions/{txn.id}/reset", headers=HTMX)
        assert row_ids(response.text) == [txn.id] and "Reset:" in toasts(response.text)[0]
        assert reload(db, txn).category_id == cats["home"].id and budget_calls == [1]
        flagged = make_transaction(db, card, needs_review=True, review_reason="Check")
        response = client.post(f"/transactions/{flagged.id}/reviewed", headers=HTMX)
        assert toasts(response.text) == ["Marked as reviewed."]
        assert not reload(db, flagged).needs_review

    @pytest.mark.parametrize(
        "path", ["category", "reset", "reviewed", "edit", "split", "split/clear", "rule"]
    )
    def test_missing_transactions_are_404(self, client, path):
        assert client.post(f"/transactions/987654321/{path}", data={}).status_code == 404

    @pytest.mark.parametrize("path", ["", "/category", "/edit", "/split", "/rule", "/rule/preview"])
    def test_missing_transactions_are_404_on_get(self, client, path):
        assert client.get(f"/transactions/987654321{path}").status_code == 404


# --------------------------------------------------------------------------- notes and tags


class TestNotesAndTags:
    def test_dialog_and_full_page(self, client, db, card):
        txn = make_transaction(db, card, notes="old", tags=["work"])
        make_transaction(db, card, tags=["travel"])
        dialog = client.get(
            f"/transactions/{txn.id}/edit",
            headers=HTMX | {"HX-Current-URL": "http://127.0.0.1:8766/transactions?q=x"},
        )
        assert dialog.template.name == "transactions/_dialog.html"
        html = dialog.text
        assert "data-dialog-autoopen" in html and f'hx-post="/transactions/{txn.id}/edit"' in html
        assert 'name="next" value="/transactions?q=x"' in html
        assert '<option value="travel">' in html and ">old</textarea>" in html
        clean(dialog)
        page = client.get(f"/transactions/{txn.id}/edit")
        assert page.template.name == "transactions/form_page.html"
        assert "hx-post" not in page.text.split('id="txn-edit-form"')[1].split(">")[0]
        assert page.text.count('<h1 class="page-header__title">') == 1
        clean(page)

    def test_save(self, client, db, card):
        txn = make_transaction(db, card)
        response = client.post(
            f"/transactions/{txn.id}/edit",
            data={"notes": "  Dinner with Sam  ", "tags": ["", " work ", "Work", "nyc"]},
            headers=HTMX,
        )
        assert response.headers["HX-Trigger"] == "dialog:close"
        assert row_ids(response.text) == [txn.id] and toasts(response.text) == [
            "Notes and tags saved."
        ]
        stored = reload(db, txn)
        assert stored.notes == "Dinner with Sam" and stored.tags == ["work", "nyc"]
        assert stored.user_edited_at is not None

    def test_errors_come_back_in_the_form(self, client, db, card):
        txn = make_transaction(db, card, notes="keep")
        response = client.post(
            f"/transactions/{txn.id}/edit", data={"notes": "new", "tags": ["a,b"]}, headers=HTMX
        )
        assert response.status_code == 422
        assert response.headers["HX-Retarget"] == "#txn-edit-form"
        assert response.headers["HX-Reswap"] == "outerHTML"
        assert "can&#39;t contain commas" in response.text and ">new</textarea>" in response.text
        assert reload(db, txn).notes == "keep"
        plain = client.post(f"/transactions/{txn.id}/edit", data={"notes": "x" * 2001})
        assert plain.status_code == 422 and plain.template.name == "transactions/form_page.html"

    def test_plain_post_redirects_with_a_toast(self, client, db, card):
        txn = make_transaction(db, card)
        response = client.post(f"/transactions/{txn.id}/edit", data={"notes": "hi"})
        assert response.url.path == f"/transactions/{txn.id}"
        assert "Notes and tags saved." in toasts(response.text)


# --------------------------------------------------------------------------- splits


class TestSplitDialog:
    def test_a_new_split_starts_with_the_whole_amount(self, client, db, card, cats):
        txn = make_transaction(db, card, amount="213.77", category=cats["shopping"])
        html = client.get(f"/transactions/{txn.id}/split", headers=HTMX).text
        assert 'data-total="213.77"' in html and "data-auto-first" in html
        assert 'value="213.77"' in html and "<template data-split-template>" in html
        assert re.search(rf'<option value="{cats["shopping"].id}"[^>]*selected', html)

    def test_save_and_clear(self, client, db, card, cats, budget_calls):
        txn = make_transaction(db, card, amount="213.77", category=cats["shopping"])
        response = client.post(
            f"/transactions/{txn.id}/split",
            data={
                "part_category": [str(cats["groceries"].id), str(cats["home"].id), ""],
                "part_amount": ["$150", "63.77", ""],
                "part_note": ["food", "", ""],
            },
            headers=HTMX,
        )
        assert response.status_code == 200 and response.headers["HX-Trigger"] == "dialog:close"
        assert toasts(response.text) == ["Split into 2 parts."] and ">Split<" in response.text
        assert [(s.category_id, s.amount, s.note) for s in reload(db, txn).splits] == [
            (cats["groceries"].id, Decimal("150.00"), "food"),
            (cats["home"].id, Decimal("63.77"), None),
        ]
        edit = client.get(f"/transactions/{txn.id}/split", headers=HTMX).text
        assert "data-auto-first" not in edit and "Remove split" in edit
        cleared = client.post(f"/transactions/{txn.id}/split/clear", headers=HTMX)
        assert "Split removed" in toasts(cleared.text)[0] and reload(db, txn).splits == []
        assert budget_calls == [1, 1]

    def test_a_credit_is_split_into_credits(self, client, db, card, cats):
        txn = make_transaction(db, card, amount="-54.99", category=cats["shopping"])
        response = client.post(
            f"/transactions/{txn.id}/split",
            data={
                "part_category": [str(cats["shopping"].id), str(cats["home"].id)],
                "part_amount": ["30", "24.99"],
                "part_note": ["", ""],
            },
            headers=HTMX,
        )
        assert response.status_code == 200
        assert sorted(s.amount for s in reload(db, txn).splits) == [
            Decimal("-30.00"),
            Decimal("-24.99"),
        ]

    @pytest.mark.parametrize(
        ("amounts", "error"),
        [
            (["150.00", "63.76"], "$0.01 left to assign"),
            (["150.00", "-63.77"], "without a minus sign"),
            (["150.00", "abc"], "is not an amount"),
            (["150.00", ""], "Enter an amount."),
        ],
    )
    def test_errors_come_back_in_the_form(self, client, db, card, cats, amounts, error):
        txn = make_transaction(db, card, amount="213.77")
        response = client.post(
            f"/transactions/{txn.id}/split",
            data={
                "part_category": [str(cats["groceries"].id), str(cats["home"].id)],
                "part_amount": amounts,
                "part_note": ["", ""],
            },
            headers=HTMX,
        )
        assert response.status_code == 422 and response.headers["HX-Retarget"] == "#txn-split-form"
        assert error in Markup(response.text).unescape()
        assert "data-auto-first" not in response.text  # keep what the user typed
        assert f'value="{amounts[0]}"' in response.text
        assert reload(db, txn).splits == []
        clean(response)

    def test_a_pending_charge_cannot_be_split(self, client, db, card, cats):
        txn = make_transaction(db, card, amount="20.00", pending=True)
        html = client.get(f"/transactions/{txn.id}/split", headers=HTMX).text
        assert "Can&#39;t split this one." in html and "Save split" not in html
        response = client.post(
            f"/transactions/{txn.id}/split",
            data={
                "part_category": [str(cats["groceries"].id), str(cats["home"].id)],
                "part_amount": ["10", "10"],
                "part_note": ["", ""],
            },
            headers=HTMX,
        )
        assert response.status_code == 422 and "pending" in response.text


# --------------------------------------------------------------------------- one-click rule


class TestOneClickRule:
    def test_dialog_prefills_and_previews(self, client, db, card, cats):
        txn = make_transaction(
            db,
            card,
            name="UBER *TRIP",
            merchant_name="Uber",
            category=cats["dining"],
            category_source="plaid",
        )
        make_transaction(
            db,
            card,
            name="UBER *EATS",
            merchant_name="Uber",
            category=cats["shopping"],
            category_source="plaid",
        )
        response = client.get(f"/transactions/{txn.id}/rule", headers=HTMX)
        html = response.text
        assert 'id="rule-pattern"' in html and 'value="Uber"' in html
        assert re.search(rf'<option value="{cats["dining"].id}"[^>]*selected', html)
        assert "Matches <strong>2 transactions</strong>" in html
        assert "<strong>1</strong> moves to Dining &amp; Drinks" in html
        assert f'hx-get="/transactions/{txn.id}/rule/preview"' in html
        clean(response)
        preview = client.get(
            f"/transactions/{txn.id}/rule/preview",
            params={"merchant_pattern": "uber", "category_id": cats["home"].id},
            headers=HTMX,
        )
        assert preview.template.name == "transactions/_rule_preview.html"
        assert "<strong>2</strong> move to Home" in preview.text
        empty = client.get(
            f"/transactions/{txn.id}/rule/preview", params={"merchant_pattern": ""}, headers=HTMX
        )
        assert "Type part of a merchant name" in empty.text

    def test_create_reloads_the_list_with_a_toast(self, client, db, card, cats, budget_calls):
        txn = make_transaction(
            db,
            card,
            name="UBER *TRIP",
            merchant_name="Uber",
            category=cats["dining"],
            category_source="plaid",
        )
        other = make_transaction(
            db,
            card,
            name="UBER *EATS",
            merchant_name="Uber",
            category=cats["shopping"],
            category_source="plaid",
        )
        mine = make_transaction(
            db,
            card,
            name="UBER",
            merchant_name="Uber",
            category=cats["shopping"],
            category_source="user",
        )
        response = client.post(
            f"/transactions/{txn.id}/rule",
            data={
                "merchant_pattern": "Uber",
                "category_id": cats["home"].id,
                "apply_past": "1",
                "next": "/transactions?q=uber",
            },
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/transactions?q=uber"
        rule = db.scalar(select(Rule).where(Rule.source_transaction_id == txn.id))
        assert (rule.merchant_pattern, rule.category_id) == ("Uber", cats["home"].id)
        assert reload(db, other).category_id == cats["home"].id
        assert reload(db, mine).category_id == cats["shopping"].id
        page = client.get("/transactions?q=uber")
        assert toasts(page.text) == [
            "Rule saved: merchants containing “Uber” go to Home. 2 past transactions moved; "
            "1 you categorized by hand stayed as they were."
        ]
        assert budget_calls == [1]

    def test_without_applying(self, client, db, card, cats):
        txn = make_transaction(
            db, card, merchant_name="Uber", category=cats["dining"], category_source="plaid"
        )
        response = client.post(
            f"/transactions/{txn.id}/rule",
            data={"merchant_pattern": "Uber", "category_id": cats["home"].id},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == "/transactions"
        assert reload(db, txn).category_id == cats["dining"].id

    def test_errors_come_back_in_the_form(self, client, db, card, cats):
        txn = make_transaction(db, card, merchant_name="Uber")
        response = client.post(
            f"/transactions/{txn.id}/rule",
            data={"merchant_pattern": " ", "category_id": cats["home"].id, "apply_past": "1"},
            headers=HTMX,
        )
        assert response.status_code == 422 and response.headers["HX-Retarget"] == "#txn-rule-form"
        assert "Add at least one condition" in response.text
        assert db.scalar(select(Rule).where(Rule.source_transaction_id == txn.id)) is None

    def test_the_menu_hides_it_for_payments(self, client, db, card, cats):
        payment = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT",
            amount="-10.00",
            category=cats["payments"],
            category_source="system",
        )
        html = client.get(f"/transactions/{payment.id}", headers=HTMX).text
        assert "Always categorize like this" not in html


# --------------------------------------------------------------------------- details


class TestDetails:
    def test_dialog_and_page(self, client, db, card, cats):
        rule = make_rule(db, cats["dining"], merchant_pattern="BLUE", name="Coffee")
        txn = make_transaction(
            db,
            card,
            name="BLUE BOTTLE COFFEE",
            merchant_name="Blue Bottle",
            category=cats["dining"],
            category_source="rule",
            rule_id=rule.id,
            notes="Latte\nwith Sam",
            tags=["coffee"],
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
            location={
                "address": "1 Ferry Building",
                "city": "San Francisco",
                "region": "CA",
                "postal_code": "94111",
                "country": "US",
                "lat": Decimal("37.7955"),
                "lon": Decimal("-122.3937"),
            },
        )
        dialog = client.get(f"/transactions/{txn.id}", headers=HTMX)
        assert dialog.template.name == "transactions/_detail_dialog.html"
        html = dialog.text
        assert "data-dialog-autoopen" in html and "Transaction details" in html
        assert 'Rule <a href="/rules">“Coffee”</a>' in html
        assert (
            "FOOD_AND_DRINK_COFFEE" in html and "1 Ferry Building, San Francisco, CA 94111" in html
        )
        assert "Latte\nwith Sam" in html and txn.plaid_transaction_id in html
        assert 'href="/transactions?merchant=Blue+Bottle"' in html
        assert "<h3" in html and "<h1" not in html
        clean(dialog)
        page = client.get(f"/transactions/{txn.id}")
        assert page.template.name == "transactions/detail.html"
        assert page.text.count('<h1 class="page-header__title">') == 1
        assert f'href="/transactions/{txn.id}/edit?next=%2Ftransactions%2F{txn.id}"' in page.text
        clean(page)

    def test_full_page_actions_post_plain_forms(self, client, db, card, cats):
        txn = make_transaction(
            db, card, category=cats["dining"], category_source="user", needs_review=True
        )
        html = client.get(f"/transactions/{txn.id}").text
        assert f'action="/transactions/{txn.id}/reset"' in html
        assert f'action="/transactions/{txn.id}/reviewed"' in html
        response = client.post(
            f"/transactions/{txn.id}/reviewed", data={"next": f"/transactions/{txn.id}"}
        )
        assert response.url.path == f"/transactions/{txn.id}" and "Marked as reviewed." in toasts(
            response.text
        )


# --------------------------------------------------------------------------- export


class TestExport:
    def test_download_follows_the_filters(self, client, db, card, cats):
        make_transaction(
            db, card, name="=SUM(A1)", amount="12.50", date="2026-10-20", category=cats["dining"]
        )
        make_transaction(db, card, name="OLD", amount="1.00", date="2025-01-01")
        make_transaction(
            db, card, name="DUPE", source="csv", duplicate_status="suspect", date="2026-10-21"
        )
        response = client.get("/transactions/export.csv?range=year&sort=amount")
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-disposition"] == (
            'attachment; filename="transactions-2026-01-01-to-2026-12-31.csv"'
        )
        assert response.headers["cache-control"] == "no-store"
        text = response.content.decode("utf-8")
        assert text.startswith("﻿Date,")
        rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"))))
        assert [(r[3], r[6]) for r in rows[1:]] == [("'=SUM(A1)", "12.50")]

    def test_the_export_link_round_trips(self, client, db, card):
        make_transaction(db, card, name="A", tags=["work"])
        html = client.get("/transactions?tag=work&q=a").text
        href = (
            re.search(r'href="(/transactions/export\.csv[^"]*)"', html)
            .group(1)
            .replace("&amp;", "&")
        )
        assert parse_qs(urlsplit(href).query) == {"q": ["a"], "tag": ["work"]}
        rows = list(csv.reader(io.StringIO(client.get(href).text.removeprefix("﻿"))))
        assert [r[3] for r in rows[1:]] == ["A"]
