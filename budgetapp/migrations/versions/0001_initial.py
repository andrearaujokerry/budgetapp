"""Initial schema: every table of ARCHITECTURE §5 plus the v_allocations view.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-28

Once applied to production, a migration is never edited: add a new one instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The single source of truth for money (ARCHITECTURE §5): one row per (transaction, category)
# allocation. Splits are used only when they sum exactly to the transaction's amount; otherwise the
# whole amount goes to the transaction's category. Suspected duplicates are excluded.
V_ALLOCATIONS = """
CREATE VIEW v_allocations AS
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


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="expense", nullable=False),
        sa.Column("system_key", sa.Text(), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('expense', 'income', 'transfer')", name=op.f("ck_categories_kind")
        ),
        sa.CheckConstraint(
            "system_key IS NULL OR system_key IN ('uncategorized', 'card_payments')",
            name=op.f("ck_categories_system_key"),
        ),
        sa.CheckConstraint(
            "system_key IS NULL OR NOT is_hidden", name=op.f("ck_categories_system_not_hidden")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("system_key", name=op.f("uq_categories_system_key")),
    )
    op.create_index(
        "uq_categories_lower_name", "categories", [sa.literal_column("lower(name)")], unique=True
    )
    op.create_table(
        "plaid_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plaid_item_id", sa.Text(), nullable=False),
        sa.Column("institution_id", sa.Text(), nullable=True),
        sa.Column("institution_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="healthy", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("transactions_cursor", sa.Text(), nullable=True),
        sa.Column("transactions_update_status", sa.Text(), nullable=True),
        sa.Column("history_complete_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("days_requested", sa.Integer(), server_default=sa.text("730"), nullable=False),
        sa.Column(
            "consented_products",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("liabilities_status", sa.Text(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plaid_last_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plaid_last_failed_update_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("item_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "liabilities_status IS NULL OR "
            "liabilities_status IN ('ok', 'not_consented', 'unsupported', 'error')",
            name=op.f("ck_plaid_items_liabilities_status"),
        ),
        sa.CheckConstraint(
            "status IN ('healthy', 'needs_reconnect', 'error')", name=op.f("ck_plaid_items_status")
        ),
        sa.CheckConstraint(
            "transactions_cursor IS NULL OR transactions_cursor <> ''",
            name=op.f("ck_plaid_items_cursor_not_empty"),
        ),
        sa.CheckConstraint(
            "days_requested BETWEEN 1 AND 730", name=op.f("ck_plaid_items_days_requested")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plaid_items")),
        sa.UniqueConstraint("plaid_item_id", name=op.f("uq_plaid_items_plaid_item_id")),
    )
    op.create_table(
        "recurring_overrides",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("merchant_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ignored', 'confirmed')", name=op.f("ck_recurring_overrides_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_overrides")),
        sa.UniqueConstraint("merchant_key", name=op.f("uq_recurring_overrides_merchant_key")),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("plaid_account_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("official_name", sa.Text(), nullable=True),
        sa.Column("mask", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("subtype", sa.Text(), nullable=True),
        sa.Column("iso_currency_code", sa.Text(), server_default="USD", nullable=True),
        sa.Column("current_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("available_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("credit_limit", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("balances_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("statement_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("minimum_payment", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("last_payment_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("statement_issue_date", sa.Date(), nullable=True),
        sa.Column("payment_due_date", sa.Date(), nullable=True),
        sa.Column("last_payment_date", sa.Date(), nullable=True),
        sa.Column("is_overdue", sa.Boolean(), nullable=True),
        sa.Column("liabilities_as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("statement_closing_day", sa.SmallInteger(), nullable=True),
        sa.Column("plaid_history_start", sa.Date(), nullable=True),
        sa.Column("notice", sa.Text(), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "statement_closing_day IS NULL OR statement_closing_day BETWEEN 1 AND 31",
            name=op.f("ck_accounts_statement_closing_day"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["plaid_items.id"],
            name=op.f("fk_accounts_item_id_plaid_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("plaid_account_id", name=op.f("uq_accounts_plaid_account_id")),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("period", sa.Text(), server_default="monthly", nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("rollover", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(scope = 'overall') = (category_id IS NULL)", name=op.f("ck_budgets_scope_category")
        ),
        sa.CheckConstraint(
            "NOT rollover OR period = 'monthly'", name=op.f("ck_budgets_rollover_monthly")
        ),
        sa.CheckConstraint("period IN ('monthly', 'annual')", name=op.f("ck_budgets_period")),
        sa.CheckConstraint("scope IN ('category', 'overall')", name=op.f("ck_budgets_scope")),
        sa.CheckConstraint(
            "amount IS NULL OR amount >= 0", name=op.f("ck_budgets_amount_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_budgets_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
        sa.UniqueConstraint(
            "category_id",
            "period",
            "effective_from",
            name=op.f("uq_budgets_category_id_period_effective_from"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "category_mappings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('plaid_pfc', 'amex_csv')", name=op.f("ck_category_mappings_source")
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_category_mappings_category_id_categories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category_mappings")),
        sa.UniqueConstraint(
            "source", "external_key", name=op.f("uq_category_mappings_source_external_key")
        ),
    )
    op.create_index(
        op.f("ix_category_mappings_category_id"), "category_mappings", ["category_id"], unique=False
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("modified_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("removed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("update_status", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'error', 'skipped', 'interrupted')",
            name=op.f("ck_sync_runs_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('schedule', 'manual', 'link', 'update', 'cli')",
            name=op.f("ck_sync_runs_trigger"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["plaid_items.id"],
            name=op.f("fk_sync_runs_item_id_plaid_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "ix_sync_runs_item_id_started_at", "sync_runs", ["item_id", "started_at"], unique=False
    )
    op.create_table(
        "app_settings",
        sa.Column(
            "id",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("budget_period_mode", sa.Text(), server_default="calendar", nullable=False),
        sa.Column("budget_cycle_account_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "include_pending_in_budgets",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "budget_alerts_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "alert_thresholds",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{80,100}'"),
            nullable=False,
        ),
        sa.Column(
            "sync_alerts_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("large_charge_threshold", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "consent_reminder_days", sa.Integer(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column(
            "statement_settle_days", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column("ai_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "budget_period_mode IN ('calendar', 'statement')",
            name=op.f("ck_app_settings_budget_period_mode"),
        ),
        sa.CheckConstraint(
            "consent_reminder_days >= 0", name=op.f("ck_app_settings_consent_reminder_days")
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_app_settings_single_row")),
        sa.CheckConstraint(
            "large_charge_threshold IS NULL OR large_charge_threshold > 0",
            name=op.f("ck_app_settings_large_charge_threshold"),
        ),
        sa.CheckConstraint(
            "statement_settle_days >= 0", name=op.f("ck_app_settings_statement_settle_days")
        ),
        sa.ForeignKeyConstraint(
            ["budget_cycle_account_id"],
            ["accounts.id"],
            name=op.f("fk_app_settings_budget_cycle_account_id_accounts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "csv_imports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("inserted_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("matched_plaid_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("date_min", sa.Date(), nullable=True),
        sa.Column("date_max", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_csv_imports_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_csv_imports")),
    )
    op.create_table(
        "rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("merchant_pattern", sa.Text(), nullable=True),
        sa.Column("description_pattern", sa.Text(), nullable=True),
        sa.Column("match_type", sa.Text(), server_default="contains", nullable=False),
        sa.Column("amount_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("amount_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "description_pattern IS NULL OR description_pattern <> ''",
            name=op.f("ck_rules_description_pattern_not_empty"),
        ),
        sa.CheckConstraint(
            "match_type IN ('contains', 'equals', 'starts_with', 'regex')",
            name=op.f("ck_rules_match_type"),
        ),
        sa.CheckConstraint(
            "merchant_pattern IS NULL OR merchant_pattern <> ''",
            name=op.f("ck_rules_merchant_pattern_not_empty"),
        ),
        sa.CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max",
            name=op.f("ck_rules_amount_range"),
        ),
        sa.CheckConstraint(
            "merchant_pattern IS NOT NULL OR description_pattern IS NOT NULL "
            "OR amount_min IS NOT NULL OR amount_max IS NOT NULL OR account_id IS NOT NULL",
            name=op.f("ck_rules_has_condition"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_rules_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_rules_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
    )
    op.create_index("ix_rules_priority_id", "rules", ["priority", "id"], unique=False)
    op.create_table(
        "statements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("closing_date", sa.Date(), nullable=False),
        sa.Column("closing_date_source", sa.Text(), nullable=False),
        sa.Column("plaid_issue_date", sa.Date(), nullable=True),
        sa.Column("period_start_override", sa.Date(), nullable=True),
        sa.Column("reported_new_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("reported_new_balance_source", sa.Text(), nullable=True),
        sa.Column("reported_previous_balance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("reported_payments_credits", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("reported_new_charges", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("reported_fees", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("reported_interest", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "closing_date_source IN ('liabilities', 'user')",
            name=op.f("ck_statements_closing_date_source"),
        ),
        sa.CheckConstraint(
            "reported_new_balance_source IS NULL OR "
            "reported_new_balance_source IN ('liabilities', 'user')",
            name=op.f("ck_statements_reported_new_balance_source"),
        ),
        sa.CheckConstraint(
            "period_start_override IS NULL OR period_start_override <= closing_date",
            name=op.f("ck_statements_period_start_override"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_statements_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statements")),
        sa.UniqueConstraint(
            "account_id", "closing_date", name=op.f("uq_statements_account_id_closing_date")
        ),
        sa.UniqueConstraint(
            "account_id", "plaid_issue_date", name=op.f("uq_statements_account_id_plaid_issue_date")
        ),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("plaid_transaction_id", sa.Text(), nullable=True),
        sa.Column("pending_transaction_id", sa.Text(), nullable=True),
        sa.Column("pending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("authorized_date", sa.Date(), nullable=True),
        sa.Column("posted_date", sa.Date(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("merchant_name", sa.Text(), nullable=True),
        sa.Column("original_description", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("iso_currency_code", sa.Text(), nullable=True),
        sa.Column("pfc_primary", sa.Text(), nullable=True),
        sa.Column("pfc_detailed", sa.Text(), nullable=True),
        sa.Column("pfc_confidence", sa.Text(), nullable=True),
        sa.Column("pfc_version", sa.Text(), nullable=True),
        sa.Column("transaction_code", sa.Text(), nullable=True),
        sa.Column("merchant_category_code", sa.Text(), nullable=True),
        sa.Column("merchant_entity_id", sa.Text(), nullable=True),
        sa.Column("external_category", sa.Text(), nullable=True),
        sa.Column("payment_channel", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("account_owner", sa.Text(), nullable=True),
        sa.Column("csv_reference", sa.Text(), nullable=True),
        sa.Column("csv_import_id", sa.BigInteger(), nullable=True),
        sa.Column("duplicate_status", sa.Text(), server_default="none", nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("category_source", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("user_edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category_source IN ('user', 'system', 'rule', 'plaid', 'amex', 'default')",
            name=op.f("ck_transactions_category_source"),
        ),
        sa.CheckConstraint(
            "duplicate_status IN ('none', 'suspect', 'kept')",
            name=op.f("ck_transactions_duplicate_status"),
        ),
        sa.CheckConstraint(
            "source <> 'csv' OR csv_reference IS NOT NULL",
            name=op.f("ck_transactions_csv_reference_required"),
        ),
        sa.CheckConstraint(
            "source <> 'plaid' OR plaid_transaction_id IS NOT NULL",
            name=op.f("ck_transactions_plaid_id_required"),
        ),
        sa.CheckConstraint("source IN ('plaid', 'csv')", name=op.f("ck_transactions_source")),
        sa.CheckConstraint(
            "NOT pending OR posted_date IS NULL", name=op.f("ck_transactions_pending_not_posted")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_transactions_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_transactions_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["csv_import_id"],
            ["csv_imports.id"],
            name=op.f("fk_transactions_csv_import_id_csv_imports"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rules.id"],
            name=op.f("fk_transactions_rule_id_rules"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
        sa.UniqueConstraint("csv_reference", name=op.f("uq_transactions_csv_reference")),
        sa.UniqueConstraint(
            "plaid_transaction_id", name=op.f("uq_transactions_plaid_transaction_id")
        ),
    )
    op.create_index(
        "ix_transactions_account_id_date", "transactions", ["account_id", "date"], unique=False
    )
    op.create_index(
        "ix_transactions_authorized_date", "transactions", ["authorized_date"], unique=False
    )
    op.create_index(
        "ix_transactions_category_id_date", "transactions", ["category_id", "date"], unique=False
    )
    op.create_index("ix_transactions_date", "transactions", ["date"], unique=False)
    op.create_index(
        "ix_transactions_merchant_name", "transactions", ["merchant_name"], unique=False
    )
    op.create_index(
        "ix_transactions_pending_transaction_id",
        "transactions",
        ["pending_transaction_id"],
        unique=False,
    )
    op.create_index("ix_transactions_posted_date", "transactions", ["posted_date"], unique=False)
    op.create_index(
        "ix_transactions_tags", "transactions", ["tags"], unique=False, postgresql_using="gin"
    )
    # rules <-> transactions reference each other: add this FK once both tables exist.
    op.create_foreign_key(
        op.f("fk_rules_source_transaction_id_transactions"),
        "rules",
        "transactions",
        ["source_transaction_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "alert_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("transaction_id", sa.BigInteger(), nullable=True),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_alert_events_category_id_categories"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["plaid_items.id"],
            name=op.f("fk_alert_events_item_id_plaid_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_alert_events_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_alert_events_dedupe_key")),
    )
    op.create_table(
        "transaction_splits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("transaction_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name=op.f("fk_transaction_splits_category_id_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_splits_transaction_id_transactions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transaction_splits")),
    )
    op.create_index(
        op.f("ix_transaction_splits_transaction_id"),
        "transaction_splits",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "transaction_tombstones",
        sa.Column("plaid_transaction_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("authorized_date", sa.Date(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("merchant_name", sa.Text(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("user_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "removed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consumed_by_transaction_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_transaction_tombstones_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_by_transaction_id"],
            ["transactions.id"],
            name=op.f("fk_transaction_tombstones_consumed_by_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("plaid_transaction_id", name=op.f("pk_transaction_tombstones")),
    )
    op.create_index(
        "ix_transaction_tombstones_account_id",
        "transaction_tombstones",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_transaction_tombstones_removed_at",
        "transaction_tombstones",
        ["removed_at"],
        unique=False,
    )
    op.execute(V_ALLOCATIONS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_allocations")
    op.drop_constraint(
        op.f("fk_rules_source_transaction_id_transactions"), "rules", type_="foreignkey"
    )
    op.drop_index("ix_transaction_tombstones_removed_at", table_name="transaction_tombstones")
    op.drop_index("ix_transaction_tombstones_account_id", table_name="transaction_tombstones")
    op.drop_table("transaction_tombstones")
    op.drop_index(op.f("ix_transaction_splits_transaction_id"), table_name="transaction_splits")
    op.drop_table("transaction_splits")
    op.drop_table("alert_events")
    op.drop_index("ix_transactions_tags", table_name="transactions", postgresql_using="gin")
    op.drop_index("ix_transactions_posted_date", table_name="transactions")
    op.drop_index("ix_transactions_pending_transaction_id", table_name="transactions")
    op.drop_index("ix_transactions_merchant_name", table_name="transactions")
    op.drop_index("ix_transactions_date", table_name="transactions")
    op.drop_index("ix_transactions_category_id_date", table_name="transactions")
    op.drop_index("ix_transactions_authorized_date", table_name="transactions")
    op.drop_index("ix_transactions_account_id_date", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("statements")
    op.drop_index("ix_rules_priority_id", table_name="rules")
    op.drop_table("rules")
    op.drop_table("csv_imports")
    op.drop_table("app_settings")
    op.drop_index("ix_sync_runs_item_id_started_at", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index(op.f("ix_category_mappings_category_id"), table_name="category_mappings")
    op.drop_table("category_mappings")
    op.drop_table("budgets")
    op.drop_table("accounts")
    op.drop_table("recurring_overrides")
    op.drop_table("plaid_items")
    op.drop_index("uq_categories_lower_name", table_name="categories")
    op.drop_table("categories")
