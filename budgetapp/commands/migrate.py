"""``migrate`` — upgrade this environment's database to the code's schema (Alembic).

Connects as the environment's owner role. Production is upgraded only by the guarded upgrade of
the Application Support install (``./run install-agents`` / ``./run live``: backup, sync lock,
migrations from the new install — ARCHITECTURE §2), so ``--env production`` is refused when this
runs from a git checkout; it works only from the installed copy.
"""

from __future__ import annotations

import argparse
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.db import migrate_env, wait_for_db


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "migrate",
        handler=run,
        help="upgrade this environment's database to the latest schema",
    )
    parser.add_argument("--revision", default="head", help="target revision (default head)")


PRODUCTION_FROM_CHECKOUT = (
    "Refusing to migrate the PRODUCTION database from a git checkout ({repo}). Production is "
    "upgraded only from the installed copy, after a backup and under the sync lock: run "
    "./run live (or ./run install-agents)."
)


def run(args: argparse.Namespace, settings: Settings) -> int:
    if settings.is_production and settings.repo_dir is not None:
        print(PRODUCTION_FROM_CHECKOUT.format(repo=settings.repo_dir))
        return 2
    if not wait_for_db(settings, timeout=30):
        print("The database is not running. Start it with: ./run db start")
        return 1
    if settings.is_production:
        print("Migrating the PRODUCTION database (budgetapp).")
    before, after = migrate_env(settings, args.revision)
    if before == after:
        print(f"{settings.db_name}: already at {after}.")
    else:
        print(f"{settings.db_name}: {before or 'empty'} → {after}.")
    return 0
