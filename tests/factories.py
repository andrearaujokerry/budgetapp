"""Builders for every model and for Plaid-shaped JSON (used by tests of every package).

Model builders take the session first, fill sensible defaults, ``flush()`` and return the object.
Plaid builders return dicts shaped like the real API (RESEARCH T1.5, T2.6, T2.7): dates are ISO
strings and amounts are ``Decimal`` (what ``json.loads(parse_float=Decimal)`` produces).
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import (
    Account,
    AlertEvent,
    AppSettings,
    Budget,
    Category,
    CategoryMapping,
    CsvImport,
    PlaidItem,
    RecurringOverride,
    Rule,
    Statement,
    SyncRun,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
    get_app_settings,
)
from budgetapp.money import to_money

DEFAULT_DATE = dt.date(2026, 10, 15)
_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


def money(value: Any) -> Decimal:
    return to_money(value if not isinstance(value, float) else str(value))


# --------------------------------------------------------------------------- categories


def get_category(db: Session, name: str) -> Category:
    """A category by name (case-insensitive), e.g. ``get_category(db, "Groceries")``."""
    category = db.scalars(select(Category).where(Category.name.ilike(name))).one_or_none()
    if category is None:
        raise LookupError(f"no category named {name!r}")
    return category


def uncategorized(db: Session) -> Category:
    return db.scalars(select(Category).where(Category.system_key == "uncategorized")).one()


def card_payments(db: Session) -> Category:
    return db.scalars(select(Category).where(Category.system_key == "card_payments")).one()


def make_category(
    db: Session, name: str | None = None, *, kind: str = "expense", **kwargs: Any
) -> Category:
    category = Category(name=name or f"Test category {_n()}", kind=kind, **kwargs)
    db.add(category)
    db.flush()
    return category


def make_mapping(db: Session, source: str, key: str, category: Category) -> CategoryMapping:
    mapping = CategoryMapping(source=source, external_key=key, category_id=category.id)
    db.add(mapping)
    db.flush()
    return mapping


# --------------------------------------------------------------------------- items & accounts


def make_item(db: Session, **kwargs: Any) -> PlaidItem:
    now = clock.now()
    values: dict[str, Any] = {
        "plaid_item_id": f"item-{_n()}",
        "institution_id": "ins_10",
        "institution_name": "American Express",
        "linked_at": now,
        "last_consented_at": now,
        "last_sync_success_at": now,
        "consented_products": ["transactions", "liabilities"],
    }
    values.update(kwargs)
    item = PlaidItem(**values)
    db.add(item)
    db.flush()
    return item


def make_account(
    db: Session, item: PlaidItem | None = None, *, manual: bool = False, **kwargs: Any
) -> Account:
    """A Plaid credit-card account (creating an Item if needed), or a manual one with ``manual``."""
    n = _n()
    values: dict[str, Any] = {
        "name": "Platinum Card" if not manual else "American Express (CSV)",
        "official_name": "Platinum Card®",
        "mask": f"{1000 + n % 9000}",
        "type": "credit",
        "subtype": "credit card",
    }
    if not manual:
        item = item or make_item(db)
        values["item_id"] = item.id
        values["plaid_account_id"] = f"acc-{n}"
    values.update(kwargs)
    account = Account(**values)
    db.add(account)
    db.flush()
    return account


# --------------------------------------------------------------------------- transactions


def make_transaction(
    db: Session,
    account: Account | None = None,
    *,
    amount: Any = "10.00",
    date: dt.date | str = DEFAULT_DATE,
    category: Category | None = None,
    source: str = "plaid",
    pending: bool = False,
    **kwargs: Any,
) -> Transaction:
    """A transaction; defaults: a Plaid posted charge in Uncategorized (``default``)."""
    account = account or make_account(db)
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    n = _n()
    values: dict[str, Any] = {
        "account_id": account.id,
        "source": source,
        "pending": pending,
        "date": date,
        "posted_date": None if pending else date,
        "name": f"MERCHANT {n}",
        "amount": money(amount),
        "iso_currency_code": "USD",
        "category_id": (category or uncategorized(db)).id,
        "category_source": "default",
    }
    if source == "plaid":
        values["plaid_transaction_id"] = f"txn-{n}"
    else:
        values["csv_reference"] = f"3{DEFAULT_DATE.year}{n:013d}"
    values.update(kwargs)
    if isinstance(values.get("authorized_date"), str):
        values["authorized_date"] = dt.date.fromisoformat(values["authorized_date"])
    if isinstance(values.get("posted_date"), str):
        values["posted_date"] = dt.date.fromisoformat(values["posted_date"])
    txn = Transaction(**values)
    db.add(txn)
    db.flush()
    return txn


def make_split(
    db: Session, txn: Transaction, category: Category, amount: Any, note: str | None = None
) -> TransactionSplit:
    split = TransactionSplit(category_id=category.id, amount=money(amount), note=note)
    txn.splits.append(split)
    db.flush()
    return split


def make_tombstone(db: Session, account: Account, **kwargs: Any) -> TransactionTombstone:
    values: dict[str, Any] = {
        "plaid_transaction_id": f"gone-{_n()}",
        "account_id": account.id,
        "amount": money("10.00"),
        "date": DEFAULT_DATE,
        "name": "REMOVED MERCHANT",
        "pending": True,
    }
    values.update(kwargs)
    tombstone = TransactionTombstone(**values)
    db.add(tombstone)
    db.flush()
    return tombstone


def make_csv_import(db: Session, account: Account, **kwargs: Any) -> CsvImport:
    values: dict[str, Any] = {
        "filename": f"activity-{_n()}.csv",
        "file_sha256": "0" * 64,
        "account_id": account.id,
    }
    values.update(kwargs)
    csv_import = CsvImport(**values)
    db.add(csv_import)
    db.flush()
    return csv_import


# ---------------------------------------------------------------- rules, budgets, statements


def make_rule(db: Session, category: Category, **kwargs: Any) -> Rule:
    values: dict[str, Any] = {"category_id": category.id}
    values.update(kwargs)
    if not any(
        values.get(k) is not None
        for k in (
            "merchant_pattern",
            "description_pattern",
            "amount_min",
            "amount_max",
            "account_id",
        )
    ):
        values["merchant_pattern"] = f"MERCHANT {_n()}"
    for key in ("amount_min", "amount_max"):
        if values.get(key) is not None:
            values[key] = money(values[key])
    rule = Rule(**values)
    db.add(rule)
    db.flush()
    return rule


def make_budget(
    db: Session,
    category: Category | None = None,
    amount: Any = "100.00",
    *,
    effective_from: dt.date = dt.date(2026, 1, 1),
    period: str = "monthly",
    **kwargs: Any,
) -> Budget:
    """A category budget, or the overall target when ``category`` is None."""
    budget = Budget(
        scope="category" if category is not None else "overall",
        category_id=category.id if category is not None else None,
        amount=money(amount) if amount is not None else None,
        effective_from=effective_from,
        period=period,
        **kwargs,
    )
    db.add(budget)
    db.flush()
    return budget


def make_statement(
    db: Session, account: Account, closing_date: dt.date | str, **kwargs: Any
) -> Statement:
    if isinstance(closing_date, str):
        closing_date = dt.date.fromisoformat(closing_date)
    values: dict[str, Any] = {
        "account_id": account.id,
        "closing_date": closing_date,
        "closing_date_source": "user",
    }
    values.update(kwargs)
    statement = Statement(**values)
    db.add(statement)
    db.flush()
    return statement


# --------------------------------------------------------------------------- other tables


def set_app_settings(db: Session, **kwargs: Any) -> AppSettings:
    """Change the single ``app_settings`` row (e.g. ``budget_period_mode="statement"``)."""
    row = get_app_settings(db)
    for key, value in kwargs.items():
        setattr(row, key, value)
    db.flush()
    return row


def make_sync_run(db: Session, item: PlaidItem, **kwargs: Any) -> SyncRun:
    values: dict[str, Any] = {"item_id": item.id, "trigger": "manual", "status": "success"}
    values.update(kwargs)
    run = SyncRun(**values)
    db.add(run)
    db.flush()
    return run


def make_alert_event(db: Session, **kwargs: Any) -> AlertEvent:
    values: dict[str, Any] = {
        "kind": "test",
        "dedupe_key": f"test:{_n()}",
        "title": "Test alert",
        "message": "Something happened.",
    }
    values.update(kwargs)
    event = AlertEvent(**values)
    db.add(event)
    db.flush()
    return event


def make_recurring_override(
    db: Session, merchant_key: str, status: str = "ignored"
) -> RecurringOverride:
    override = RecurringOverride(merchant_key=merchant_key, status=status)
    db.add(override)
    db.flush()
    return override


# --------------------------------------------------------------------------- Plaid-shaped JSON


def plaid_txn(
    *,
    transaction_id: str | None = None,
    account_id: str = "acc-credit-1",
    amount: Any = "12.34",
    date: dt.date | str = DEFAULT_DATE,
    authorized_date: dt.date | str | None = None,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    name: str = "BLUE BOTTLE COFFEE",
    merchant_name: str | None = "Blue Bottle Coffee",
    original_description: str | None = None,
    pfc_primary: str | None = "FOOD_AND_DRINK",
    pfc_detailed: str | None = "FOOD_AND_DRINK_COFFEE",
    pfc_confidence: str = "VERY_HIGH",
    transaction_code: str | None = None,
    iso_currency_code: str | None = "USD",
    payment_channel: str = "in store",
    account_owner: str | None = None,
    merchant_entity_id: str | None = None,
    location: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A /transactions/sync ``added``/``modified`` entry with every field Plaid always sends."""
    iso = lambda d: d.isoformat() if isinstance(d, dt.date) else d  # noqa: E731
    pfc = None
    if pfc_primary or pfc_detailed:
        pfc = {
            "primary": pfc_primary,
            "detailed": pfc_detailed,
            "confidence_level": pfc_confidence,
            "version": "v2",
        }
    txn: dict[str, Any] = {
        "transaction_id": transaction_id or f"plaid-txn-{_n()}",
        "account_id": account_id,
        "amount": money(amount),
        "iso_currency_code": iso_currency_code,
        "unofficial_currency_code": None,
        "date": iso(date),
        "datetime": None,
        "authorized_date": iso(authorized_date),
        "authorized_datetime": None,
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "name": name,
        "merchant_name": merchant_name,
        "original_description": original_description if original_description is not None else name,
        "merchant_entity_id": merchant_entity_id,
        "logo_url": None,
        "website": None,
        "payment_channel": payment_channel,
        "payment_meta": {
            "by_order_of": None,
            "payee": None,
            "payer": None,
            "payment_method": None,
            "payment_processor": None,
            "ppd_id": None,
            "reason": None,
            "reference_number": None,
        },
        "location": location
        or {
            "address": None,
            "city": None,
            "region": None,
            "postal_code": None,
            "country": None,
            "lat": None,
            "lon": None,
            "store_number": None,
        },
        "account_owner": account_owner,
        "transaction_code": transaction_code,
        "transaction_type": "place",
        "personal_finance_category": pfc,
        "personal_finance_category_icon_url": None,
        "counterparties": [],
        "category": None,
        "category_id": None,
        "check_number": None,
        "merchant_category_code": None,
    }
    txn.update(extra)
    return txn


