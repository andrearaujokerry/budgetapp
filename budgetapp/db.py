"""Database access: URLs, engines, sessions, advisory locks, schema guard and migrations.

Connection rules (ARCHITECTURE §2):

* sandbox / production: TCP to 127.0.0.1:5434 as the environment's role (``budgetapp_dev`` /
  ``budgetapp_live``) with the password from Keychain and ``require_auth=scram-sha-256``.
* test: the private Unix socket with peer authentication as ``budgetapp_test`` (non-superuser).
* admin (``setup`` and ``./run db`` only): the private socket with peer auth as the macOS user,
  who is the cluster superuser.

Locks (ARCHITECTURE §6 "Locks"):

* :func:`sync_lock` — per-Item ``pg_try_advisory_lock`` on a dedicated connection, held for the
  whole sync and released in ``finally``.
* :func:`ledger_lock` — global ``pg_advisory_xact_lock`` taken FIRST in every transaction that
  writes transactions, splits or tombstones.
* :func:`link_lock` — ``pg_advisory_xact_lock`` around the production new-link guard.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
from sqlalchemy import URL, Connection, Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import secrets
from budgetapp.config import Settings

if TYPE_CHECKING:
    from alembic.config import Config

log = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"

#: int4 namespaces for advisory locks (two-key form): (namespace, object id).
LOCK_NS_SYNC = 0x62610001  # per Item: (LOCK_NS_SYNC, plaid_items.id)
LOCK_NS_GLOBAL = 0x62610002  # (LOCK_NS_GLOBAL, LEDGER_KEY | LINK_KEY)
LEDGER_KEY = 1
LINK_KEY = 2

MIGRATIONS_LOCATION = "budgetapp:migrations"


class SchemaMismatch(Exception):
    """The database's Alembic revision differs from the code's head revision."""

    def __init__(self, db_revision: str | None, code_revision: str | None) -> None:
        self.db_revision = db_revision
        self.code_revision = code_revision
        super().__init__(
            f"Database schema is at {db_revision or 'nothing (empty database)'} but this code "
            f"expects {code_revision}. Upgrade needed."
        )


# --------------------------------------------------------------------------- URLs and engines


def database_url(
    settings: Settings,
    *,
    admin: bool = False,
    dbname: str | None = None,
    password: str | None = None,
) -> URL:
    """The SQLAlchemy URL for ``settings`` (the password is hidden when the URL is printed).

    ``admin=True`` connects over the socket as the macOS user (cluster superuser) to ``dbname``
    (default ``postgres``). Otherwise sandbox/production use TCP + scram with the role password
    from Keychain (or ``password``), and test uses the socket + peer as ``budgetapp_test``.
    """
    socket_query = {"host": str(settings.pg_socket_dir), "port": str(settings.pg_port)}
    if admin:
        return URL.create(
            DRIVER,
            username=settings.os_user,
            database=dbname or "postgres",
            query=socket_query,
        )
    if settings.env == "test":
        return URL.create(
            DRIVER,
            username=settings.db_role,
            database=dbname or settings.db_name,
            query=socket_query,
        )
    if password is None:
        password = secrets.require_secret(
            secrets.db_password_key(settings.db_role),
            hint="Run ./run setup to create the database roles.",
        )
    return URL.create(
        DRIVER,
        username=settings.db_role,
        password=password,
        host=settings.pg_host,
        port=settings.pg_port,
        database=dbname or settings.db_name,
        query={"require_auth": "scram-sha-256", "sslmode": "disable"},
    )


def make_engine(
    settings: Settings,
    *,
    admin: bool = False,
    dbname: str | None = None,
    password: str | None = None,
    **kwargs: object,
) -> Engine:
    """An engine for the environment's database (see :func:`database_url`)."""
    connect_args = {"connect_timeout": 10, "application_name": f"budgetapp-{settings.env}"}
    connect_args.update(kwargs.pop("connect_args", {}) or {})  # type: ignore[arg-type]
    options: dict[str, object] = {"pool_pre_ping": True, "connect_args": connect_args}
    options.update(kwargs)
    return create_engine(
        database_url(settings, admin=admin, dbname=dbname, password=password), **options
    )


