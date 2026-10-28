import threading
import time

import pytest
from sqlalchemy import exc, func, select, text

from budgetapp import db as dbmod
from budgetapp.config import load_settings
from budgetapp.db import (
    LEDGER_KEY,
    LOCK_NS_GLOBAL,
    LOCK_NS_SYNC,
    SchemaMismatch,
    code_head,
    database_url,
    ledger_lock,
    link_lock,
    make_engine,
    run_migrations,
    schema_guard,
    sync_lock,
    wait_for_db,
)
from tests.conftest import create_scratch_database, drop_scratch_database


def _advisory_locks(engine, ns):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :ns"),
            {"ns": ns},
        ).scalar()


class TestSyncLock:
    def test_second_holder_is_refused_until_released(self, engine):
        with sync_lock(engine, 41) as first:
            assert first is True
            with sync_lock(engine, 41) as second:
                assert second is False
            with sync_lock(engine, 42) as other_item:
                assert other_item is True
        with sync_lock(engine, 41) as again:
            assert again is True
        assert _advisory_locks(engine, LOCK_NS_SYNC) == 0

    def test_released_when_the_body_raises(self, engine):
        with pytest.raises(RuntimeError), sync_lock(engine, 43) as acquired:
            assert acquired
            raise RuntimeError("sync failed")
        with sync_lock(engine, 43) as acquired:
            assert acquired
        assert _advisory_locks(engine, LOCK_NS_SYNC) == 0

    def test_lock_survives_commits_inside_the_block(self, engine):
        with sync_lock(engine, 44) as acquired:
            assert acquired
            with engine.begin() as conn:  # other transactions come and go
                conn.execute(text("SELECT 1"))
            with sync_lock(engine, 44) as second:
                assert second is False


class TestLedgerLock:
    def test_serializes_writers(self, engine, session_factory):
        holder = session_factory()
        ledger_lock(holder)
        with engine.connect() as other:
            other.execute(text("SET lock_timeout = '200ms'"))
            with pytest.raises(exc.OperationalError):
                ledger_lock(other)
            other.rollback()
        holder.rollback()
        with engine.connect() as other:
            got = other.execute(
                select(func.pg_try_advisory_xact_lock(LOCK_NS_GLOBAL, LEDGER_KEY))
            ).scalar()
            assert got is True
            other.rollback()

    def test_second_writer_waits_then_proceeds(self, engine):
        order: list[str] = []
        first = engine.connect()
        first.begin()
        ledger_lock(first)

        def writer():
            with engine.connect() as conn:
                ledger_lock(conn)
                order.append("second")
                conn.rollback()

        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(0.3)
        order.append("first-commit")
        first.commit()
        first.close()
        thread.join(timeout=10)
        assert order == ["first-commit", "second"]

    def test_link_lock_is_separate(self, engine):
        with engine.connect() as a, engine.connect() as b:
            a.begin()
            ledger_lock(a)
            b.begin()
            b.execute(text("SET LOCAL lock_timeout = '200ms'"))
            link_lock(b)  # different key: no wait
            b.rollback()
            a.rollback()


class TestSchemaGuard:
    def test_passes_at_head(self, engine):
        assert schema_guard(engine) == code_head()

    def test_detects_an_empty_or_old_database(self, settings):
        name = f"{settings.db_name}_guard"
        create_scratch_database(settings, name)
        engine = make_engine(settings, dbname=name)
        try:
            with pytest.raises(SchemaMismatch) as empty:
                schema_guard(engine)
            assert empty.value.db_revision is None
            run_migrations(engine, "0001_initial")
            with pytest.raises(SchemaMismatch) as old:
                schema_guard(engine)
            assert old.value.db_revision == "0001_initial"
            assert old.value.code_revision == code_head()
            assert "Upgrade needed" in str(old.value)
        finally:
            engine.dispose()
            drop_scratch_database(settings, name)


class TestUrls:
    def test_sandbox_uses_tcp_scram_and_keychain_password(self, secrets):
        secrets.values["db.password.budgetapp_dev"] = "s3cret-password-value"
        url = database_url(load_settings("sandbox"))
        assert url.host == "127.0.0.1" and url.port == 5434
        assert url.username == "budgetapp_dev" and url.database == "budgetapp_sandbox"
        assert url.query["require_auth"] == "scram-sha-256"
        assert url.password == "s3cret-password-value"
        assert "s3cret" not in str(url)

    def test_production_needs_its_own_password(self, secrets):
        from budgetapp.secrets import MissingSecretError

        with pytest.raises(MissingSecretError):
            database_url(load_settings("production"))

    def test_test_env_uses_the_socket_and_peer(self, settings):
        url = database_url(settings)
        assert url.username == "budgetapp_test"
        assert url.password is None
        assert url.query["host"] == str(settings.pg_socket_dir)

    def test_admin_is_the_os_user_over_the_socket(self, settings):
        url = database_url(settings, admin=True)
        assert url.username == settings.os_user and url.database == "postgres"
        assert url.host is None


def test_wait_for_db(settings, tmp_path):
    assert wait_for_db(settings, timeout=5)
    nowhere = settings.replace(pg_socket_dir=tmp_path / "nosock")
    started = time.monotonic()
    assert wait_for_db(nowhere, timeout=1, interval=0.2) is False
    assert time.monotonic() - started < 15


def test_transaction_helper_commits_and_rolls_back(session_factory):
    from budgetapp.models import RecurringOverride

    with dbmod.transaction(session_factory) as session:
        session.add(RecurringOverride(merchant_key="kept", status="ignored"))
    with pytest.raises(RuntimeError), dbmod.transaction(session_factory) as session:
        session.add(RecurringOverride(merchant_key="dropped", status="ignored"))
        session.flush()
        raise RuntimeError
    with session_factory() as check:
        keys = set(check.scalars(select(RecurringOverride.merchant_key)))
    assert "kept" in keys and "dropped" not in keys
