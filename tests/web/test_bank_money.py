"""Bank money on the pages this package touches (W3-PLAN MON-1, MON-3): the bank rows of the
"Plaid & Amex categories" editor, the new categories, the card-only Statements and Import pages,
and the Rules form's account picker. The Transactions and Settings pages have their own tests."""

from __future__ import annotations

import html
import re

import pytest

from budgetapp import clock
from budgetapp.models import CategoryMapping
from tests.bank_factories import make_bank_account
from tests.factories import get_category, make_account, make_statement, make_transaction
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems


def toasts(text: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', text)]


@pytest.fixture
def checking(db):
    return make_bank_account(db, mask="4417")


class TestMappingEditor:
    def test_bank_rows_carry_a_badge_and_their_own_label(self, client, db, checking):
        make_transaction(
            db, checking, amount="-3412.18", pfc_primary="INCOME", pfc_detailed="INCOME_SALARY"
        )
        response = client.get("/categories/mappings?source=plaid_pfc&show=used")
        page = response.text
        assert csp_problems(page, rendered=True) == []
        row = re.search(
            r'<tr>(?:(?!</tr>).)*value="INCOME_SALARY\|bank"(?:(?!</tr>).)*</tr>', page, re.S
        )
        assert row is not None
        assert ">Bank accounts<" in row.group(0) and "Salary</span>" in row.group(0)
        assert 'aria-label="Income: Salary (bank accounts) goes to"' in row.group(0)
        assert "<code>INCOME_SALARY</code>" in row.group(0)

    def test_changing_a_bank_row_says_bank_accounts(self, client, db, checking):
        salary = get_category(db, "Other Income")
        response = client.post(
            "/categories/mappings",
            data={
                "source": "plaid_pfc",
                "key": "INCOME_SALARY|bank",
                "show": "all",
                "category_id": str(salary.id),
            },
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "categories/_mapping_group.html"
        assert toasts(response.text) == ["Salary (bank accounts) now goes to Other Income."]
        stored = db.query(CategoryMapping).filter_by(external_key="INCOME_SALARY|bank").one()
        assert stored.category_id == salary.id
        card_row = db.query(CategoryMapping).filter_by(external_key="INCOME_SALARY").one()
        assert card_row.category_id == get_category(db, "Credits & Rewards").id

    def test_an_amex_key_never_takes_a_bank_suffix(self, client, db):
        response = client.post(
            "/categories/mappings",
            data={
                "source": "amex_csv",
                "key": "Travel|bank",
                "show": "all",
                "category_id": str(get_category(db, "Travel").id),
            },
            headers=HTMX,
        )
        assert response.status_code == 409


def test_the_categories_page_lists_the_bank_categories(client, db):
    page = client.get("/categories").text
    for name, kind in (
        ("Paycheck", "Credits &amp; income"),
        ("Interest", "Credits &amp; income"),
        ("Other Income", "Credits &amp; income"),
        ("Transfers", "Card payments &amp; transfers"),
        ("Cash &amp; ATM", "Spending"),
    ):
        row = re.search(rf">{re.escape(name)}</span>(?:(?!</tr>).)*</tr>", page, re.S)
        assert row is not None and kind in row.group(0), name
    assert "Not spending: paychecks, interest, cash back and other money coming in." in page
    assert csp_problems(page, rendered=True) == []


class TestCardOnlyPages:
    def test_statements_never_list_or_open_a_bank_account(self, client, db, checking):
        card = make_account(db, statement_closing_day=27)
        make_statement(db, card, "2026-09-27")
        page = client.get("/statements").text
        assert "Everyday Checking" not in page
        assert client.get(f"/statements?account={checking.id}").status_code == 404
        assert client.get(f"/statements/{checking.id}/2026-09-27").status_code == 404
        refused = client.post(
            "/statements", data={"account_id": str(checking.id), "closing_date": "2026-09-27"}
        )
        assert refused.status_code == 404

    def test_import_offers_only_amex_cards(self, client, db, checking):
        amex = make_account(db, name="Gold Card", mask="1001")
        page = client.get("/import").text
        options = re.findall(r'<option value="([^"]*)"', page)
        assert options == [str(amex.id), "new"]
        assert "Everyday Checking" not in page


def test_the_rule_form_picks_any_account(client, db, checking):
    """W3-MONEY review: the rule form's account picker lists cards and bank accounts, so it is
    labelled "Account" ("Any account"), not "Card"."""
    card = make_account(db, name="Gold Card", mask="1001")
    page = client.get("/rules/new").text
    assert csp_problems(page, rendered=True) == []
    field = re.search(
        r'<label[^>]*for="f-account_id"[^>]*>(.*?)</label>\s*<select(.*?)</select>', page, re.S
    )
    assert field is not None
    assert field.group(1).strip() == "Account"
    options = re.findall(r'<option value="([^"]*)">([^<]*)</option>', field.group(2))
    assert options[0] == ("", "Any account")
    assert {value for value, _ in options[1:]} >= {str(card.id), str(checking.id)}


#: How the details describe a row in a transfer category other than Card Payments (money-1).
TRANSFER_COUNTS_AS = (
    "A transfer, not spending or income. If it is really spending or income (a payment to "
    "someone, a deposit), change its category."
)
#: What the pages said before (final review money-1): every Transfers row moved money between
#: your own accounts.
OWN_ACCOUNTS_ONLY = (
    "A transfer between your accounts",
    "moving money between your accounts",
    "transfers between your accounts are neither",
)


def counts_as(page: str) -> str:
    found = re.search(r"<dt>Counts as</dt>\s*<dd>(.*?)</dd>", page, re.S)
    assert found is not None
    return html.unescape(found.group(1).strip())


def test_a_transfer_is_never_called_a_move_between_your_accounts(client, db, checking):
    """Final review money-1: Plaid files payments to people, wires and check deposits as transfers
    too (its TRANSFER_* codes, W3-PLAN C3), so no page says a Transfers row moved money between
    your own accounts, and the details say what to do when one is really spending or income."""
    zelle = make_transaction(
        db,
        checking,
        amount="300.00",
        name="ZELLE PAYMENT TO JORDAN LEE",
        category=get_category(db, "Transfers"),
        category_source="plaid",
        pfc_primary="TRANSFER_OUT",
        pfc_detailed="TRANSFER_OUT_TRANSFER_OUT_FROM_APPS",
    )
    paycheck = get_category(db, "Paycheck")
    make_transaction(db, checking, amount="-2000.00", category=paycheck, date=clock.today())
    detail = client.get(f"/transactions/{zelle.id}").text
    assert counts_as(detail) == TRANSFER_COUNTS_AS
    split = html.unescape(client.get(f"/transactions/{zelle.id}/split").text)
    assert (
        "A transfer isn't spending, so it can't be split. If this one is really spending" in split
    )
    categories = html.unescape(client.get("/categories").text)
    assert (
        "Not spending or income: card payments, money moved between your accounts and anything "
        "else Plaid files as a transfer."
    ) in categories
    trends = client.get("/trends").text
    assert "Card payments and transfers are neither." in trends
    for page in (detail, split, categories, trends):
        assert not [old for old in OWN_ACCOUNTS_ONLY if old in html.unescape(page)]
