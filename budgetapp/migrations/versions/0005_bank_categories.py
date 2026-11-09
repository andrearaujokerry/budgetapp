"""Bank money: income, transfer and cash categories, and Plaid mappings for bank accounts.

Revision ID: 0005_bank_categories
Revises: 0003_f1_verification_fixes
Create Date: 2026-11-09

Wave 3 links checking, savings and money market accounts (Plaid ``type = "depository"``) next to
cards (W3-PLAN C2, C3).

* Categories: **Paycheck**, **Interest** and **Other Income** (kind ``income``), **Transfers**
  (``transfer``) and **Cash & ATM** (``expense``). A category with the same name (ignoring case)
  is left as it is and used when its kind is the intended one.
* Mappings for bank accounts: ``plaid_pfc`` keys ``<code>|bank``, which categorization tries before
  the plain keys for a row on a bank account (``categorize.BANK_KEY_SUFFIX``): paychecks
  (``INCOME_SALARY``, and ``INCOME_WAGES``, its PFC v1 name) → Paycheck; ``INCOME_INTEREST_EARNED``
  and ``INCOME_DIVIDENDS`` → Interest; every other ``INCOME`` code → Other Income;
  ``TRANSFER_IN`` and ``TRANSFER_OUT`` codes → Transfers, except ``TRANSFER_OUT_WITHDRAWAL``
  (ATM and cash) → Cash & ATM; ``LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`` (a card payment, positive on
  the bank side) → Card Payments. The cards' mappings of the other loan codes are card-sided (on a
  card, money in under a loan code is the card's payment, Card Payments, and a cash advance is
  spending), so they get bank keys too: a loan payment from checking is spending like a card's car
  or mortgage payment (the car, mortgage, student loan, personal loan and buy-now-pay-later codes
  keep the card mapping's category; every other ``LOAN_PAYMENTS`` code, the primary included →
  Other), and money a lender puts into checking (every ``LOAN_DISBURSEMENTS`` code) → Transfers,
  neither income nor spending. Cards keep their mappings: nothing they use changes.

Safe on a live database the owner has customized: existing categories, mappings and rules are
never changed (inserts only, ``ON CONFLICT DO NOTHING``). A bank mapping goes only to a category of
the intended kind, so a same-named category of another kind (an "Interest" the owner made for
interest charges, say) never receives bank money of the wrong kind: an income code then has no
bank mapping of its own and uses the next key categorization tries (its group's bank mapping,
then the card mapping); a transfer goes to the built-in Card Payments (a transfer, so never
spending); spending (an ATM withdrawal, a loan payment) keeps the card mapping's spending
category, else Uncategorized. Before this wave no bank account existed, so no stored transaction
changes category. The downgrade removes only what this migration added (the categories it
created, recognised by its transaction's timestamp, and only while nothing uses them).

Once applied to production, a migration is never edited: add a new one instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_bank_categories"
down_revision: str | None = "0003_f1_verification_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``categorize.BANK_KEY_SUFFIX`` (a migration never imports app code that may change later).
BANK_SUFFIX = "|bank"
CARD_PAYMENTS = "card_payments"

# (name, kind, icon) in list order after the owner's categories: spending first, then income,
# then transfers. The colour is chosen in _colour(). Not spending: the neutral grey, like Credits &
# Rewards and the system categories (migration 0003).
CATEGORIES: list[tuple[str, str, str]] = [
    ("Cash & ATM", "expense", "🏧"),
    ("Paycheck", "income", "💵"),
    ("Interest", "income", "🏦"),
    ("Other Income", "income", "💰"),
    ("Transfers", "transfer", "🔁"),
]
NEUTRAL = "other"
#: Cash & ATM is spending, so it takes a palette slot: the one fewest visible categories use,
#: olive (``cat-12``, Education's) first on a tie, then the palette's quieter end.
SLOT_PREFERENCE: tuple[str, ...] = ("cat-12", *(f"cat-{n}" for n in (16, 15, 14, 13, 11, 10, 9)))
SLOT_PREFERENCE += tuple(f"cat-{n}" for n in range(1, 9))

_INCOME = (
    "CHILD_SUPPORT",
    "CONTRACTOR",
    "GIG_ECONOMY",
    "LONG_TERM_DISABILITY",
    "MILITARY",
    "RENTAL",
    "RETIREMENT_PENSION",
    "TAX_REFUND",
    "UNEMPLOYMENT",
    "OTHER",
)
_TRANSFER_IN = (
    "ACCOUNT_TRANSFER",
    "DEPOSIT",
    "INVESTMENT_AND_RETIREMENT_FUNDS",
    "SAVINGS",
    "TRANSFER_IN_FROM_APPS",
    "WIRE",
    "OTHER_TRANSFER_IN",
)
_TRANSFER_OUT = (
    "ACCOUNT_TRANSFER",
    "CRYPTO",
    "INVESTMENT_AND_RETIREMENT_FUNDS",
    "SAVINGS",
    "TRANSFER_OUT_FROM_APPS",
    "WIRE",
    "OTHER_TRANSFER_OUT",
)
_LOAN_DISBURSEMENTS = (
    "AUTO",
    "CASH_ADVANCES",
    "EWA",
    "MORTGAGE",
    "PERSONAL",
    "STUDENT",
    "OTHER_DISBURSEMENT",
)
#: Loan payments that migration 0002 maps to a spending category of their own. Their bank keys copy
#: the card mapping (the owner's choice, if spending; else 0002's category), because the
#: ``LOAN_PAYMENTS|bank`` key below is tried before a code's plain key and would otherwise send
#: them to Other.
_LOAN_PAYMENTS_SPENDING = (
    ("BNPL", "Shopping"),
    ("CAR_PAYMENT", "Transportation"),
    ("MORTGAGE_PAYMENT", "Home"),
    ("PERSONAL_LOAN_PAYMENT", "Utilities & Bills"),
    ("STUDENT_LOAN_PAYMENT", "Education"),
)
FOLLOW_CARDS = frozenset(f"LOAN_PAYMENTS_{code}" for code, _name in _LOAN_PAYMENTS_SPENDING)

# Plaid PFC code (detailed or primary) -> (category, kind) for rows on a bank account. Every code
# of the INCOME, TRANSFER_IN, TRANSFER_OUT, LOAN_DISBURSEMENTS and LOAN_PAYMENTS primaries (PFC v2,
# RESEARCH T1.6) is listed, so each shows in the "Plaid & Amex categories" editor with its own
# bank mapping.
BANK_MAPPINGS: list[tuple[str, str, str]] = [
    ("INCOME", "Other Income", "income"),
    ("INCOME_SALARY", "Paycheck", "income"),
    ("INCOME_WAGES", "Paycheck", "income"),  # PFC v1's name for INCOME_SALARY
    ("INCOME_INTEREST_EARNED", "Interest", "income"),
    ("INCOME_DIVIDENDS", "Interest", "income"),
    *((f"INCOME_{code}", "Other Income", "income") for code in _INCOME),
    ("TRANSFER_IN", "Transfers", "transfer"),
    *((f"TRANSFER_IN_{code}", "Transfers", "transfer") for code in _TRANSFER_IN),
    ("TRANSFER_OUT", "Transfers", "transfer"),
    *((f"TRANSFER_OUT_{code}", "Transfers", "transfer") for code in _TRANSFER_OUT),
    ("TRANSFER_OUT_WITHDRAWAL", "Cash & ATM", "expense"),
    # Money a lender deposits (a personal loan, a payday or wage advance): not income, not a refund.
    ("LOAN_DISBURSEMENTS", "Transfers", "transfer"),
    *((f"LOAN_DISBURSEMENTS_{code}", "Transfers", "transfer") for code in _LOAN_DISBURSEMENTS),
    # A debt payment from checking is spending, like 0002's car and mortgage payments; only a
    # payment to a credit card is a card payment (the card's charges are the spending).
    ("LOAN_PAYMENTS", "Other", "expense"),
    *((f"LOAN_PAYMENTS_{code}", name, "expense") for code, name in _LOAN_PAYMENTS_SPENDING),
    ("LOAN_PAYMENTS_CASH_ADVANCES", "Other", "expense"),
    ("LOAN_PAYMENTS_EWA", "Other", "expense"),
    ("LOAN_PAYMENTS_OTHER_PAYMENT", "Other", "expense"),
    ("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "Card Payments", "transfer"),
]

_INSERT_CATEGORY = sa.text(
    "INSERT INTO categories (name, kind, color, icon, sort_order) "
    "VALUES (:name, :kind, :color, :icon, :sort_order) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_MAPPING = sa.text(
    "INSERT INTO category_mappings (source, external_key, category_id) "
    "VALUES ('plaid_pfc', :key, :category_id) "
    "ON CONFLICT (source, external_key) DO NOTHING"
)


def _colour(bind: sa.Connection, kind: str) -> str:
    if kind != "expense":
        return NEUTRAL
    used = dict.fromkeys(SLOT_PREFERENCE, 0)
    for (color,) in bind.execute(sa.text("SELECT color FROM categories WHERE NOT is_hidden")):
        key = (color or "").strip().lower()
        if key in used:
            used[key] += 1
    return min(SLOT_PREFERENCE, key=lambda slot: (used[slot], SLOT_PREFERENCE.index(slot)))


def _system(bind: sa.Connection, key: str) -> int | None:
    query = sa.text("SELECT id FROM categories WHERE system_key = :key")
    return bind.execute(query, {"key": key}).scalar()


def _card_spending(bind: sa.Connection, code: str) -> int | None:
    """The category of ``code``'s card mapping (its plain key) when that category is spending."""
    return bind.execute(
        sa.text(
            "SELECT c.id FROM category_mappings m JOIN categories c ON c.id = m.category_id "
            "WHERE m.source = 'plaid_pfc' AND m.external_key = :code AND c.kind = 'expense'"
        ),
        {"code": code},
    ).scalar()


