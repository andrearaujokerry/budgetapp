"""F1 verification fixes: schema hardening, a leaner v_allocations, credit mappings, colours.

Revision ID: 0003_f1_verification_fixes
Revises: 0002_seed_defaults
Create Date: 2026-10-28

* ``plaid_items.error_display_message``: Plaid's ``display_message`` for the user, kept apart from
  the technical ``error_message`` (cleared on success like ``error_code``).
* ``accounts``: a row is either manual (no Item, no Plaid account id) or linked (both), never half.
* ``categories``: the system categories keep their kinds (Uncategorized ``expense``, Card Payments
  ``transfer``), which the spending definition relies on.
* ``v_allocations``: the CTE reads only the twelve columns the view needs instead of
  ``SELECT *`` (no full-width copy of the table per query, and unrelated ``transactions`` columns
  can change without recreating the view). Same output columns, so ``CREATE OR REPLACE`` works.
* Amex CSV credits: negative rows in ``Fees & Adjustments`` (cash back, billing-inquiry credits)
  map to Credits & Rewards through ``<key>|credit`` mappings, which categorization tries first for
  negative amounts (fee reversals keep the plain key). Positive fees stay in Fees & Interest.
* Category colours: the seeded hex colours become the theme-aware, colour-blind-checked palette
  slots (``cat-1`` … ``cat-16``) and ``other``; rows the user recoloured are left alone.

Once applied to production, a migration is never edited: add a new one instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_f1_verification_fixes"
down_revision: str | None = "0002_seed_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

V_ALLOCATIONS = """
CREATE OR REPLACE VIEW v_allocations AS
WITH t AS (
    SELECT id, account_id, date, authorized_date, posted_date, pending, category_id, amount,
           pfc_primary, pfc_detailed, transaction_code, source
    FROM transactions
    WHERE duplicate_status <> 'suspect'
),
s AS (SELECT transaction_id, SUM(amount) AS total FROM transaction_splits GROUP BY 1)
SELECT t.id AS transaction_id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date) AS spend_date, t.pending,
       sp.category_id, sp.amount, t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t JOIN s ON s.transaction_id = t.id AND s.total = t.amount
JOIN transaction_splits sp ON sp.transaction_id = t.id
UNION ALL
SELECT t.id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date), t.pending, t.category_id, t.amount,
       t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t LEFT JOIN s ON s.transaction_id = t.id
WHERE s.transaction_id IS NULL OR s.total <> t.amount
"""

# The 0001 definition, restored by the downgrade.
V_ALLOCATIONS_0001 = """
CREATE OR REPLACE VIEW v_allocations AS
WITH t AS (SELECT * FROM transactions WHERE duplicate_status <> 'suspect'),
s AS (SELECT transaction_id, SUM(amount) AS total FROM transaction_splits GROUP BY 1)
SELECT t.id AS transaction_id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date) AS spend_date, t.pending,
       sp.category_id, sp.amount, t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t JOIN s ON s.transaction_id = t.id AND s.total = t.amount
JOIN transaction_splits sp ON sp.transaction_id = t.id
UNION ALL
SELECT t.id, t.account_id, t.date, t.authorized_date, t.posted_date,
       COALESCE(t.authorized_date, t.date), t.pending, t.category_id, t.amount,
       t.pfc_primary, t.pfc_detailed, t.transaction_code, t.source
