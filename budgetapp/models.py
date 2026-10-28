"""SQLAlchemy models: every table of ARCHITECTURE §5.

Conventions: ``id BIGINT GENERATED ALWAYS AS IDENTITY``; ``created_at``/``updated_at`` timestamptz
default ``now()``; money ``NUMERIC(14,2)`` (``Decimal``); enumerations are ``TEXT`` + ``CHECK``;
constraint names follow :data:`NAMING_CONVENTION`; every foreign key states its ``ON DELETE``.

Sign convention: positive amount = charge, fee or interest; negative = credit (refund, payment).

The schema itself is created by Alembic migrations (``budgetapp/migrations``); a test checks that
these models and the migrations agree. ``v_allocations`` (the single source of truth for money) is
a view, mapped on the separate :data:`view_metadata` for querying only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(14, 2)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        Decimal: Money,
        dt.datetime: DateTime(timezone=True),
        dt.date: Date,
        str: Text,
        int: BigInteger,
        bool: Boolean,
        dict[str, Any]: JSONB,
    }


class IdMixin:
    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True, sort_order=-100
    )


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), sort_order=100)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), sort_order=101
    )


def _check_in(
    column: str, values: tuple[str, ...], name: str, *, nullable: bool = False
) -> CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    condition = f"{column} IN ({listed})"
    if nullable:
        condition = f"{column} IS NULL OR {condition}"
    return CheckConstraint(condition, name=name)


# --------------------------------------------------------------------------- enumerations

ITEM_STATUSES = ("healthy", "needs_reconnect", "error")
LIABILITIES_STATUSES = ("ok", "not_consented", "unsupported", "error")
CATEGORY_KINDS = ("expense", "income", "transfer")
SYSTEM_KEYS = ("uncategorized", "card_payments")
MAPPING_SOURCES = ("plaid_pfc", "amex_csv")
MATCH_TYPES = ("contains", "equals", "starts_with", "regex")
TRANSACTION_SOURCES = ("plaid", "csv")
DUPLICATE_STATUSES = ("none", "suspect", "kept")
CATEGORY_SOURCES = ("user", "system", "rule", "plaid", "amex", "default")
BUDGET_SCOPES = ("category", "overall")
BUDGET_PERIODS = ("monthly", "annual")
CLOSING_DATE_SOURCES = ("liabilities", "user")
PERIOD_MODES = ("calendar", "statement")
SYNC_TRIGGERS = ("schedule", "manual", "link", "update", "cli")
SYNC_STATUSES = ("running", "success", "error", "skipped", "interrupted")
RECURRING_STATUSES = ("ignored", "confirmed")


# --------------------------------------------------------------------------- Plaid items & accounts


class PlaidItem(IdMixin, TimestampMixin, Base):
    """One Plaid Item (a linked login). v1 allows exactly one in production."""

    __tablename__ = "plaid_items"
    __table_args__ = (
        _check_in("status", ITEM_STATUSES, "status"),
        _check_in("liabilities_status", LIABILITIES_STATUSES, "liabilities_status", nullable=True),
        CheckConstraint(
            "transactions_cursor IS NULL OR transactions_cursor <> ''", name="cursor_not_empty"
        ),
        CheckConstraint("days_requested BETWEEN 1 AND 730", name="days_requested"),
    )

    plaid_item_id: Mapped[str] = mapped_column(unique=True)
    institution_id: Mapped[str | None]
    institution_name: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="healthy", server_default="healthy")
    error_code: Mapped[str | None]
    error_message: Mapped[str | None]
    #: Saved /transactions/sync cursor; NULL means "start from scratch" (never store '').
    transactions_cursor: Mapped[str | None]
    transactions_update_status: Mapped[str | None]
    history_complete_at: Mapped[dt.datetime | None]
    days_requested: Mapped[int] = mapped_column(Integer, default=730, server_default=text("730"))
    consented_products: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(ARRAY(Text)), default=list, server_default=text("'{}'")
    )
    liabilities_status: Mapped[str | None]
    linked_at: Mapped[dt.datetime]
    last_consented_at: Mapped[dt.datetime]
    consent_expires_at: Mapped[dt.datetime | None]
    plaid_last_update_at: Mapped[dt.datetime | None]
    plaid_last_failed_update_at: Mapped[dt.datetime | None]
    last_sync_attempt_at: Mapped[dt.datetime | None]
    last_sync_success_at: Mapped[dt.datetime | None]
    item_snapshot: Mapped[dict[str, Any] | None]

    accounts: Mapped[list[Account]] = relationship(back_populates="item")


class Account(IdMixin, TimestampMixin, Base):
    """A card account: from Plaid (``item_id`` set) or manual, for CSV-only history (NULL)."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "statement_closing_day IS NULL OR statement_closing_day BETWEEN 1 AND 31",
            name="statement_closing_day",
        ),
    )

    item_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_items.id", ondelete="RESTRICT"))
    plaid_account_id: Mapped[str | None] = mapped_column(unique=True)
    name: Mapped[str | None]
    official_name: Mapped[str | None]
    mask: Mapped[str | None]
    type: Mapped[str | None]
    subtype: Mapped[str | None]
    iso_currency_code: Mapped[str | None] = mapped_column(default="USD", server_default="USD")
    current_balance: Mapped[Decimal | None]
    available_balance: Mapped[Decimal | None]
    credit_limit: Mapped[Decimal | None]
    balances_as_of: Mapped[dt.datetime | None]
    statement_balance: Mapped[Decimal | None]
    minimum_payment: Mapped[Decimal | None]
    last_payment_amount: Mapped[Decimal | None]
    statement_issue_date: Mapped[dt.date | None]
    payment_due_date: Mapped[dt.date | None]
    last_payment_date: Mapped[dt.date | None]
    is_overdue: Mapped[bool | None]
    liabilities_as_of: Mapped[dt.datetime | None]
    statement_closing_day: Mapped[int | None] = mapped_column(SmallInteger)
    plaid_history_start: Mapped[dt.date | None]
    notice: Mapped[str | None]
    is_hidden: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    item: Mapped[PlaidItem | None] = relationship(back_populates="accounts")

    @property
    def is_manual(self) -> bool:
        return self.item_id is None

    @property
    def display_name(self) -> str:
        base = self.name or self.official_name or "Account"
        return f"{base} ··{self.mask}" if self.mask else base