def _target(bind: sa.Connection, code: str, name: str, kind: str) -> int | None:
    """The category ``code``'s bank mapping goes to: ``name`` when it has ``kind``.

    A loan payment in :data:`FOLLOW_CARDS` first takes its card mapping's category when that is
    spending (what the owner chose for it, or 0002's). When ``name`` belongs to a category of
    another kind: a transfer goes to Card Payments (a transfer, never spending); spending (an ATM
    withdrawal, a loan payment) keeps the card mapping's category if that is spending, else
    Uncategorized; income gets no bank mapping of its own and uses its group's (Other Income) or
    the card mapping, which is income too unless the owner changed it.
    """
    if name == "Card Payments":
        return _system(bind, CARD_PAYMENTS)
    if code in FOLLOW_CARDS:
        card = _card_spending(bind, code)
        if card is not None:
            return card
    found = bind.execute(
        sa.text("SELECT id FROM categories WHERE lower(name) = lower(:name) AND kind = :kind"),
        {"name": name, "kind": kind},
    ).scalar()
    if found is not None or kind == "income":
        return found
    if kind == "transfer":
        return _system(bind, CARD_PAYMENTS)
    card = _card_spending(bind, code)
    return card if card is not None else _system(bind, "uncategorized")


def upgrade() -> None:
    bind = op.get_bind()
    last = bind.execute(
        sa.text("SELECT coalesce(max(sort_order), 0) FROM categories WHERE system_key IS NULL")
    ).scalar()
    for order, (name, kind, icon) in enumerate(CATEGORIES, start=1):
        bind.execute(
            _INSERT_CATEGORY,
            {
                "name": name,
                "kind": kind,
                "color": _colour(bind, kind),
                "icon": icon,
                "sort_order": last + order * 10,
            },
        )
    for code, name, kind in BANK_MAPPINGS:
        category_id = _target(bind, code, name, kind)
        if category_id is not None:
            bind.execute(_INSERT_MAPPING, {"key": code + BANK_SUFFIX, "category_id": category_id})


