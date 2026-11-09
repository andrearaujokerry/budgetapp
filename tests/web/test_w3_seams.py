"""Wave-3 seams on the pages, pinned by the integrator (W3-PLAN §4).

INCOME's Accounts card links to MONEY's Account filter; MONEY's card-only pickers hold with bank
accounts BANK's linking made; Amex CSV history attaches only to an Amex card through the page;
and the Accounts card leaves out accounts BANK marks as no longer shared.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from budgetapp.models import Account, Transaction
from budgetapp.services import linking
from budgetapp.services import sync as sync_service
from tests.bank_factories import BANK_INSTITUTION_ID, BANK_NAME, make_bank_account, make_bank_item
from tests.factories import get_category, make_account, make_transaction, plaid_account
from tests.web.csp import csp_problems

D = Decimal
NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


def toasts(text: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', text)]


@pytest.fixture
def accounts(db, freeze_today):
    """Harbor Bank's checking and savings and an Amex card, each with money of every kind."""
    freeze_today(now=NOW)
    checking = make_bank_account(
        db,
        mask="4417",
        current_balance=D("5200.00"),
        available_balance=D("5100.00"),
        balances_as_of=NOW,
    )
    savings = make_bank_account(
        db,
        checking.item,
        subtype="savings",
        mask="7702",
        current_balance=D("18000.00"),
        balances_as_of=NOW,
    )
    card = make_account(db, mask="1001", current_balance=D("1234.56"), balances_as_of=NOW)
    rows = [
        (checking, "-3000.00", "Paycheck"), (checking, "2450.00", "Home"),
        (checking, "120.00", "Card Payments"), (checking, "500.00", "Transfers"),
        (checking, "60.00", "Cash & ATM"), (savings, "-500.00", "Transfers"),
        (savings, "-61.50", "Interest"), (card, "120.00", "Groceries"),
        (card, "-120.00", "Card Payments"), (card, "-25.00", "Credits & Rewards"),
        (card, "-8.00", "Groceries"),
    ]  # fmt: skip
    for n, (account, amount, category) in enumerate(rows, start=1):
        make_transaction(db, account, amount=amount, date=f"2026-10-{n:02d}",
                         category=get_category(db, category))  # fmt: skip
    return checking, savings, card


def test_the_accounts_cards_links_list_each_accounts_money_and_their_sums_match(
    client, db, accounts
):
    """INC-3 × MON-4: every account on the Dashboard's Accounts card opens
    ``/transactions?account=<id>``, MONEY's Account filter, which lists exactly that account's
    rows; the list's sums are the account's raw sums. Cash is checking + savings, Owed the card."""
    checking, savings, card = accounts
    response = client.get("/")
    assert response.status_code == 200 and csp_problems(response.text, rendered=True) == []
    card_view = response.context["d"].accounts
    assert (card_view.cash, card_view.owed) == (D("23200.00"), D("1234.56"))
    links = re.findall(r'href="/transactions\?account=(\d+)"', response.text)
    assert sorted(map(int, links)) == sorted([checking.id, savings.id, card.id])
    for account in (checking, savings, card):
        listing = client.get(f"/transactions?account={account.id}")
        assert listing.status_code == 200
        page = listing.context["page"]
        amounts = list(
            db.scalars(select(Transaction.amount).where(Transaction.account_id == account.id))
        )
        assert {row.account_id for row in page.rows} == {account.id}
        assert page.total_count == len(amounts)
        assert page.sums.net == sum(amounts, D("0.00"))
        assert page.sums.charges == sum((a for a in amounts if a > 0), D("0.00"))
        assert page.sums.credits == sum((a for a in amounts if a < 0), D("0.00"))
    assert client.get(f"/transactions?account={checking.id}").context["page"].sums.net == D(
        "130.00"
    )


