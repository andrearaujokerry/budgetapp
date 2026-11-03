import csv
import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import exc, select, text

from budgetapp.db import (
    code_head,
    current_revision,
    downgrade_migrations,
    make_engine,
    run_migrations,
)
from budgetapp.models import Base, Category, CategoryMapping, get_app_settings
from tests.conftest import create_scratch_database, drop_scratch_database
from tests.factories import make_account, make_item

VERSIONS = Path(__file__).parent.parent / "budgetapp/migrations/versions"
MIGRATION_2 = VERSIONS / "0002_seed_defaults.py"
HEAD = "0003_f1_verification_fixes"

# The palette values migration 0003 gives the seeded categories (ui.color_value / resolveColor).
PALETTE = {
    "Travel": "cat-1",
    "Dining & Drinks": "cat-2",
    "Groceries": "cat-3",
    "Transportation": "cat-4",
    "Shopping": "cat-5",
    "Home": "cat-6",
    "Subscriptions & Streaming": "cat-7",
    "Entertainment": "cat-8",
    "Utilities & Bills": "cat-9",
    "Other": "cat-10",
    "Personal Care": "cat-11",
    "Education": "cat-12",
    "Health & Fitness": "cat-13",
    "Fees & Interest": "cat-14",
    "Services": "cat-15",
    "Gifts & Donations": "cat-16",
    "Credits & Rewards": "other",
    "Uncategorized": "other",
    "Card Payments": "other",
}

