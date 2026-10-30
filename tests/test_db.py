import threading
import time
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import exc, func, select, text

from budgetapp import db as dbmod
from budgetapp.config import ConfigError, PostgresNotFound, load_settings
from budgetapp.db import (
    LEDGER_KEY,
    LOCK_NS_GLOBAL,
    LOCK_NS_SYNC,
    DatabaseUnavailable,
    SchemaMismatch,
    admin_connect,
    code_head,
    database_url,
    is_ready,
    ledger_lock,
    link_lock,
    make_engine,
    run_migrations,
    schema_guard,
    sync_lock,
    wait_for_db,
)
from budgetapp.models import Transaction
from budgetapp.plaid_gateway import parse_json
from tests.conftest import (
    create_scratch_database,
    drop_scratch_database,
    maintenance_connection,
)
from tests.factories import make_account, make_transaction


def _advisory_locks(engine, ns):
    """Advisory locks of namespace ``ns`` in THIS database (other suites share the cluster)."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :ns "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            ),
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

    def test_accepts_a_connection_bind(self, engine, connection, session_factory):
        """SYNC gets a session factory; in tests its bind is the test Connection."""
        bind = session_factory.kw["bind"]
        assert bind is connection
        with sync_lock(bind, 45) as acquired:
            assert acquired
            with sync_lock(engine, 45) as second:
                assert second is False
        assert _advisory_locks(engine, LOCK_NS_SYNC) == 0

    def test_counts_only_this_database(self, engine, settings):
        """A sync lock held in another database (another suite) is not ours to count."""
        other = f"{settings.db_name}_lockcount"
        create_scratch_database(settings, other)
        holder_engine = make_engine(settings, dbname=other)
        try:
            with holder_engine.connect() as holder:
                holder.execute(select(func.pg_advisory_lock(LOCK_NS_SYNC, 999)))
                assert _advisory_locks(engine, LOCK_NS_SYNC) == 0
                holder.execute(select(func.pg_advisory_unlock(LOCK_NS_SYNC, 999)))
        finally:
            holder_engine.dispose()
            drop_scratch_database(settings, other)


class TestDropDatabase:
    """``drop_database``: FORCE ends our own sessions; an autovacuum worker, which FORCE can't end
    for the test role, is left to a plain DROP (the flaky "permission denied to terminate
    process" when a freshly filled database is dropped)."""

    class Conn:
        def __init__(self, *outcomes):
            self.outcomes = list(outcomes)
            self.statements: list[str] = []

        def execute(self, statement):
            self.statements.append("force" if "FORCE" in repr(statement) else "plain")
            outcome = self.outcomes.pop(0) if self.outcomes else None
            if outcome is not None:
                raise outcome

    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(dbmod.time, "sleep", lambda _seconds: None)

    def test_force_is_enough_without_autovacuum(self):
        conn = self.Conn()
        dbmod.drop_database(conn, "budgetapp_test_x")
        assert conn.statements == ["force"]

    def test_a_plain_drop_stops_an_autovacuum_worker(self):
        conn = self.Conn(psycopg.errors.InsufficientPrivilege("permission denied"))
        dbmod.drop_database(conn, "budgetapp_test_x")
        assert conn.statements == ["force", "plain"]

    def test_sessions_and_autovacuum_together_are_retried(self):
        denied = psycopg.errors.InsufficientPrivilege("permission denied")
        busy = psycopg.errors.ObjectInUse("being accessed by other users")
        conn = self.Conn(denied, busy, None)
        dbmod.drop_database(conn, "budgetapp_test_x")
        assert conn.statements == ["force", "plain", "force"]
        stuck = self.Conn(*[denied, busy] * 3)
        with pytest.raises(psycopg.errors.ObjectInUse):
            dbmod.drop_database(stuck, "budgetapp_test_x", attempts=3)
        assert stuck.statements == ["force", "plain"] * 3

    def test_a_real_database_with_a_session_is_dropped(self, settings):
        name = f"{settings.db_name}_drop"
        create_scratch_database(settings, name)
        other = make_engine(settings, dbname=name)
        held = other.connect()  # a session of our own: FORCE ends it
        try:
            with maintenance_connection(settings) as conn:
                dbmod.drop_database(conn, name)
                row = conn.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (name,))
                assert row.fetchone() == (0,)
        finally:
            held.invalidate()
            other.dispose()


