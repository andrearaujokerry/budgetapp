"""``migrate`` — upgrade this environment's database to the code's schema (Alembic).

Connects as the environment's owner role. Production normally migrates only through the
Application Support install (``./run install-agents`` / ``./run live``); doing it here needs an
explicit ``--env production``.
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


def run(args: argparse.Namespace, settings: Settings) -> int:
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
