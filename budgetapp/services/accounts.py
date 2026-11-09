"""Kinds of account: cards and bank accounts (wave 3).

Plaid's account ``type`` is ``"credit"`` for a credit card and ``"depository"`` for a checking,
savings or money market account. The app links nothing else: investment, loan and mortgage
accounts are filtered out in Plaid Link. Amounts keep Plaid's sign on every account: positive is
money leaving the account, negative is money coming in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp.models import Account

CARD = "credit"
BANK = "depository"


def is_card(account: Account) -> bool:
    """A credit card, linked through Plaid or imported from an Amex CSV file."""
    return account.type == CARD


def is_bank(account: Account) -> bool:
    """A checking, savings or money market account."""
    return account.type == BANK


def card_accounts(session: Session, *, include_hidden: bool = False) -> list[Account]:
    """Credit-card accounts, linked or manual, oldest first."""
    return _of_type(session, CARD, include_hidden)


def bank_accounts(session: Session, *, include_hidden: bool = False) -> list[Account]:
    """Checking, savings and money market accounts, oldest first."""
    return _of_type(session, BANK, include_hidden)


def _of_type(session: Session, kind: str, include_hidden: bool) -> list[Account]:
    query = select(Account).where(Account.type == kind)
    if not include_hidden:
        query = query.where(Account.is_hidden.is_(False))
    return list(session.scalars(query.order_by(Account.id)))
