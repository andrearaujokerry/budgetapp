"""``import-csv`` — built by the CSV package (SYNC-8)."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "import-csv",
        handler=not_built,
        help="import an Amex CSV download (extended format) into an account",
    )
    parser.add_argument(
        "file", metavar="FILE", help="the CSV file downloaded from americanexpress.com"
    )
    parser.add_argument("--account", type=int, metavar="ID", help="target account (database id)")
    parser.add_argument("--dry-run", action="store_true", help="preview without importing")
