"""Builders for bank Items and accounts (wave 3), next to ``tests/factories.py``.

A bank account is Plaid ``type = "depository"`` (checking, savings, money market). The Item is a
made-up bank; no real institution is named.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from budgetapp.models import Account, PlaidItem
from tests.factories import make_account, make_item

BANK_INSTITUTION_ID = "ins_demo_harbor"
BANK_NAME = "Harbor Bank"
_NAMES = {"checking": "Everyday Checking", "savings": "High Yield Savings"}


def make_bank_item(db: Session, **kwargs: Any) -> PlaidItem:
    """A linked bank login (no Liabilities: bank accounts have no statements)."""
    values: dict[str, Any] = {
        "institution_id": BANK_INSTITUTION_ID,
        "institution_name": BANK_NAME,
        "consented_products": ["transactions"],
    }
    values.update(kwargs)
    return make_item(db, **values)


def make_bank_account(
    db: Session, item: PlaidItem | None = None, *, subtype: str = "checking", **kwargs: Any
) -> Account:
    """A checking (default) or savings account at a bank Item (made when not given)."""
    values: dict[str, Any] = {
        "name": _NAMES.get(subtype, "Money Market"),
        "official_name": None,
        "type": "depository",
        "subtype": subtype,
    }
    values.update(kwargs)
    return make_account(db, item or make_bank_item(db), **values)
