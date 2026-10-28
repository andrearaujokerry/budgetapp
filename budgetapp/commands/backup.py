"""``backup`` / ``restore`` — built by the OPS package (REL-2, ARCHITECTURE §10)."""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    backup = add_command(
        subparsers, "backup", handler=not_built, help="back up this environment's database now"
    )
    backup.add_argument(
        "--if-due", action="store_true", help="skip when the newest verified dump is recent"
    )
    backup.add_argument(
        "--min-age-hours", type=float, default=20.0, help="with --if-due: hours (default 20)"
    )
    backup.add_argument("--keep", type=int, default=30, help="dumps to keep (default 30)")
    backup.add_argument(
        "--unattended", action="store_true", help="for launchd: never show Keychain prompts"
    )
    restore = add_command(
        subparsers,
        "restore",
        handler=not_built,
        help="restore a backup (asks for confirmation; production needs --env production)",
    )
    restore.add_argument("file", metavar="FILE", help="a .dump file made by ./run backup")
    restore.add_argument(
        "--force-disconnect", action="store_true", help="disconnect running app processes first"
    )
