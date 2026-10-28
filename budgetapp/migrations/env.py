"""Alembic environment for budgetapp (ARCHITECTURE §2, MNT-3).

The app runs migrations through ``budgetapp.db.run_migrations()``, which passes an open connection
in ``config.attributes["connection"]``; this file then migrates on that connection inside the
caller's transaction. Run by hand (``alembic`` with the repo-root ``alembic.ini``), it connects to
the database of ``$BUDGETAPP_ENV`` (default sandbox) as that environment's role.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from budgetapp.models import Base

config = context.config
target_metadata = Base.metadata

if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """``alembic upgrade --sql``: emit SQL without a database connection."""
    context.configure(
        url="postgresql+psycopg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure_and_run(connection)
        return

    from budgetapp.config import load_settings
    from budgetapp.db import make_engine

    engine = make_engine(load_settings())
    try:
        with engine.begin() as conn:
            _configure_and_run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
