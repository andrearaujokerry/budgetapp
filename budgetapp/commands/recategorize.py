"""``recategorize`` — re-apply rules and the Plaid/Amex category mappings (CAT-4).

``./run recategorize [--since YYYY-MM-DD] [--only-uncategorized] [--dry-run]`` runs
categorization again (card payments, then your rules in order, then the Plaid or Amex category
mapping, else Uncategorized) on every past transaction in scope whose category you didn't set
yourself, and prints the counts. Transactions you categorized by hand are never changed.
``--dry-run`` only counts. The same thing is on the Rules page ("Re-apply rules to past
transactions").
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.db import (
    DatabaseUnavailable,
    make_engine,
    make_session_factory,
    schema_guard,
    transaction,
    wait_for_db,
)
from budgetapp.services import rules

NOT_RUNNING = "The database is not running. Start it with: ./run db start"


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from exc


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "recategorize",
        handler=run,
        help="re-apply rules and mappings to past transactions (never changes your own edits)",
        description=__doc__,
    )
    parser.add_argument(
        "--since",
        type=_date,
        metavar="YYYY-MM-DD",
        help="only transactions on or after this date",
    )
    parser.add_argument(
        "--only-uncategorized", action="store_true", help="only Uncategorized transactions"
    )
    parser.add_argument("--dry-run", action="store_true", help="count changes without saving")


def open_database(settings: Settings) -> tuple[sessionmaker[Session], Callable[[], None]]:
    """A session factory for this environment's database (schema checked) and its closer."""
    if not wait_for_db(settings, timeout=30):
        raise DatabaseUnavailable(NOT_RUNNING)
    engine = make_engine(settings)
    try:
        schema_guard(engine)
    except BaseException:
        engine.dispose()
        raise
    return make_session_factory(engine), engine.dispose


def run(args: argparse.Namespace, settings: Settings) -> int:
    factory, close = open_database(settings)
    try:
        return recategorize(
            factory,
            since=args.since,
            only_uncategorized=args.only_uncategorized,
            dry_run=args.dry_run,
        )
    finally:
        close()


def recategorize(
    factory: sessionmaker[Session],
    *,
    since: dt.date | None = None,
    only_uncategorized: bool = False,
    dry_run: bool = False,
    out: Callable[[str], Any] = print,
) -> int:
    """Re-apply categories in one transaction and print what happened; returns the exit code."""
    scope = rules.ReapplyScope(since=since, only_uncategorized=only_uncategorized)
    with transaction(factory) as session:
        result = rules.reapply(session, scope, dry_run=dry_run, limit=0)
    for line in report_lines(result):
        out(line)
    return 0


def report_lines(result: rules.ReapplyResult) -> list[str]:
    """The command's output for a re-apply result."""
    what = result.scope.description
    if result.dry_run:
        head = f"Dry run for {what}: nothing was saved."
        changed = "would change category"
    else:
        head = f"Re-applied rules and categories to {what}."
        changed = "changed category"
    width = len(f"{max(result.examined, result.changed, result.skipped_user):,}")
    return [
        head,
        f"  {result.examined:>{width},} checked",
        f"  {result.changed:>{width},} {changed}",
        f"  {result.skipped_user:>{width},} kept: you set their category yourself",
    ]
