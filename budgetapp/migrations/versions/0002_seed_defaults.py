"""Seed data: system and default categories, Plaid PFC v2 and Amex CSV mappings, app_settings.

Revision ID: 0002_seed_defaults
Revises: 0001_initial
Create Date: 2026-10-28

Idempotent: every insert is ON CONFLICT DO NOTHING, so rows the user already has (or renamed
categories) are left alone. The Plaid mappings cover every primary and detailed code of the PFC v2
taxonomy (18 primaries, 127 detailed), generated from https://plaid.com/documents/pfc-taxonomy-all.csv.
The Amex mappings cover the CSV "Category" values in RESEARCH T4.2 plus each "Group" prefix.

Once applied to production, a migration is never edited: add a new one instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_defaults"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, kind, system_key, colour, icon)
CATEGORIES: list[tuple[str, str, str | None, str, str]] = [
    ("Groceries", "expense", None, "#16A34A", "🛒"),
    ("Dining & Drinks", "expense", None, "#EA580C", "🍽️"),
    ("Shopping", "expense", None, "#9333EA", "🛍️"),
    ("Travel", "expense", None, "#2563EB", "✈️"),
    ("Transportation", "expense", None, "#0891B2", "🚕"),
    ("Entertainment", "expense", None, "#DB2777", "🎭"),
    ("Subscriptions & Streaming", "expense", None, "#7C3AED", "📺"),
    ("Health & Fitness", "expense", None, "#65A30D", "💪"),
    ("Personal Care", "expense", None, "#E11D48", "💇"),
    ("Home", "expense", None, "#A16207", "🏠"),
    ("Utilities & Bills", "expense", None, "#CA8A04", "💡"),
    ("Services", "expense", None, "#0D9488", "🧰"),
    ("Gifts & Donations", "expense", None, "#DC2626", "🎁"),
    ("Education", "expense", None, "#4F46E5", "🎓"),
    ("Fees & Interest", "expense", None, "#9F1239", "🧾"),
    ("Credits & Rewards", "income", None, "#059669", "⭐"),
    ("Other", "expense", None, "#78716C", "📦"),
    ("Uncategorized", "expense", "uncategorized", "#9CA3AF", "❔"),
    ("Card Payments", "transfer", "card_payments", "#64748B", "💳"),
]

SYSTEM_CATEGORY_KEYS = {"Uncategorized": "uncategorized", "Card Payments": "card_payments"}

# Plaid personal finance category (v2) code -> category. Primary codes are the fallback for
# detailed codes Plaid may add later. Card payments are also caught by the fixed payment rule
# (step 0 of categorization), whatever their PFC.
PLAID_PFC: list[tuple[str, str]] = [
    ("INCOME", "Credits & Rewards"),
    ("INCOME_CHILD_SUPPORT", "Credits & Rewards"),
    ("INCOME_CONTRACTOR", "Credits & Rewards"),
    ("INCOME_DIVIDENDS", "Credits & Rewards"),
    ("INCOME_GIG_ECONOMY", "Credits & Rewards"),
    ("INCOME_INTEREST_EARNED", "Credits & Rewards"),
    ("INCOME_LONG_TERM_DISABILITY", "Credits & Rewards"),
    ("INCOME_MILITARY", "Credits & Rewards"),
    ("INCOME_RENTAL", "Credits & Rewards"),
    ("INCOME_RETIREMENT_PENSION", "Credits & Rewards"),
    ("INCOME_SALARY", "Credits & Rewards"),
    ("INCOME_TAX_REFUND", "Credits & Rewards"),
    ("INCOME_UNEMPLOYMENT", "Credits & Rewards"),
    ("INCOME_OTHER", "Credits & Rewards"),
    ("LOAN_DISBURSEMENTS", "Card Payments"),
    ("LOAN_DISBURSEMENTS_AUTO", "Card Payments"),
    ("LOAN_DISBURSEMENTS_CASH_ADVANCES", "Other"),
    ("LOAN_DISBURSEMENTS_EWA", "Card Payments"),
    ("LOAN_DISBURSEMENTS_MORTGAGE", "Card Payments"),
    ("LOAN_DISBURSEMENTS_PERSONAL", "Card Payments"),
    ("LOAN_DISBURSEMENTS_STUDENT", "Card Payments"),
    ("LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT", "Card Payments"),
    ("LOAN_PAYMENTS", "Card Payments"),
    ("LOAN_PAYMENTS_BNPL", "Shopping"),
    ("LOAN_PAYMENTS_CAR_PAYMENT", "Transportation"),
    ("LOAN_PAYMENTS_CASH_ADVANCES", "Card Payments"),
    ("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "Card Payments"),
    ("LOAN_PAYMENTS_EWA", "Card Payments"),
    ("LOAN_PAYMENTS_MORTGAGE_PAYMENT", "Home"),
    ("LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENT", "Utilities & Bills"),
    ("LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", "Education"),
    ("LOAN_PAYMENTS_OTHER_PAYMENT", "Card Payments"),
    ("TRANSFER_IN", "Card Payments"),
    ("TRANSFER_IN_ACCOUNT_TRANSFER", "Card Payments"),
    ("TRANSFER_IN_DEPOSIT", "Card Payments"),
    ("TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS", "Card Payments"),
    ("TRANSFER_IN_SAVINGS", "Card Payments"),
    ("TRANSFER_IN_TRANSFER_IN_FROM_APPS", "Card Payments"),
    ("TRANSFER_IN_WIRE", "Card Payments"),
    ("TRANSFER_IN_OTHER_TRANSFER_IN", "Card Payments"),
    ("TRANSFER_OUT", "Other"),
    ("TRANSFER_OUT_ACCOUNT_TRANSFER", "Other"),
    ("TRANSFER_OUT_CRYPTO", "Other"),
    ("TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS", "Other"),
    ("TRANSFER_OUT_SAVINGS", "Other"),
    ("TRANSFER_OUT_TRANSFER_OUT_FROM_APPS", "Other"),
    ("TRANSFER_OUT_WIRE", "Other"),
    ("TRANSFER_OUT_WITHDRAWAL", "Other"),
    ("TRANSFER_OUT_OTHER_TRANSFER_OUT", "Other"),
    ("BANK_FEES", "Fees & Interest"),
    ("BANK_FEES_ATM_FEES", "Fees & Interest"),
    ("BANK_FEES_INSUFFICIENT_FUNDS", "Fees & Interest"),
    ("BANK_FEES_INTEREST_CHARGE", "Fees & Interest"),
    ("BANK_FEES_FOREIGN_TRANSACTION_FEES", "Fees & Interest"),
    ("BANK_FEES_OVERDRAFT_FEES", "Fees & Interest"),
    ("BANK_FEES_LATE_FEES", "Fees & Interest"),
    ("BANK_FEES_CASH_ADVANCE", "Fees & Interest"),
    ("BANK_FEES_OTHER_BANK_FEES", "Fees & Interest"),
    ("ENTERTAINMENT", "Entertainment"),
    ("ENTERTAINMENT_CASINOS_AND_GAMBLING", "Entertainment"),
    ("ENTERTAINMENT_MUSIC_AND_AUDIO", "Subscriptions & Streaming"),
    ("ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS", "Entertainment"),
    ("ENTERTAINMENT_TV_AND_MOVIES", "Subscriptions & Streaming"),
    ("ENTERTAINMENT_VIDEO_GAMES", "Entertainment"),
    ("ENTERTAINMENT_OTHER_ENTERTAINMENT", "Entertainment"),
    ("FOOD_AND_DRINK", "Dining & Drinks"),
    ("FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", "Dining & Drinks"),
    ("FOOD_AND_DRINK_COFFEE", "Dining & Drinks"),
    ("FOOD_AND_DRINK_FAST_FOOD", "Dining & Drinks"),
    ("FOOD_AND_DRINK_GROCERIES", "Groceries"),
    ("FOOD_AND_DRINK_RESTAURANT", "Dining & Drinks"),
    ("FOOD_AND_DRINK_VENDING_MACHINES", "Dining & Drinks"),
    ("FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", "Dining & Drinks"),
    ("GENERAL_MERCHANDISE", "Shopping"),
    ("GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS", "Shopping"),
    ("GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", "Shopping"),
    ("GENERAL_MERCHANDISE_CONVENIENCE_STORES", "Groceries"),
    ("GENERAL_MERCHANDISE_DEPARTMENT_STORES", "Shopping"),
    ("GENERAL_MERCHANDISE_DISCOUNT_STORES", "Shopping"),
    ("GENERAL_MERCHANDISE_ELECTRONICS", "Shopping"),
    ("GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES", "Gifts & Donations"),
    ("GENERAL_MERCHANDISE_OFFICE_SUPPLIES", "Shopping"),
    ("GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "Shopping"),
    ("GENERAL_MERCHANDISE_PET_SUPPLIES", "Shopping"),
    ("GENERAL_MERCHANDISE_SPORTING_GOODS", "Shopping"),
    ("GENERAL_MERCHANDISE_SUPERSTORES", "Shopping"),
    ("GENERAL_MERCHANDISE_TOBACCO_AND_VAPE", "Shopping"),
    ("GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE", "Shopping"),
    ("HOME_IMPROVEMENT", "Home"),
    ("HOME_IMPROVEMENT_FURNITURE", "Home"),
    ("HOME_IMPROVEMENT_HARDWARE", "Home"),
    ("HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE", "Home"),
    ("HOME_IMPROVEMENT_SECURITY", "Home"),
    ("HOME_IMPROVEMENT_OTHER_HOME_IMPROVEMENT", "Home"),
    ("MEDICAL", "Health & Fitness"),
    ("MEDICAL_DENTAL_CARE", "Health & Fitness"),
    ("MEDICAL_EYE_CARE", "Health & Fitness"),
    ("MEDICAL_NURSING_CARE", "Health & Fitness"),
    ("MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "Health & Fitness"),
    ("MEDICAL_PRIMARY_CARE", "Health & Fitness"),
    ("MEDICAL_VETERINARY_SERVICES", "Services"),
    ("MEDICAL_OTHER_MEDICAL", "Health & Fitness"),
    ("PERSONAL_CARE", "Personal Care"),
    ("PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "Health & Fitness"),
    ("PERSONAL_CARE_HAIR_AND_BEAUTY", "Personal Care"),
    ("PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING", "Personal Care"),
    ("PERSONAL_CARE_OTHER_PERSONAL_CARE", "Personal Care"),
    ("GENERAL_SERVICES", "Services"),
    ("GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING", "Services"),
    ("GENERAL_SERVICES_AUTOMOTIVE", "Transportation"),
    ("GENERAL_SERVICES_CHILDCARE", "Services"),
    ("GENERAL_SERVICES_CONSULTING_AND_LEGAL", "Services"),
    ("GENERAL_SERVICES_EDUCATION", "Education"),
    ("GENERAL_SERVICES_INSURANCE", "Utilities & Bills"),
    ("GENERAL_SERVICES_POSTAGE_AND_SHIPPING", "Services"),
    ("GENERAL_SERVICES_STORAGE", "Services"),
    ("GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Subscriptions & Streaming"),
    ("GOVERNMENT_AND_NON_PROFIT", "Other"),
    ("GOVERNMENT_AND_NON_PROFIT_DONATIONS", "Gifts & Donations"),
    ("GOVERNMENT_AND_NON_PROFIT_GOVERNMENT_DEPARTMENTS_AND_AGENCIES", "Services"),
    ("GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT", "Other"),
    ("GOVERNMENT_AND_NON_PROFIT_OTHER_GOVERNMENT_AND_NON_PROFIT", "Services"),
    ("TRANSPORTATION", "Transportation"),
    ("TRANSPORTATION_BIKES_AND_SCOOTERS", "Transportation"),
    ("TRANSPORTATION_GAS", "Transportation"),
    ("TRANSPORTATION_PARKING", "Transportation"),
    ("TRANSPORTATION_PUBLIC_TRANSIT", "Transportation"),
    ("TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Transportation"),
    ("TRANSPORTATION_TOLLS", "Transportation"),
    ("TRANSPORTATION_OTHER_TRANSPORTATION", "Transportation"),
    ("TRAVEL", "Travel"),
    ("TRAVEL_FLIGHTS", "Travel"),
    ("TRAVEL_LODGING", "Travel"),
    ("TRAVEL_RENTAL_CARS", "Travel"),
    ("TRAVEL_OTHER_TRAVEL", "Travel"),
    ("RENT_AND_UTILITIES", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_INTERNET_AND_CABLE", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_RENT", "Home"),
    ("RENT_AND_UTILITIES_SEWAGE_AND_WASTE_MANAGEMENT", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_TELEPHONE", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_WATER", "Utilities & Bills"),
    ("RENT_AND_UTILITIES_OTHER_UTILITIES", "Utilities & Bills"),
    ("OTHER", "Other"),
    ("OTHER_OTHER", "Other"),
]

# Amex CSV "Category" value ("Group-Subgroup", or the "Group" prefix alone) -> category.
AMEX_CSV: list[tuple[str, str]] = [
    ("Business Services", "Services"),
    ("Business Services-Mailing & Shipping", "Services"),
    ("Business Services-Office Supplies", "Shopping"),
    ("Business Services-Printing & Publishing", "Services"),
    ("Communications", "Utilities & Bills"),
    ("Communications-Cable & Internet Comm", "Utilities & Bills"),
    ("Communications-Mobile Telecom", "Utilities & Bills"),
    ("Entertainment", "Entertainment"),
    ("Fees & Adjustments", "Fees & Interest"),
    ("Fees & Adjustments-Fees & Adjustments", "Fees & Interest"),
    ("Merchandise & Supplies", "Shopping"),
    ("Merchandise & Supplies-Book Stores", "Shopping"),
    ("Merchandise & Supplies-Computer Supplies", "Shopping"),
    ("Merchandise & Supplies-Groceries", "Groceries"),
    ("Merchandise & Supplies-Internet Purchase", "Shopping"),
    ("Merchandise & Supplies-Mail Order", "Shopping"),
    ("Merchandise & Supplies-Pharmacies", "Health & Fitness"),
    ("Other", "Other"),
    ("Other-Miscellaneous", "Other"),
    ("Restaurant", "Dining & Drinks"),
    ("Restaurant-Bar & Café", "Dining & Drinks"),
    ("Restaurant-Restaurant", "Dining & Drinks"),
    ("Transportation", "Transportation"),
    ("Transportation-Fuel", "Transportation"),
    ("Transportation-Rail Services", "Transportation"),
    ("Transportation-Taxis & Coach", "Transportation"),
    ("Travel", "Travel"),
    ("Travel-Airline", "Travel"),
    ("Travel-Lodging", "Travel"),
]

_INSERT_CATEGORY = sa.text(
    "INSERT INTO categories (name, kind, system_key, color, icon, sort_order) "
    "VALUES (:name, :kind, :system_key, :color, :icon, :sort_order) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_MAPPING_BY_NAME = sa.text(
    "INSERT INTO category_mappings (source, external_key, category_id) "
    "SELECT :source, :key, c.id FROM categories c WHERE lower(c.name) = lower(:category) "
    "ON CONFLICT (source, external_key) DO NOTHING"
)
_INSERT_MAPPING_BY_SYSTEM_KEY = sa.text(
    "INSERT INTO category_mappings (source, external_key, category_id) "
    "SELECT :source, :key, c.id FROM categories c WHERE c.system_key = :system_key "
    "ON CONFLICT (source, external_key) DO NOTHING"
)


def _mapping_rows() -> list[tuple[str, str, str]]:
    return [("plaid_pfc", key, cat) for key, cat in PLAID_PFC] + [
        ("amex_csv", key, cat) for key, cat in AMEX_CSV
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for order, (name, kind, system_key, color, icon) in enumerate(CATEGORIES, start=1):
        bind.execute(
            _INSERT_CATEGORY,
            {
                "name": name,
                "kind": kind,
                "system_key": system_key,
                "color": color,
                "icon": icon,
                "sort_order": order * 10,
            },
        )
    for source, key, category in _mapping_rows():
        system_key = SYSTEM_CATEGORY_KEYS.get(category)
        if system_key:
            bind.execute(
                _INSERT_MAPPING_BY_SYSTEM_KEY,
                {"source": source, "key": key, "system_key": system_key},
            )
        else:
            bind.execute(
                _INSERT_MAPPING_BY_NAME, {"source": source, "key": key, "category": category}
            )
    bind.execute(sa.text("INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))


def downgrade() -> None:
    bind = op.get_bind()
    for source, key, _ in _mapping_rows():
        bind.execute(
            sa.text("DELETE FROM category_mappings WHERE source = :source AND external_key = :key"),
            {"source": source, "key": key},
        )
    bind.execute(sa.text("DELETE FROM app_settings WHERE id = 1"))
    # Seeded categories go only when nothing references them any more.
    for name, *_ in CATEGORIES:
        bind.execute(
            sa.text(
                "DELETE FROM categories c WHERE lower(c.name) = lower(:name) "
                "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM rules r WHERE r.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM budgets b WHERE b.category_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM category_mappings m WHERE m.category_id = c.id)"
            ),
            {"name": name},
        )
