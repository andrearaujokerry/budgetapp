"""``install-agents`` / ``uninstall-agents``: the production launchd agents (ARCHITECTURE §10).

``./run install-agents`` upgrades the production install in
``~/Library/Application Support/budgetapp/venv`` from this repository (backup first, never
editable), then writes and loads the launchd agents: ``postgres`` (at login, kept running),
``sync`` (00:15, 06:15, 12:15, 18:15 and at login) and ``backup`` (02:30 and at login), plus
``web`` (the production app on http://127.0.0.1:8765, kept running) with ``--with-web``.
Running it again re-installs them. ``--dry-run`` prints the plan and changes nothing.

``./run uninstall-agents`` boots them out (web → backup → sync → postgres, stopping PostgreSQL
with ``pg_ctl stop -m fast`` first) and deletes their plists. The install, the database and the
backups stay.

The agents always run the production app, whatever ``--env`` says.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings, load_settings
from budgetapp.services import agents
from budgetapp.services.backup import BackupError


def register(subparsers: Any) -> None:
    install = add_command(
        subparsers,
        "install-agents",
        handler=run_install,
        help="install/upgrade the production copy and (re)load the launchd agents",
        description=__doc__,
    )
    install.add_argument(
        "--with-web", action="store_true", help="also keep the production web app running"
    )
    install.add_argument("--dry-run", action="store_true", help="print the plan without changes")
    uninstall = add_command(
        subparsers,
        "uninstall-agents",
        handler=run_uninstall,
        help="remove the launchd agents",
        description=__doc__,
    )
    uninstall.add_argument("--dry-run", action="store_true", help="print the plan without changes")


def production_settings(settings: Settings) -> Settings:
    """The agents run production only: use its settings whatever ``--env`` selected."""
    return settings if settings.is_production else load_settings("production")


def run_install(args: argparse.Namespace, settings: Settings) -> int:
    print("Production launchd agents: they run the installed copy with BUDGETAPP_ENV=production.")
    try:
        agents.install_agents(
            production_settings(settings), with_web=args.with_web, dry_run=args.dry_run
        )
    except (agents.AgentError, BackupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def run_uninstall(args: argparse.Namespace, settings: Settings) -> int:
    try:
        agents.uninstall_agents(production_settings(settings), dry_run=args.dry_run)
    except agents.AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
