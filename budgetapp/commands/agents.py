"""``install-agents`` / ``uninstall-agents`` — built by the OPS package (ARCHITECTURE §10)."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    install = add_command(
        subparsers,
        "install-agents",
        handler=not_built,
        help="install/upgrade the production copy and (re)load the launchd agents",
    )
    install.add_argument(
        "--with-web", action="store_true", help="also keep the production web app running"
    )
    install.add_argument("--dry-run", action="store_true", help="print the plan without changes")
    uninstall = add_command(
        subparsers, "uninstall-agents", handler=not_built, help="remove the launchd agents"
    )
    uninstall.add_argument("--dry-run", action="store_true", help="print the plan without changes")