def test_an_account_plaid_no_longer_shares_is_marked_and_left_out_of_cash(client, db, accounts):
    """INC-3 × BANK-5: the savings account Plaid stopped listing keeps its last balance, marked,
    and Cash adds only the checking account."""
    checking, savings, _card = accounts
    sync_service.mark_not_shared(savings, NOW)
    db.flush()
    response = client.get("/")
    assert response.context["d"].accounts.cash == D("5200.00")
    row = re.search(rf'<li class="dash-account" id="dash-account-{savings.id}">.*?</li>',
                    response.text, re.S).group(0)  # fmt: skip
    assert "no longer shared, left out of Cash" in html.unescape(row)
    total = re.search(r'id="dash-accounts-cash">Cash</h3>\s*<span class="dash-accounts__total">'
                      r"(.*?)</span>\s*</span>", response.text, re.S)  # fmt: skip
    assert total and "$5,200.00" in html.unescape(total.group(1))
    other = re.search(rf'id="dash-account-{checking.id}">.*?</li>', response.text, re.S).group(0)
    assert "no longer shared" not in other


def test_cards_only_in_the_pickers_once_a_bank_is_linked(
    client, db, session_factory, settings, fake_plaid, secrets, tmp_path
):
    """MON-3 × BANK-1: with bank accounts made by BANK's linking (a Sandbox exchange, accounts
    from /accounts/get), Statements and Settings still list cards only."""
    card = make_account(db, name="Gold Card", mask="1001")
    bank = fake_plaid.add_item(
        item_id="harbor",
        institution_id=BANK_INSTITUTION_ID,
        institution_name=BANK_NAME,
        consented_products=("transactions",),
        accounts=[
            plaid_account(account_id="chk", name="Everyday Checking", official_name=None,
                          mask="4417", type="depository", subtype="checking", current="5200.00"),
            plaid_account(account_id="sav", name="High Yield Savings", official_name=None,
                          mask="7702", type="depository", subtype="savings", current="18000.00"),
        ],
    )  # fmt: skip
    meta = {
        "institution": {"name": BANK_NAME, "institution_id": BANK_INSTITUTION_ID},
        "accounts": [
            {"id": "chk", "name": "Everyday Checking", "mask": "4417", "type": "depository"},
            {"id": "sav", "name": "High Yield Savings", "mask": "7702", "type": "depository"},
        ],
    }
    linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        settings.replace(state_dir=tmp_path / "state"),
        fake_plaid.public_token_for(bank),
        meta,
    )
    db.expire_all()
    banks = list(db.scalars(select(Account).where(Account.type == "depository")))
    assert sorted(a.name for a in banks) == ["Everyday Checking", "High Yield Savings"]

    statements = client.get("/statements")
    assert [a.id for a in statements.context["accounts"]] == [card.id]
    assert "Everyday Checking" not in statements.text
    for account in banks:
        assert client.get(f"/statements?account={account.id}").status_code == 404
        assert client.get(f"/statements/{account.id}/2026-09-27").status_code == 404
    settings_page = client.get("/settings")
    assert [c.id for c in settings_page.context["view"].cards] == [card.id]


def test_amex_csv_history_attaches_only_to_an_amex_card_through_the_page(client, db):
    """BANK-8 × MON-3 (the server-side check of /import/attach): another bank's card and a
    checking account are refused with a toast and nothing moves; the Amex card takes it."""
    manual = make_account(db, manual=True)
    manual_id = manual.id
    row = make_transaction(db, manual, source="csv")
    amex = make_account(db, name="Gold Card", mask="1001")
    other = make_bank_item(db, institution_id="ins_demo_other", institution_name="Other Bank")
    other_card = make_account(db, other, name="Cash Rewards", mask="2002")
    checking = make_bank_account(db)
    for target in (other_card, checking):
        response = client.post(
            "/import/attach", data={"manual_id": str(manual_id), "account_id": str(target.id)}
        )
        assert response.status_code == 200
        assert toasts(response.text) == [
            "Choose a linked American Express card to attach the CSV history to."
        ]
        db.expire_all()
        assert db.get(Transaction, row.id).account_id == manual_id
    response = client.post(
        "/import/attach", data={"manual_id": str(manual_id), "account_id": str(amex.id)}
    )
    assert toasts(response.text)[0].startswith("Attached: 1 transaction moved")
    db.expire_all()
    assert db.get(Transaction, row.id).account_id == amex.id
    assert db.scalar(select(func.count()).select_from(Account).where(Account.id == manual_id)) == 0
