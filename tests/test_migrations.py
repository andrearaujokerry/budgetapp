import csv
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, text

from budgetapp.db import (
    code_head,
    current_revision,
    downgrade_migrations,
    make_engine,
    run_migrations,
)
from budgetapp.models import Base, Category, CategoryMapping, get_app_settings
from tests.conftest import create_scratch_database, drop_scratch_database

MIGRATION_2 = Path(__file__).parent.parent / "budgetapp/migrations/versions/0002_seed_defaults.py"


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
    assert current_revision(scratch_engine) == code_head() == "0002_seed_defaults"
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
        assert counts == [("amex_csv", 29), ("plaid_pfc", 145)]
        assert conn.execute(text("SELECT count(*) FROM app_settings")).scalar() == 1


def test_models_match_the_migrations(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diffs = compare_metadata(context, Base.metadata)
    # Expression indexes (lower(name)) cannot be compared by autogenerate; everything else must.
    diffs = [d for d in diffs if "uq_categories_lower_name" not in repr(d)]
    assert diffs == []


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
        assert len({c.color for c in categories.values()}) == 19
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
        csv_path = Path(
            "/private/tmp/claude-501/-Users-andrearaujokerry-Desktop-budgetapp/"
            "26193c1b-37e3-48b9-9278-3fb6a7b619fe/scratchpad/dl/csv/pfc-taxonomy-all.csv"
        )
        if not csv_path.exists():
            pytest.skip("Plaid's taxonomy CSV is not on this machine")
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

    def test_app_settings_defaults(self, db):
        row = get_app_settings(db)
        assert row.budget_period_mode == "calendar"
        assert row.include_pending_in_budgets is True
        assert row.alert_thresholds == [80, 100]
        assert row.consent_reminder_days == 30
        assert row.statement_settle_days == 3
        assert row.ai_enabled is False
        assert row.large_charge_threshold is None