def downgrade() -> None:
    bind = op.get_bind()
    keys = [code + BANK_SUFFIX for code, _name, _kind in BANK_MAPPINGS]
    # The upgrade's rows share its transaction's now(): the categories it created have the
    # created_at of its earliest bank mapping (a later edit of one adds no earlier row). A
    # same-named category the owner made before the upgrade (reused, not created) is older.
    created = bind.execute(
        sa.text(
            "SELECT min(created_at) FROM category_mappings "
            "WHERE source = 'plaid_pfc' AND external_key = ANY(:keys)"
        ),
        {"keys": keys},
    ).scalar()
    for key in keys:
        bind.execute(
            sa.text(
                "DELETE FROM category_mappings WHERE source = 'plaid_pfc' AND external_key = :key"
            ),
            {"key": key},
        )
    if created is None:
        return
    # A category goes only when this migration made it (its kind, icon and transaction) and
    # nothing uses it.
    for name, kind, icon in CATEGORIES:
        bind.execute(
            sa.text(
                "DELETE FROM categories c WHERE lower(c.name) = lower(:name) "
                "AND c.kind = :kind AND c.icon = :icon AND c.system_key IS NULL "
                "AND c.created_at = :created "
                "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM rules r WHERE r.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM budgets b WHERE b.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM category_mappings m WHERE m.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM alert_events a WHERE a.category_id = c.id)"
            ),
            {"name": name, "kind": kind, "icon": icon, "created": created},
        )