def make_session_factory(engine: Engine | Connection) -> sessionmaker[Session]:
    """A ``sessionmaker`` bound to ``engine`` with ``expire_on_commit=False``."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def admin_connect(settings: Settings, dbname: str = "postgres") -> psycopg.Connection:
    """An autocommit psycopg connection as the cluster superuser over the private socket.

    Only ``setup`` and ``./run db`` admin operations use this.
    """
    return psycopg.connect(
        host=str(settings.pg_socket_dir),
        port=settings.pg_port,
        dbname=dbname,
        user=settings.os_user,
        autocommit=True,
        connect_timeout=10,
        application_name="budgetapp-admin",
    )


def wait_for_db(settings: Settings, timeout: float = 120.0, *, interval: float = 1.0) -> bool:
    """Wait until the cluster accepts connections (``pg_isready``), up to ``timeout`` seconds.

    Returns ``False`` on timeout: "not ready" means "try later", never an alert.
    """
    deadline = time.monotonic() + timeout
    delay = interval
    while True:
        if is_ready(settings):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(delay, remaining))
        delay = min(delay * 1.5, 10.0)


def is_ready(settings: Settings) -> bool:
    """True when ``pg_isready`` reports the cluster is accepting connections."""
    try:
        result = subprocess.run(
            [
                settings.pg_bin("pg_isready"),
                "-q",
                "-h",
                str(settings.pg_socket_dir),
                "-p",
                str(settings.pg_port),
                "-d",
                "postgres",
                "-t",
                "3",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------- locks


@contextmanager
def sync_lock(engine: Engine, item_id: int) -> Iterator[bool]:
    """Hold the per-Item sync lock for the duration of the ``with`` block.

    Yields ``True`` when acquired, ``False`` when another process holds it (record the run as
    ``skipped``). The lock lives on a dedicated autocommit connection that stays checked out for
    the whole block and is unlocked in ``finally``; if unlocking fails the connection is discarded,
    which also releases the lock.
    """
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(
            conn.execute(select(func.pg_try_advisory_lock(LOCK_NS_SYNC, int(item_id)))).scalar()
        )
        yield acquired
    finally:
        try:
            if acquired:
                released = conn.execute(
                    select(func.pg_advisory_unlock(LOCK_NS_SYNC, int(item_id)))
                ).scalar()
                if not released:
                    conn.invalidate()
        except Exception:
            log.warning("could not release the sync lock cleanly; discarding the connection")
            conn.invalidate()
        finally:
            conn.close()


def ledger_lock(session: Session | Connection) -> None:
    """Take the global ledger lock for the current transaction (``pg_advisory_xact_lock``).

    Call it FIRST in every transaction that writes transactions, splits or tombstones (sync apply,
    CSV import, reconciliation, attaching, rule re-apply, category merge, user edits). Released at
    commit or rollback.
    """
    session.execute(select(func.pg_advisory_xact_lock(LOCK_NS_GLOBAL, LEDGER_KEY)))


def link_lock(session: Session | Connection) -> None:
    """Take the link lock for the current transaction; guards issuing production new-link tokens."""
    session.execute(select(func.pg_advisory_xact_lock(LOCK_NS_GLOBAL, LINK_KEY)))


# --------------------------------------------------------------------------- migrations


def alembic_config(connection: Connection | None = None) -> Config:
    """An Alembic config built in code (``script_location = budgetapp:migrations``).

    With ``connection``, ``migrations/env.py`` runs on that connection instead of opening its own.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_LOCATION)
    cfg.set_main_option("path_separator", "os")
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def code_head() -> str:
    """The head revision of the migrations shipped with this code."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - there is always at least 0001
        raise RuntimeError("no migrations found")
    return head


def current_revision(bind: Engine | Connection) -> str | None:
    """The database's current Alembic revision (``None`` for an empty database)."""
    from alembic.runtime.migration import MigrationContext

    if isinstance(bind, Engine):
        with bind.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    return MigrationContext.configure(bind).get_current_revision()


def run_migrations(bind: Engine | Connection, revision: str = "head") -> None:
    """Upgrade the database to ``revision`` (default ``head``) in one transaction."""
    from alembic import command

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            command.upgrade(alembic_config(conn), revision)
    else:
        command.upgrade(alembic_config(bind), revision)


def downgrade_migrations(bind: Engine | Connection, revision: str) -> None:
    """Downgrade the database to ``revision`` (``base`` drops everything). Never on production."""
    from alembic import command

    if isinstance(bind, Engine):
        with bind.begin() as conn:
            command.downgrade(alembic_config(conn), revision)
    else:
        command.downgrade(alembic_config(bind), revision)


def migrate_env(settings: Settings, revision: str = "head") -> tuple[str | None, str | None]:
    """Migrate the environment's own database as its owner role; returns ``(before, after)``."""
    engine = make_engine(settings)
    try:
        before = current_revision(engine)
        run_migrations(engine, revision)
        after = current_revision(engine)
    finally:
        engine.dispose()
    return before, after


def schema_guard(engine: Engine) -> str:
    """Raise :class:`SchemaMismatch` unless the database is exactly at the code's head revision.

    Every entry point calls this. In production nothing migrates automatically: the web app shows
    an "upgrade needed" page and sync exits quietly; sandbox ``./run`` migrates first.
    """
    head = code_head()
    try:
        current = current_revision(engine)
    except Exception as exc:
        raise SchemaMismatch(None, head) from exc
    if current != head:
        raise SchemaMismatch(current, head)
    return head


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """``with db.transaction(factory) as session:`` — one unit of work.

    Commits when the block succeeds, rolls back when it raises, and always closes the session.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def ping(bind: Engine | Connection) -> bool:
    """True if a trivial query succeeds."""
    try:
        if isinstance(bind, Engine):
            with bind.connect() as conn:
                conn.execute(text("SELECT 1"))
        else:
            bind.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