FROM t LEFT JOIN s ON s.transaction_id = t.id
WHERE s.transaction_id IS NULL OR s.total <> t.amount
"""

ACCOUNTS_CHECK = "(item_id IS NULL) = (plaid_account_id IS NULL)"
SYSTEM_KIND_CHECK = (
    "system_key IS NULL OR (system_key = 'uncategorized' AND kind = 'expense') "
    "OR (system_key = 'card_payments' AND kind = 'transfer')"
)

#: Amex CSV keys tried first for NEGATIVE rows (see ``categorize.CREDIT_KEY_SUFFIX``).
CREDIT_MAPPINGS: list[tuple[str, str]] = [
    ("Fees & Adjustments-Fees & Adjustments|credit", "Credits & Rewards"),
    ("Fees & Adjustments|credit", "Credits & Rewards"),
]

# (seeded name, colour seeded by 0002, palette value). The palette's slots 1-8 are its strongest
# set and go to the categories that dominate a typical month; related categories (Dining vs
# Groceries, Travel vs Transportation, Shopping vs Gifts, Subscriptions vs Entertainment, Health
# vs Personal Care) sit in different hue families.
CATEGORY_COLOURS: list[tuple[str, str, str]] = [
    ("Travel", "#2563EB", "cat-1"),
    ("Dining & Drinks", "#EA580C", "cat-2"),
    ("Groceries", "#16A34A", "cat-3"),
    ("Transportation", "#0891B2", "cat-4"),
    ("Shopping", "#9333EA", "cat-5"),
    ("Home", "#A16207", "cat-6"),
    ("Subscriptions & Streaming", "#7C3AED", "cat-7"),
    ("Entertainment", "#DB2777", "cat-8"),
    ("Utilities & Bills", "#CA8A04", "cat-9"),
    ("Other", "#78716C", "cat-10"),
    ("Personal Care", "#E11D48", "cat-11"),
    ("Education", "#4F46E5", "cat-12"),
    ("Health & Fitness", "#65A30D", "cat-13"),
    ("Fees & Interest", "#9F1239", "cat-14"),
    ("Services", "#0D9488", "cat-15"),
    ("Gifts & Donations", "#DC2626", "cat-16"),
    # Not spending: the neutral grey, like the two system categories below.
    ("Credits & Rewards", "#059669", "other"),
]
SYSTEM_COLOURS: list[tuple[str, str, str]] = [
    ("uncategorized", "#9CA3AF", "other"),
    ("card_payments", "#64748B", "other"),
]

_RECOLOUR_BY_NAME = sa.text(
    "UPDATE categories SET color = :new, updated_at = now() "
    "WHERE system_key IS NULL AND lower(name) = lower(:name) AND lower(color) = lower(:old)"
)
_RECOLOUR_BY_SYSTEM_KEY = sa.text(
    "UPDATE categories SET color = :new, updated_at = now() "
    "WHERE system_key = :key AND lower(color) = lower(:old)"
)


def _recolour(*, forward: bool) -> None:
    bind = op.get_bind()
    for name, seeded, palette in CATEGORY_COLOURS:
        old, new = (seeded, palette) if forward else (palette, seeded)
        bind.execute(_RECOLOUR_BY_NAME, {"name": name, "old": old, "new": new})
    for key, seeded, palette in SYSTEM_COLOURS:
        old, new = (seeded, palette) if forward else (palette, seeded)
        bind.execute(_RECOLOUR_BY_SYSTEM_KEY, {"key": key, "old": old, "new": new})


def upgrade() -> None:
    op.add_column("plaid_items", sa.Column("error_display_message", sa.Text(), nullable=True))
    op.create_check_constraint(op.f("ck_accounts_manual_or_linked"), "accounts", ACCOUNTS_CHECK)
    op.create_check_constraint(op.f("ck_categories_system_kind"), "categories", SYSTEM_KIND_CHECK)
    op.execute(V_ALLOCATIONS)
    bind = op.get_bind()
    for key, category in CREDIT_MAPPINGS:
        bind.execute(
            sa.text(
                "INSERT INTO category_mappings (source, external_key, category_id) "
                "SELECT 'amex_csv', :key, c.id FROM categories c "
                "WHERE lower(c.name) = lower(:category) "
                "ON CONFLICT (source, external_key) DO NOTHING"
            ),
            {"key": key, "category": category},
        )
    _recolour(forward=True)


def downgrade() -> None:
    _recolour(forward=False)
    bind = op.get_bind()
    for key, _ in CREDIT_MAPPINGS:
        bind.execute(
            sa.text(
                "DELETE FROM category_mappings WHERE source = 'amex_csv' AND external_key = :key"
            ),
            {"key": key},
        )
    op.execute(V_ALLOCATIONS_0001)
    op.drop_constraint(op.f("ck_categories_system_kind"), "categories", type_="check")
    op.drop_constraint(op.f("ck_accounts_manual_or_linked"), "accounts", type_="check")
    op.drop_column("plaid_items", "error_display_message")