def plaid_account(
    *,
    account_id: str = "acc-credit-1",
    name: str = "Platinum Card",
    official_name: str | None = "Platinum Card®",
    mask: str | None = "1001",
    type: str = "credit",
    subtype: str | None = "credit card",
    current: Any = "1234.56",
    available: Any = None,
    limit: Any = None,
    iso_currency_code: str | None = "USD",
) -> dict[str, Any]:
    """An AccountBase dict with balances (``limit`` is often null on Amex charge cards)."""
    return {
        "account_id": account_id,
        "balances": {
            "available": money(available) if available is not None else None,
            "current": money(current) if current is not None else None,
            "limit": money(limit) if limit is not None else None,
            "iso_currency_code": iso_currency_code,
            "unofficial_currency_code": None,
            "last_updated_datetime": None,
        },
        "mask": mask,
        "name": name,
        "official_name": official_name,
        "type": type,
        "subtype": subtype,
        "holder_category": "personal",
    }


def plaid_credit_liability(
    *,
    account_id: str = "acc-credit-1",
    last_statement_issue_date: dt.date | str | None = dt.date(2026, 10, 3),
    last_statement_balance: Any = "1500.00",
    minimum_payment_amount: Any = "40.00",
    next_payment_due_date: dt.date | str | None = dt.date(2026, 10, 28),
    last_payment_amount: Any = "1200.00",
    last_payment_date: dt.date | str | None = dt.date(2026, 9, 25),
    is_overdue: bool | None = False,
) -> dict[str, Any]:
    """A ``liabilities.credit[]`` entry (every money field may be null for Amex)."""
    iso = lambda d: d.isoformat() if isinstance(d, dt.date) else d  # noqa: E731
    opt = lambda v: money(v) if v is not None else None  # noqa: E731
    return {
        "account_id": account_id,
        "aprs": [],
        "is_overdue": is_overdue,
        "last_payment_amount": opt(last_payment_amount),
        "last_payment_date": iso(last_payment_date),
        "last_statement_issue_date": iso(last_statement_issue_date),
        "last_statement_balance": opt(last_statement_balance),
        "minimum_payment_amount": opt(minimum_payment_amount),
        "next_payment_due_date": iso(next_payment_due_date),
    }