# ------------------------------------------------------------------ categories, mappings, rules


class Category(IdMixin, TimestampMixin, Base):
    """A spending category. ``kind``: expense (spending), income (credits), transfer (payments)."""

    __tablename__ = "categories"
    __table_args__ = (
        _check_in("kind", CATEGORY_KINDS, "kind"),
        _check_in("system_key", SYSTEM_KEYS, "system_key", nullable=True),
        CheckConstraint("system_key IS NULL OR NOT is_hidden", name="system_not_hidden"),
    )

    name: Mapped[str]
    kind: Mapped[str] = mapped_column(default="expense", server_default="expense")
    system_key: Mapped[str | None] = mapped_column(unique=True)
    is_hidden: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    color: Mapped[str | None]
    icon: Mapped[str | None]
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


Index("uq_categories_lower_name", func.lower(Category.name), unique=True)


class CategoryMapping(IdMixin, TimestampMixin, Base):
    """``(source, external_key) → category`` for Plaid PFC v2 codes and Amex CSV categories."""

    __tablename__ = "category_mappings"
    __table_args__ = (
        UniqueConstraint("source", "external_key"),
        _check_in("source", MAPPING_SOURCES, "source"),
    )

    source: Mapped[str]
    external_key: Mapped[str]
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )

    category: Mapped[Category] = relationship()


class Rule(IdMixin, TimestampMixin, Base):
    """A categorization rule (CAT-3). All set conditions must match; lower priority runs first."""

    __tablename__ = "rules"
    __table_args__ = (
        _check_in("match_type", MATCH_TYPES, "match_type"),
        CheckConstraint(
            "merchant_pattern IS NOT NULL OR description_pattern IS NOT NULL "
            "OR amount_min IS NOT NULL OR amount_max IS NOT NULL OR account_id IS NOT NULL",
            name="has_condition",
        ),
        CheckConstraint(
            "amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max",
            name="amount_range",
        ),
        CheckConstraint(
            "merchant_pattern IS NULL OR merchant_pattern <> ''", name="merchant_pattern_not_empty"
        ),
        CheckConstraint(
            "description_pattern IS NULL OR description_pattern <> ''",
            name="description_pattern_not_empty",
        ),
        Index("ix_rules_priority_id", "priority", "id"),
    )

    name: Mapped[str | None]
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    merchant_pattern: Mapped[str | None]
    description_pattern: Mapped[str | None]
    match_type: Mapped[str] = mapped_column(default="contains", server_default="contains")
    amount_min: Mapped[Decimal | None]
    amount_max: Mapped[Decimal | None]
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    is_system: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    source_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL", use_alter=True)
    )

    category: Mapped[Category] = relationship()
    account: Mapped[Account | None] = relationship()