# Exact catalog comparison: columns (type, nullability, default, identity), every constraint's
# full definition (CHECK text, FK ON DELETE, UNIQUE NULLS NOT DISTINCT) and every index definition
# (method, expression, uniqueness). Column ORDER is not compared (ALTER TABLE ADD COLUMN appends).
_COLUMNS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attidentity
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relname <> 'alembic_version'
"""
_CONSTRAINTS = """
SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid)
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname <> 'alembic_version' AND k.contype <> 'n'
"""
_INDEXES = """
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename <> 'alembic_version'
"""


def _catalog(engine):
    with engine.connect() as conn:
        return {
            name: set(map(tuple, conn.execute(text(query)).all()))
            for name, query in (
                ("columns", _COLUMNS),
                ("constraints", _CONSTRAINTS),
                ("indexes", _INDEXES),
            )
        }


def _view_column_dependencies(conn):
    """How many ``transactions`` columns the v_allocations view pins (pg_depend)."""
    return conn.execute(
        text(
            "SELECT count(DISTINCT d.refobjsubid) FROM pg_depend d "
            "JOIN pg_rewrite r ON r.oid = d.objid "
            "WHERE r.ev_class = 'v_allocations'::regclass "
            "AND d.refobjid = 'transactions'::regclass AND d.refobjsubid > 0"
        )
    ).scalar()


@pytest.fixture
def scratch_engine(settings):
    name = f"{settings.db_name}_mig"
    create_scratch_database(settings, name)
    engine = make_engine(settings, dbname=name)
    yield engine
    engine.dispose()
    drop_scratch_database(settings, name)


def _tables(engine):
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def test_upgrade_downgrade_upgrade(scratch_engine):
    run_migrations(scratch_engine)
    assert current_revision(scratch_engine) == code_head() == HEAD
    tables = _tables(scratch_engine)
    assert set(Base.metadata.tables) <= tables

    downgrade_migrations(scratch_engine, "base")
    assert current_revision(scratch_engine) is None
    assert _tables(scratch_engine) == {"alembic_version"}
    with scratch_engine.connect() as conn:
        views = conn.execute(text("SELECT count(*) FROM pg_views WHERE viewname = 'v_allocations'"))
        assert views.scalar() == 0

    run_migrations(scratch_engine)
    assert current_revision(scratch_engine) == code_head()
    with scratch_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM categories")).scalar() == 19
        assert conn.execute(text("SELECT count(*) FROM v_allocations")).scalar() == 0


def test_0003_downgrades_and_upgrades_cleanly(scratch_engine):
    run_migrations(scratch_engine, "0002_seed_defaults")
    with scratch_engine.begin() as conn:  # the user recoloured one seeded category
        conn.execute(text("UPDATE categories SET color = '#123456' WHERE name = 'Travel'"))
        wide = _view_column_dependencies(conn)
    run_migrations(scratch_engine)
    with scratch_engine.connect() as conn:
        colours = dict(conn.execute(text("SELECT name, color FROM categories")).all())
        assert colours == {**PALETTE, "Travel": "#123456"}  # user colours are left alone
        assert _view_column_dependencies(conn) == 13 < wide  # 12 read + duplicate_status
    downgrade_migrations(scratch_engine, "0002_seed_defaults")
    with scratch_engine.connect() as conn:
        colours = dict(conn.execute(text("SELECT name, color FROM categories")).all())
        assert colours["Groceries"] == "#16A34A" and colours["Uncategorized"] == "#9CA3AF"
        assert colours["Travel"] == "#123456"
        assert _view_column_dependencies(conn) == wide
        keys = set(
            conn.execute(
                text("SELECT external_key FROM category_mappings WHERE source = 'amex_csv'")
            )
            .scalars()
            .all()
        )
        assert not any(k.endswith("|credit") for k in keys)
        columns = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'plaid_items' AND column_name = 'error_display_message'"
            )
        ).scalar()
        assert columns == 0
    run_migrations(scratch_engine)
    assert current_revision(scratch_engine) == HEAD


def test_the_view_pins_only_the_columns_it_reads(connection):
    """Display-only columns can change without recreating v_allocations."""
    assert _view_column_dependencies(connection) == 13  # 12 read + duplicate_status
    connection.execute(text("ALTER TABLE transactions DROP COLUMN logo_url"))  # rolled back


def test_seed_is_idempotent_and_respects_existing_rows(scratch_engine):
    run_migrations(scratch_engine, "0001_initial")
    with scratch_engine.begin() as conn:  # the user already has a "groceries" category
        conn.execute(
            text(
                "INSERT INTO categories (name, kind, color) VALUES ('groceries', 'expense', '#000')"
            )
        )
    run_migrations(scratch_engine)
    with scratch_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, color FROM categories WHERE lower(name) = 'groceries'")
        ).all()
        assert rows == [("groceries", "#000")]  # kept, not duplicated or overwritten
        mapped = conn.execute(
            text(
                "SELECT c.name FROM category_mappings m JOIN categories c ON c.id = m.category_id "
                "WHERE m.external_key = 'FOOD_AND_DRINK_GROCERIES'"
            )
        ).scalar()
        assert mapped == "groceries"
        assert conn.execute(text("SELECT count(*) FROM categories")).scalar() == 19

    # Downgrading the seed and applying it again restores the same data.
    downgrade_migrations(scratch_engine, "0001_initial")
    run_migrations(scratch_engine)
    run_migrations(scratch_engine)  # already at head: a no-op
    with scratch_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM categories")).scalar() == 19
        counts = conn.execute(
            text("SELECT source, count(*) FROM category_mappings GROUP BY source ORDER BY source")
        ).all()
        assert counts == [("amex_csv", 29 + 2), ("plaid_pfc", 145)]
        assert conn.execute(text("SELECT count(*) FROM app_settings")).scalar() == 1


def test_models_match_the_migrations(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diffs = compare_metadata(context, Base.metadata)
    # Expression indexes (lower(name)) cannot be compared by autogenerate; the exact catalog
    # comparison below covers them, and CHECK constraints and index methods.
    diffs = [d for d in diffs if "uq_categories_lower_name" not in repr(d)]
    assert diffs == []


def test_models_and_migrations_build_the_same_catalog(engine, settings):
    """A database built by the migrations equals one built by ``Base.metadata.create_all``.

    Every package that adds a migration must keep this green: it compares every column, the
    full text of every constraint (CHECK, FK with ON DELETE, UNIQUE … NULLS NOT DISTINCT) and
    every index definition (method, expression, uniqueness).
    """
    name = f"{settings.db_name}_meta"
    create_scratch_database(settings, name)
    meta_engine = make_engine(settings, dbname=name)
    try:
        Base.metadata.create_all(meta_engine)
        migrated, modelled = _catalog(engine), _catalog(meta_engine)
    finally:
        meta_engine.dispose()
        drop_scratch_database(settings, name)
    for kind in ("columns", "constraints", "indexes"):
        assert sorted(migrated[kind] - modelled[kind]) == [], f"{kind} only in the migrations"
        assert sorted(modelled[kind] - migrated[kind]) == [], f"{kind} only in models.py"
    assert len(migrated["constraints"]) > 80 and len(migrated["indexes"]) > 40


def _taxonomy_codes():
    """Every PFC v2 code, read from the generated seed (which was built from Plaid's CSV)."""
    namespace: dict = {}
    source = MIGRATION_2.read_text()
    start = source.index("PLAID_PFC: list")
    end = source.index("]\n", start) + 2
    exec(source[start:end], namespace)  # noqa: S102 - our own constant
    return namespace["PLAID_PFC"]


class TestSeedData:
    def test_categories(self, db):
        categories = {c.name: c for c in db.scalars(select(Category))}
        assert len(categories) == 19
        assert {name: c.color for name, c in categories.items()} == PALETTE
        spending = [c.color for c in categories.values() if c.color != "other"]
        assert sorted(spending) == sorted(f"cat-{n}" for n in range(1, 17))  # 16 distinct slots
        assert categories["Card Payments"].kind == "transfer"
        assert categories["Card Payments"].system_key == "card_payments"
        assert categories["Uncategorized"].system_key == "uncategorized"
        assert categories["Uncategorized"].kind == "expense"
        assert categories["Credits & Rewards"].kind == "income"
        expense = {n for n, c in categories.items() if c.kind == "expense"}
        assert {"Groceries", "Dining & Drinks", "Fees & Interest", "Other"} <= expense

    def test_every_pfc_v2_code_is_mapped(self, db):
        pairs = _taxonomy_codes()
        codes = [code for code, _ in pairs]
        assert len(codes) == len(set(codes)) == 18 + 127
        mapped = dict(
            db.execute(
                select(CategoryMapping.external_key, Category.name)
                .join(Category)
                .where(CategoryMapping.source == "plaid_pfc")
            ).all()
        )
        assert set(mapped) == set(codes)
        assert mapped["LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"] == "Card Payments"
        assert mapped["LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT"] == "Card Payments"
        assert mapped["TRANSFER_IN"] == "Card Payments"
        assert mapped["BANK_FEES_INTEREST_CHARGE"] == "Fees & Interest"
        assert mapped["BANK_FEES_OTHER_BANK_FEES"] == "Fees & Interest"
        assert mapped["BANK_FEES_LATE_FEES"] == "Fees & Interest"
        assert mapped["INCOME_OTHER"] == "Credits & Rewards"
        assert mapped["ENTERTAINMENT_TV_AND_MOVIES"] == "Subscriptions & Streaming"
        assert mapped["ENTERTAINMENT_MUSIC_AND_AUDIO"] == "Subscriptions & Streaming"
        assert mapped["FOOD_AND_DRINK_GROCERIES"] == "Groceries"
        assert mapped["OTHER_OTHER"] == "Other"

    def test_seed_matches_plaids_csv_when_available(self):
        """Set ``BUDGETAPP_PFC_CSV`` to Plaid's PFC taxonomy CSV (all versions) to check the seed
        against it."""
        location = os.environ.get("BUDGETAPP_PFC_CSV")
        if not location:
            pytest.skip("set BUDGETAPP_PFC_CSV to Plaid's taxonomy CSV to run this check")
        csv_path = Path(location).expanduser()
        if not csv_path.is_file():
            pytest.fail(f"BUDGETAPP_PFC_CSV={location} is not a file")
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            rows = [
                r for r in list(csv.reader(fh))[1:] if r and r[0] and not r[0].startswith("Note")
            ]
        expected = {r[0] for r in rows} | {r[1] for r in rows}
        assert {code for code, _ in _taxonomy_codes()} == expected

    def test_amex_mappings(self, db):
        mapped = dict(
            db.execute(
                select(CategoryMapping.external_key, Category.name)
                .join(Category)
                .where(CategoryMapping.source == "amex_csv")
            ).all()
        )
        for key, category in {
            "Restaurant-Bar & Café": "Dining & Drinks",
            "Merchandise & Supplies-Groceries": "Groceries",
            "Travel-Lodging": "Travel",
            "Transportation-Fuel": "Transportation",
            "Fees & Adjustments-Fees & Adjustments": "Fees & Interest",
            "Business Services-Office Supplies": "Shopping",
            "Communications-Mobile Telecom": "Utilities & Bills",
            "Other-Miscellaneous": "Other",
        }.items():
            assert mapped[key] == category
        for group in (
            "Business Services",
            "Communications",
            "Fees & Adjustments",
            "Merchandise & Supplies",
            "Other",
            "Restaurant",
            "Transportation",
            "Travel",
        ):
            assert group in mapped
        # Negative rows (cash back, billing-inquiry credits) in Fees & Adjustments:
        assert mapped["Fees & Adjustments-Fees & Adjustments|credit"] == "Credits & Rewards"
        assert mapped["Fees & Adjustments|credit"] == "Credits & Rewards"

    def test_app_settings_defaults(self, db):
        row = get_app_settings(db)
        assert row.budget_period_mode == "calendar"
        assert row.include_pending_in_budgets is True
        assert row.alert_thresholds == [80, 100]
        assert row.consent_reminder_days == 30
        assert row.statement_settle_days == 3
        assert row.ai_enabled is False
        assert row.large_charge_threshold is None


class TestConstraints:
    def test_system_categories_keep_their_kinds(self, db):
        for key, kind in (("card_payments", "expense"), ("uncategorized", "income")):
            with (
                pytest.raises(exc.IntegrityError, match="ck_categories_system_kind"),
                db.begin_nested(),
            ):
                db.execute(
                    text("UPDATE categories SET kind = :kind WHERE system_key = :key"),
                    {"kind": kind, "key": key},
                )

    def test_an_account_is_manual_or_linked_never_half(self, db):
        item = make_item(db)
        for values in ({"item_id": item.id}, {"plaid_account_id": "acc-orphan"}):
            with (
                pytest.raises(exc.IntegrityError, match="ck_accounts_manual_or_linked"),
                db.begin_nested(),
            ):
                make_account(db, manual=True, **values)

    def test_deleting_an_item_with_accounts_is_refused_even_through_the_orm(self, db):
        """The relationship must not null accounts.item_id behind ON DELETE RESTRICT's back."""
        item = make_item(db)
        account = make_account(db, item)
        with (
            pytest.raises(exc.IntegrityError, match="fk_accounts_item_id_plaid_items"),
            db.begin_nested(),
        ):
            db.delete(item)
            db.flush()
        db.expire_all()
        assert account.item_id == item.id and not account.is_manual
