"""CLI commands: one module per command, each with ``register(subparsers)``.

A command module adds its parser with :func:`add_command` (which accepts ``--env`` after the
command name too, e.g. ``./run start --env production``) and a handler
``handler(args, settings) -> int`` (the process exit code).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

NOT_BUILT = "This command isn't built yet."
Handler = Callable[[argparse.Namespace, Any], int]


def common_parent() -> argparse.ArgumentParser:
    """Options every command accepts after its name (``--env``, ``-v``)."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--env",
        choices=("sandbox", "production"),
        default=argparse.SUPPRESS,
        help="environment (default: $BUDGETAPP_ENV or sandbox)",
    )
    parent.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="more output"
    )
    return parent


def add_command(
    subparsers: Any,
    name: str,
    *,
    handler: Handler,
    help: str,
    description: str | None = None,
    accepts_extra: bool = False,
) -> argparse.ArgumentParser:
    """Add a sub-command; ``accepts_extra`` passes unknown arguments through as ``args.extra``."""
    parser = subparsers.add_parser(
        name,
        help=help,
        description=description or help,
        parents=[common_parent()],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=handler, accepts_extra=accepts_extra)
    return parser


def not_built(args: argparse.Namespace, settings: Any) -> int:
    """Handler for commands a later package implements."""
    print(NOT_BUILT)
    return 2