# --------------------------------------------------------------------------- transactions


class CsvImport(IdMixin, TimestampMixin, Base):
    """One imported Amex CSV file (SYNC-8)."""

    __tablename__ = "csv_imports"

    filename: Mapped[str]
    file_sha256: Mapped[str]
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    imported_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    matched_plaid_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    date_min: Mapped[dt.date | None]
    date_max: Mapped[dt.date | None]

    account: Mapped[Account] = relationship()


class Transaction(IdMixin, TimestampMixin, Base):
    """A card transaction from Plaid or an Amex CSV file.

    Plaid-owned fields are overwritten on every add/modify. User-owned state (see
    ``services/userstate.py``): the category when ``category_source = 'user'``, ``notes``,
    ``tags`` and splits.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        _check_in("source", TRANSACTION_SOURCES, "source"),
        CheckConstraint(
            "source <> 'csv' OR csv_reference IS NOT NULL", name="csv_reference_required"
        ),
        CheckConstraint(
            "source <> 'plaid' OR plaid_transaction_id IS NOT NULL", name="plaid_id_required"
        ),
        _check_in("duplicate_status", DUPLICATE_STATUSES, "duplicate_status"),
        _check_in("category_source", CATEGORY_SOURCES, "category_source"),
        CheckConstraint("NOT pending OR posted_date IS NULL", name="pending_not_posted"),
        Index("ix_transactions_date", "date"),
        Index("ix_transactions_posted_date", "posted_date"),
        Index("ix_transactions_account_id_date", "account_id", "date"),
        Index("ix_transactions_category_id_date", "category_id", "date"),
        Index("ix_transactions_authorized_date", "authorized_date"),
        Index("ix_transactions_merchant_name", "merchant_name"),
        Index("ix_transactions_tags", "tags", postgresql_using="gin"),
        Index("ix_transactions_pending_transaction_id", "pending_transaction_id"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    source: Mapped[str]
    plaid_transaction_id: Mapped[str | None] = mapped_column(unique=True)
    pending_transaction_id: Mapped[str | None]
    pending: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    date: Mapped[dt.date]
    authorized_date: Mapped[dt.date | None]
    posted_date: Mapped[dt.date | None]
    name: Mapped[str]
    merchant_name: Mapped[str | None]
    original_description: Mapped[str | None]
    amount: Mapped[Decimal]
    iso_currency_code: Mapped[str | None]
    pfc_primary: Mapped[str | None]
    pfc_detailed: Mapped[str | None]
    pfc_confidence: Mapped[str | None]
    pfc_version: Mapped[str | None]
    transaction_code: Mapped[str | None]
    merchant_category_code: Mapped[str | None]
    merchant_entity_id: Mapped[str | None]
    external_category: Mapped[str | None]
    payment_channel: Mapped[str | None]
    logo_url: Mapped[str | None]
    website: Mapped[str | None]
    location: Mapped[dict[str, Any] | None]
    account_owner: Mapped[str | None]
    #: Amex ``Reference`` (apostrophes stripped); globally unique where not null.
    csv_reference: Mapped[str | None] = mapped_column(unique=True)
    csv_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("csv_imports.id", ondelete="SET NULL")
    )
    duplicate_status: Mapped[str] = mapped_column(default="none", server_default="none")
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    category_source: Mapped[str]
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"))
    notes: Mapped[str | None]
    tags: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(ARRAY(Text)), default=list, server_default=text("'{}'")
    )
    user_edited_at: Mapped[dt.datetime | None]
    needs_review: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    review_reason: Mapped[str | None]

    account: Mapped[Account] = relationship()
    category: Mapped[Category] = relationship()
    rule: Mapped[Rule | None] = relationship(foreign_keys=[rule_id])
    csv_import: Mapped[CsvImport | None] = relationship()
    splits: Mapped[list[TransactionSplit]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TransactionSplit.id",
    )

    @property
    def spend_date(self) -> dt.date:
        """``COALESCE(authorized_date, date)``: the date shown, and the calendar-mode basis."""
        return self.authorized_date or self.date


class TransactionSplit(IdMixin, TimestampMixin, Base):
    """One part of a split transaction (TXN-4). Splits count only when they sum to the amount."""

    __tablename__ = "transaction_splits"

    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal]
    note: Mapped[str | None]

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category] = relationship()


class TransactionTombstone(TimestampMixin, Base):
    """A removed Plaid transaction: blocks stale resurrection and carries user edits forward."""

    __tablename__ = "transaction_tombstones"
    __table_args__ = (
        Index("ix_transaction_tombstones_removed_at", "removed_at"),
        Index("ix_transaction_tombstones_account_id", "account_id"),
    )

    plaid_transaction_id: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    amount: Mapped[Decimal]
    date: Mapped[dt.date]
    authorized_date: Mapped[dt.date | None]
    name: Mapped[str | None]
    merchant_name: Mapped[str | None]
    pending: Mapped[bool]
    #: ``userstate.to_json()`` of the removed row, NULL when it had no user edits.
    user_state: Mapped[dict[str, Any] | None]
    removed_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    consumed_by_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )


# --------------------------------------------------------------------------- budgets & statements


class Budget(IdMixin, TimestampMixin, Base):
    """A versioned budget amount (BUD-1/3/4/5): the row with the latest ``effective_from`` wins."""

    __tablename__ = "budgets"
    __table_args__ = (
        _check_in("scope", BUDGET_SCOPES, "scope"),
        CheckConstraint("(scope = 'overall') = (category_id IS NULL)", name="scope_category"),
        _check_in("period", BUDGET_PERIODS, "period"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount_not_negative"),
        CheckConstraint("NOT rollover OR period = 'monthly'", name="rollover_monthly"),
        UniqueConstraint(
            "category_id", "period", "effective_from", postgresql_nulls_not_distinct=True
        ),
    )

    scope: Mapped[str]
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT")
    )
    period: Mapped[str] = mapped_column(default="monthly", server_default="monthly")
    #: NULL means "no budget from this date on".
    amount: Mapped[Decimal | None]
    rollover: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    effective_from: Mapped[dt.date]

    category: Mapped[Category | None] = relationship()


class Statement(IdMixin, TimestampMixin, Base):
    """A card statement (INS-4, REL-3); a period starts the day after the previous closing date."""

    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint("account_id", "closing_date"),
        UniqueConstraint("account_id", "plaid_issue_date"),
        _check_in("closing_date_source", CLOSING_DATE_SOURCES, "closing_date_source"),
        _check_in(
            "reported_new_balance_source",
            CLOSING_DATE_SOURCES,
            "reported_new_balance_source",
            nullable=True,
        ),
        CheckConstraint(
            "period_start_override IS NULL OR period_start_override <= closing_date",
            name="period_start_override",
        ),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    closing_date: Mapped[dt.date]
    closing_date_source: Mapped[str]
    plaid_issue_date: Mapped[dt.date | None]
    period_start_override: Mapped[dt.date | None]
    reported_new_balance: Mapped[Decimal | None]
    reported_new_balance_source: Mapped[str | None]
    reported_previous_balance: Mapped[Decimal | None]
    reported_payments_credits: Mapped[Decimal | None]
    reported_new_charges: Mapped[Decimal | None]
    reported_fees: Mapped[Decimal | None]
    reported_interest: Mapped[Decimal | None]
    checked_at: Mapped[dt.datetime | None]
    notes: Mapped[str | None]

    account: Mapped[Account] = relationship()


# --------------------------------------------------------------------------- other tables


class AppSettings(TimestampMixin, Base):
    """The single settings row (``id = 1``)."""

    __tablename__ = "app_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        _check_in("budget_period_mode", PERIOD_MODES, "budget_period_mode"),
        CheckConstraint(
            "large_charge_threshold IS NULL OR large_charge_threshold > 0",
            name="large_charge_threshold",
        ),
        CheckConstraint("consent_reminder_days >= 0", name="consent_reminder_days"),
        CheckConstraint("statement_settle_days >= 0", name="statement_settle_days"),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False, default=1, server_default=text("1")
    )
    budget_period_mode: Mapped[str] = mapped_column(default="calendar", server_default="calendar")
    budget_cycle_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL")
    )
    include_pending_in_budgets: Mapped[bool] = mapped_column(
        default=True, server_default=text("true")
    )
    budget_alerts_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    alert_thresholds: Mapped[list[int]] = mapped_column(
        MutableList.as_mutable(ARRAY(Integer)),
        default=lambda: [80, 100],
        server_default=text("'{80,100}'"),
    )
    sync_alerts_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    large_charge_threshold: Mapped[Decimal | None]
    consent_reminder_days: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30")
    )
    statement_settle_days: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    ai_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    budget_cycle_account: Mapped[Account | None] = relationship()


class SyncRun(IdMixin, TimestampMixin, Base):
    """One run of the sync engine for an Item (SYNC-5)."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        _check_in("trigger", SYNC_TRIGGERS, "trigger"),
        _check_in("status", SYNC_STATUSES, "status"),
        Index("ix_sync_runs_item_id_started_at", "item_id", "started_at"),
    )

    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id", ondelete="CASCADE"))
    trigger: Mapped[str]
    status: Mapped[str]
    started_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[dt.datetime | None]
    added_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    modified_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    removed_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    update_status: Mapped[str | None]
    error_code: Mapped[str | None]
    error_message: Mapped[str | None]

    item: Mapped[PlaidItem] = relationship()


