"""Shared pytest fixtures (ARCHITECTURE §12, MNT-2).

Each pytest process connects as ``budgetapp_test`` (a non-superuser, peer-mapped on the private
socket), creates ``budgetapp_test_<pid>``, migrates it, and drops it at the end. The harness
refuses to run unless the role is not a superuser and the database name starts with
``budgetapp_test_``. Every test runs inside a transaction that is rolled back.

Fixtures: ``settings``, ``engine``, ``db`` (a Session joined to the outer transaction with
savepoints), ``session_factory`` (sessions on the same connection), ``secrets`` (the in-memory
store, installed for EVERY test so nothing touches the real Keychain), ``notifier`` (recording),
``fake_plaid``, ``freeze_today``.
"""

from __future__ import annotations

import os

os.environ["BUDGETAPP_ENV"] = "test"  # before anything from budgetapp is imported

import datetime as dt  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy import Connection, Engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from budgetapp import clock, logsetup  # noqa: E402
from budgetapp import secrets as app_secrets  # noqa: E402
from budgetapp.config import TEST_DB_PREFIX, Settings, load_settings  # noqa: E402
from budgetapp.db import is_ready, make_engine, run_migrations  # noqa: E402
from budgetapp.services.notify import RecordingNotifier  # noqa: E402
from tests.fake_plaid import FakePlaidGateway  # noqa: E402

LIVE_SANDBOX = bool(os.environ.get("BUDGETAPP_LIVE_SANDBOX"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if LIVE_SANDBOX:
        return
    skip = pytest.mark.skip(reason="live Plaid Sandbox test: run with ./run test --sandbox")
    for item in items:
        if "sandbox" in item.keywords:
            item.add_marker(skip)


def _refuse(message: str) -> None:
    pytest.exit(f"Refusing to run the tests: {message}", returncode=3)


def maintenance_connection(settings: Settings) -> psycopg.Connection:
    """Autocommit connection to ``postgres`` as ``budgetapp_test`` (never a superuser)."""
    return psycopg.connect(
        host=str(settings.pg_socket_dir),
        port=settings.pg_port,
        dbname="postgres",
        user=settings.db_role,
        autocommit=True,
        connect_timeout=10,
    )


def create_scratch_database(settings: Settings, name: str) -> None:
    """Create an empty ``budgetapp_test_*`` database owned by ``budgetapp_test``."""
    if not name.startswith(TEST_DB_PREFIX):
        _refuse(f"{name!r} is not a budgetapp_test_* database")
    with maintenance_connection(settings) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_scratch_database(settings: Settings, name: str) -> None:
    if not name.startswith(TEST_DB_PREFIX):
        _refuse(f"{name!r} is not a budgetapp_test_* database")
    with maintenance_connection(settings) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Test settings (``budgetapp_test_<pid>`` as ``budgetapp_test``)."""
    return load_settings("test")


@pytest.fixture(scope="session")
def test_database(settings: Settings) -> Iterator[str]:
    """Create, migrate and finally drop this process's test database."""
    if not settings.db_name.startswith(TEST_DB_PREFIX) or settings.db_role != "budgetapp_test":
        _refuse(f"unexpected database {settings.db_name!r} / role {settings.db_role!r}")
    if not is_ready(settings):
        pytest.exit(
            "The budgetapp PostgreSQL cluster (port 5434) is not running. "
            "Start it with ./run db start (or create it with ./run setup).",
            returncode=3,
        )
    with maintenance_connection(settings) as conn:
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        if row is None or row[0]:
            _refuse("the test role must exist and must not be a superuser")
    create_scratch_database(settings, settings.db_name)
    engine = make_engine(settings)
    try:
        run_migrations(engine)
        yield settings.db_name
    finally:
        engine.dispose()
        drop_scratch_database(settings, settings.db_name)


@pytest.fixture(scope="session")
def engine(settings: Settings, test_database: str) -> Iterator[Engine]:
    """Engine for the migrated test database (verified: not superuser, budgetapp_test_*)."""
    engine = make_engine(settings)
    with engine.connect() as conn:
        database, is_super = conn.execute(
            text("SELECT current_database(), rolsuper FROM pg_roles WHERE rolname = current_user")
        ).one()
    if not database.startswith(TEST_DB_PREFIX) or is_super:
        _refuse(f"connected to {database!r} (superuser={is_super})")
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after the test."""
    conn = engine.connect()
    outer = conn.begin()
    try:
        yield conn
    finally:
        if outer.is_active:
            outer.rollback()
        conn.close()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    """Sessions on the test connection; ``commit()`` releases a savepoint, nothing persists."""
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A Session whose work is rolled back at the end of the test."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def secrets() -> Iterator[app_secrets.MemoryStore]:
    """The in-memory secret store, installed for every test (the real Keychain is never used)."""
    store = app_secrets.MemoryStore()
    with app_secrets.use_store(store):
        yield store


@pytest.fixture(autouse=True)
def _isolate_globals() -> Iterator[None]:
    yield
    clock.clear_override()
    logsetup.clear_registered_secrets()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def fake_plaid() -> FakePlaidGateway:
    return FakePlaidGateway()


@pytest.fixture
def freeze_today() -> Iterator[Callable[..., dt.date]]:
    """``freeze_today("2026-10-15")`` (or a date) freezes ``clock.today()``; ``now=`` too."""

    def freeze(day: dt.date | str | None = None, *, now: dt.datetime | None = None) -> dt.date:
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        clock.set_override(today=day, now=now)
        return clock.today()

    yield freeze
    clock.clear_override()
