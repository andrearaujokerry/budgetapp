"""``doctor`` — built by the OPS package (SEC-1..6 and health checks).

Run interactively (``./run doctor --env production``), it also reads every Keychain item of both
environments so the user can click Always Allow after a Python upgrade.
"""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    add_command(
        subparsers,
        "doctor",
        handler=not_built,
        help="check security, backups, agents, keys and schema; re-approve Keychain access",
    )