class AlertEvent(IdMixin, TimestampMixin, Base):
    """A recorded alert; ``dedupe_key`` makes each alert fire once (ALR-1..3, SYNC-6)."""

    __tablename__ = "alert_events"

    kind: Mapped[str]
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]
    message: Mapped[str]
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL")
    )
    item_id: Mapped[int | None] = mapped_column(ForeignKey("plaid_items.id", ondelete="SET NULL"))
    delivered: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    delivered_at: Mapped[dt.datetime | None]
    error: Mapped[str | None]


class RecurringOverride(IdMixin, TimestampMixin, Base):
    """User decision about a recurring-charge series (INS-3): ``ignored`` or ``confirmed``."""

    __tablename__ = "recurring_overrides"
    __table_args__ = (_check_in("status", RECURRING_STATUSES, "status"),)

    merchant_key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str]


# --------------------------------------------------------------------------- v_allocations (view)

#: Separate metadata so the view is never created, dropped or compared as a table.
view_metadata = MetaData()

v_allocations = Table(
    "v_allocations",
    view_metadata,
    Column("transaction_id", BigInteger),
    Column("account_id", BigInteger),
    Column("date", Date),
    Column("authorized_date", Date),
    Column("posted_date", Date),
    Column("spend_date", Date),
    Column("pending", Boolean),
    Column("category_id", BigInteger),
    Column("amount", Numeric(14, 2)),
    Column("pfc_primary", Text),
    Column("pfc_detailed", Text),
    Column("transaction_code", Text),
    Column("source", Text),
)
"""One row per (transaction, category) allocation; the single source of truth for money.

Splits are used only when they sum exactly to the transaction's amount; otherwise the whole amount
goes to the transaction's category. Rows with ``duplicate_status = 'suspect'`` are excluded.
Query it with Core: ``select(v_allocations.c.amount).where(...)``.
"""


# --------------------------------------------------------------------------- helpers


def get_app_settings(session: Session) -> AppSettings:
    """The single ``app_settings`` row (re-created with defaults if it is missing)."""
    row = session.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1)
        session.add(row)
        session.flush()
    return row


def system_category(session: Session, key: str) -> Category:
    """The system category ``uncategorized`` or ``card_payments``."""
    if key not in SYSTEM_KEYS:
        raise ValueError(f"unknown system category {key!r}")
    category = session.scalars(select(Category).where(Category.system_key == key)).one_or_none()
    if category is None:
        raise LookupError(f"system category {key!r} is missing; run the migrations")
    return category
