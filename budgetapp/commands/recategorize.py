"""``recategorize`` — built by the CAT package (CAT-4) on ``services.categorize.recategorize``."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "recategorize",
        handler=not_built,
        help="re-apply rules and mappings to past transactions (never changes your own edits)",
    )
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD", help="only transactions on or after this date"
    )
    parser.add_argument(
        "--only-uncategorized", action="store_true", help="only Uncategorized transactions"
    )
    parser.add_argument("--dry-run", action="store_true", help="count changes without saving")
