"""``sync`` — built by the SYNC package (ARCHITECTURE §8)."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    parser = add_command(subparsers, "sync", handler=not_built, help="sync every Plaid Item now")
    parser.add_argument(
        "--if-due", action="store_true", help="skip Items synced successfully in the last 5 hours"
    )
    parser.add_argument(
        "--min-age-hours", type=float, default=5.0, help="with --if-due: hours (default 5)"
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="for launchd: never show Keychain prompts (exit 75 when approval is needed)",
    )
    parser.add_argument("--item", type=int, metavar="ID", help="sync only this Item (database id)")
