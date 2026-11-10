"""``services/accounts.py`` (W3-PLAN C1): cards and bank accounts."""

from __future__ import annotations

from pathlib import Path

from budgetapp.services import accounts
from tests.bank_factories import make_bank_account, make_bank_item
from tests.factories import make_account

PLAN_C1 = (
    'Plaid\'s account ``type`` is ``"credit"`` for a credit card and ``"depository"`` for a '
    "checking,"
)


def test_kinds_are_plaids_account_types():
    assert accounts.CARD == "credit" and accounts.BANK == "depository"


def test_is_card_and_is_bank(db):
    card = make_account(db)
    manual = make_account(db, manual=True)  # "American Express (CSV)"
    checking = make_bank_account(db)
    savings = make_bank_account(db, checking.item, subtype="savings")
    untyped = make_account(db, type=None)
    assert [accounts.is_card(a) for a in (card, manual, checking, savings, untyped)] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert [accounts.is_bank(a) for a in (card, manual, checking, savings, untyped)] == [
        False,
        False,
        True,
        True,
        False,
    ]


def test_lists_are_oldest_first_and_leave_hidden_ones_out(db):
    bank = make_bank_item(db)
    checking = make_bank_account(db, bank)
    card = make_account(db)
    hidden_card = make_account(db, is_hidden=True)
    savings = make_bank_account(db, bank, subtype="savings", is_hidden=True)
    manual = make_account(db, manual=True)
    assert accounts.card_accounts(db) == [card, manual]
    assert accounts.card_accounts(db, include_hidden=True) == [card, hidden_card, manual]
    assert accounts.bank_accounts(db) == [checking]
    assert accounts.bank_accounts(db, include_hidden=True) == [checking, savings]


def test_no_accounts(db):
    assert accounts.card_accounts(db) == [] and accounts.bank_accounts(db) == []


def test_the_module_is_the_plans_text():
    """C1: identical bytes in every wave-3 branch so the merge is clean (never edit it here)."""
    source = (Path(accounts.__file__)).read_text()
    assert source.startswith('"""Kinds of account: cards and bank accounts (wave 3).')
    assert PLAN_C1 in source.replace("\n", " ")
    assert source.count("def ") == 5 and source.endswith("order_by(Account.id)))\n")
