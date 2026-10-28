"""``python -m budgetapp <command>`` (what ``./run`` calls).

Every command defaults to the Sandbox environment; real data is touched only with an explicit
``--env production`` (or ``./run live``) — Principle 6 / SEC-5.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys

from budgetapp import __version__
from budgetapp.config import ConfigError, load_settings
from budgetapp.logsetup import install_excepthook, setup_logging

#: Command modules in ``budgetapp/commands`` (a fixed list; each has ``register(subparsers)``).
COMMAND_MODULES = (
    "serve",
    "setup",
    "db",
    "migrate",
    "sync",
    "sandbox",
    "import_csv",
    "recategorize",
    "backup",
    "doctor",
    "agents",
    "notify_test",
    "test",
)

EXIT_KEYCHAIN = 75  # EX_TEMPFAIL: launchd jobs exit with this when Keychain approval is needed


def default_env() -> str:
    return os.environ.get("BUDGETAPP_ENV") or "sandbox"


def build_parser() -> argparse.ArgumentParser:
    """The full argument parser with every command registered."""
    parser = argparse.ArgumentParser(
        prog="budgetapp",
        description="Amex Budget Tracker — runs only on this Mac. Run ./run with no arguments to "
        "start the Sandbox app.",
        epilog="Real data is used only with --env production (or ./run live).",
    )
    parser.add_argument(
        "--env",
        choices=("sandbox", "production"),
        default=default_env(),
        help="environment (default: $BUDGETAPP_ENV or sandbox)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="more output")
    parser.add_argument("--version", action="version", version=f"budgetapp {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for name in COMMAND_MODULES:
        module = importlib.import_module(f"budgetapp.commands.{name}")
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load settings for the chosen environment and run the command."""
    from budgetapp.db import SchemaMismatch
    from budgetapp.pgcluster import ClusterError
    from budgetapp.secrets import KeychainApprovalNeeded, SecretStoreError

    parser = build_parser()
    args, extra = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if extra and not getattr(args, "accepts_extra", False):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.extra = extra
    try:
        settings = load_settings(args.env)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verbose = bool(getattr(args, "verbose", False))
    setup_logging(settings, console_level=logging.INFO if verbose else logging.WARNING)
    install_excepthook()
    try:
        return int(args.handler(args, settings) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except KeychainApprovalNeeded as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return EXIT_KEYCHAIN
    except (ConfigError, ClusterError, SchemaMismatch, SecretStoreError) as exc:
        print(f"\nerror: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
