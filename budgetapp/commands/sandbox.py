"""Sandbox-only test helpers — built by the SYNC package. They refuse to run in production."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    link = add_command(
        subparsers,
        "sandbox-link",
        handler=not_built,
        help="Sandbox only: create a test Item without Plaid Link",
    )
    link.add_argument(
        "--user",
        choices=("user_good", "user_transactions_dynamic", "custom"),
        default="user_transactions_dynamic",
        help="Sandbox test user (custom = an Amex-like credit card with known totals)",
    )
    link.add_argument(
        "--institution", default="ins_109508", help="institution id (default ins_109508)"
    )
    refresh = add_command(
        subparsers,
        "sandbox-refresh",
        handler=not_built,
        help="Sandbox only: /transactions/refresh, then sync",
    )
    refresh.add_argument("--item", type=int, metavar="ID", help="only this Item (database id)")
    reset = add_command(
        subparsers,
        "sandbox-reset-login",
        handler=not_built,
        help="Sandbox only: force ITEM_LOGIN_REQUIRED to test reconnecting",
    )
    reset.add_argument("--item", type=int, metavar="ID", help="only this Item (database id)")
