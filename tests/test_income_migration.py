"""Migration 0006_income_preference (INC-2): ``app_settings.show_income`` on a live database.

The owner's production database already holds a settings row they customized. Upgrading must add
the switch, on, and leave every other setting as it was; downgrading drops only the column. The
previous revision is read from the script (``down_revision``): wave 3 chains 0003 →
0005_bank_categories → 0006_income_preference.
"""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text

from budgetapp.db import (
    alembic_config,
    current_revision,
    downgrade_migrations,
    make_engine,
    run_migrations,
)
from budgetapp.models import get_app_settings
from tests.conftest import create_scratch_database, drop_scratch_database
from tests.test_migrations import HEAD

REVISION = "0006_income_preference"


def _previous() -> str:
    """The revision 0006 upgrades from (0005_bank_categories)."""
    script = ScriptDirectory.from_config(alembic_config())
    revision = script.get_revision(REVISION)
    assert revision is not None
    return revision.down_revision


@pytest.fixture
def scratch_engine(settings):
    name = f"{settings.db_name}_income"
    create_scratch_database(settings, name)
    engine = make_engine(settings, dbname=name)
    yield engine
    engine.dispose()
    drop_scratch_database(settings, name)


def _settings_row(conn) -> dict:
    row = conn.execute(text("SELECT * FROM app_settings WHERE id = 1")).mappings().one()
    return {k: v for k, v in row.items() if k != "updated_at"}


def test_upgrading_a_customized_database_turns_the_switch_on_and_keeps_the_rest(scratch_engine):
    previous = _previous()
    run_migrations(scratch_engine, previous)
    with scratch_engine.begin() as conn:  # the owner's choices before the upgrade
        conn.execute(
            text(
                "UPDATE app_settings SET budget_period_mode = 'statement', "
                "include_pending_in_budgets = false, alert_thresholds = '{50,90,100}', "
                "large_charge_threshold = 250.00, consent_reminder_days = 14 WHERE id = 1"
            )
        )
        before = _settings_row(conn)
    run_migrations(scratch_engine, REVISION)
    with scratch_engine.connect() as conn:
        after = _settings_row(conn)
        assert after.pop("show_income") is True
        assert after == before
        column = conn.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'app_settings' AND column_name = 'show_income'"
            )
        ).one()
        assert column == ("NO", "true")
        conn.execute(text("UPDATE app_settings SET show_income = false WHERE id = 1"))
        conn.commit()

    downgrade_migrations(scratch_engine, previous)
    assert current_revision(scratch_engine) == previous
    with scratch_engine.connect() as conn:
        assert _settings_row(conn) == before  # only the column went
    run_migrations(scratch_engine)
    assert current_revision(scratch_engine) == HEAD
    with scratch_engine.connect() as conn:
        assert _settings_row(conn)["show_income"] is True  # a fresh column starts on again


def test_a_new_database_starts_with_the_switch_on(db):
    assert get_app_settings(db).show_income is True
