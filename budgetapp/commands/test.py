"""``test`` — run the test suite with pytest (MNT-2).

``./run test`` runs everything except the live Plaid Sandbox tests; ``./run test --sandbox`` runs
only those (they need Sandbox keys in Keychain). Any other arguments go to pytest, e.g.
``./run test -k money -x``. Tests always use ``BUDGETAPP_ENV=test``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "test",
        handler=run,
        help="run the test suite (extra arguments go to pytest)",
        accepts_extra=True,
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="run only the live Plaid Sandbox tests (needs Sandbox keys in Keychain)",
    )


def pytest_command(args: argparse.Namespace) -> list[str]:
    """The pytest argv for ``./run test``."""
    marker = "sandbox" if args.sandbox else "not sandbox"
    return [sys.executable, "-m", "pytest", "-m", marker, *args.extra]


def run(args: argparse.Namespace, settings: Settings) -> int:
    repo = settings.repo_dir or Path.cwd()
    env = {**os.environ, "BUDGETAPP_ENV": "test"}
    if args.sandbox:
        env["BUDGETAPP_LIVE_SANDBOX"] = "1"
    os.chdir(repo)
    argv = pytest_command(args)
    os.execve(argv[0], argv, env)
    return 0  # pragma: no cover - execve does not return