def test_every_global_lock_key_is_registered_once():
    """``db`` lists every key of ``LOCK_NS_GLOBAL``; features take them from there."""
    keys = {name: getattr(dbmod, name) for name in ("LEDGER_KEY", "LINK_KEY", "LINK_WORK_KEY")}
    assert sorted(keys.values()) == [1, 2, 3]
    assert len({dbmod.LOCK_NS_SYNC, dbmod.LOCK_NS_GLOBAL}) == 2


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

    def test_an_unreachable_database_is_not_an_upgrade_problem(self, settings, tmp_path):
        missing = make_engine(settings, dbname=f"{settings.db_name}_does_not_exist")
        nowhere = make_engine(settings.replace(pg_socket_dir=tmp_path / "nosock"))
        try:
            for engine in (missing, nowhere):
                with pytest.raises(DatabaseUnavailable) as info:
                    schema_guard(engine)
                assert not isinstance(info.value, SchemaMismatch)
                assert "Upgrade needed" not in str(info.value)
        finally:
            missing.dispose()
            nowhere.dispose()

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

    def test_admin_is_the_os_user_over_the_socket(self, monkeypatch):
        monkeypatch.setenv("BUDGETAPP_ENV", "sandbox")  # as ./run db would run it
        settings = load_settings("sandbox")
        url = database_url(settings, admin=True)
        assert url.username == settings.os_user and url.database == "postgres"
        assert url.host is None and url.query["host"] == str(settings.pg_socket_dir)

    def test_test_code_never_gets_a_superuser_connection(self, settings):
        for candidate in (settings, load_settings("sandbox")):
            with pytest.raises(ConfigError, match="superuser"):
                database_url(candidate, admin=True)
            with pytest.raises(ConfigError, match="superuser"):
                make_engine(candidate, admin=True)
            with pytest.raises(ConfigError, match="superuser"):
                admin_connect(candidate)


class TestJsonColumns:
    def test_decimal_coordinates_from_the_gateway_are_stored(self, db):
        """Plaid JSON parsed with parse_float=Decimal (location.lat/lon) fits a JSONB column."""
        body = parse_json(
            b'{"location": {"address": "1 Main St", "city": "New York", '
            b'"lat": 40.7411, "lon": -73.9897}}'
        )
        assert isinstance(body["location"]["lat"], Decimal)
        txn = make_transaction(db, make_account(db), location=body["location"])
        db.expire_all()
        stored = db.get(Transaction, txn.id).location
        assert stored == {
            "address": "1 Main St",
            "city": "New York",
            "lat": Decimal("40.7411"),
            "lon": Decimal("-73.9897"),
        }
        assert isinstance(stored["lat"], Decimal)  # read back exactly, never a float

    def test_values_json_cannot_express_are_refused(self):
        with pytest.raises(TypeError):
            dbmod.json_dumps({"x": Decimal("NaN")})
        with pytest.raises(TypeError):
            dbmod.json_dumps({"x": object()})


def test_wait_for_db(settings, tmp_path):
    assert wait_for_db(settings, timeout=5)
    nowhere = settings.replace(pg_socket_dir=tmp_path / "nosock")
    started = time.monotonic()
    assert wait_for_db(nowhere, timeout=1, interval=0.2) is False
    assert time.monotonic() - started < 15


def test_a_missing_pg_isready_is_not_a_stopped_cluster(settings, tmp_path):
    no_postgres = settings.replace(pg_bin_dir=tmp_path / "bin")
    with pytest.raises(PostgresNotFound, match="brew install postgresql@18"):
        is_ready(no_postgres)
    started = time.monotonic()
    with pytest.raises(PostgresNotFound):
        wait_for_db(no_postgres, timeout=30)
    assert time.monotonic() - started < 5  # fails at once, no 120 s wait


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
