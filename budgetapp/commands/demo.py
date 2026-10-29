"""``demo create|drop|list`` — demo databases with realistic, made-up card data (Sandbox only).

* ``./run demo create NAME [--seed N] [--today YYYY-MM-DD]``: (re)creates the database
  ``budgetapp_test_demo_<NAME>`` as ``budgetapp_test``, migrates it and fills it with about two
  years of consistent fake data (a linked Amex card, ~2,500 transactions, statements, budgets,
  rules, sync history and alerts). The same seed and date always give the same data.
* ``./run demo drop NAME`` and ``./run demo list``.

View one with ``./run serve --demo NAME [--port N]`` (default port 8800). Nothing here touches
your Sandbox or production data, Keychain or Plaid. See budgetapp/devtools/README.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Any

from budgetapp.commands import add_command
from budgetapp.config import Settings


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from exc


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "demo",
        handler=run,
        help="create, drop and list demo databases with realistic fake data",
        description=__doc__,
    )
    parser.add_argument("action", choices=("create", "drop", "list"))
    parser.add_argument("name", nargs="?", help="demo name: a-z, 0-9 and _ (create and drop)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (create; default 1)")
    parser.add_argument(
        "--today",
        type=_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="pretend today is this date (create; default: today)",
    )


def run(args: argparse.Namespace, settings: Settings) -> int:
    if settings.is_production:
        print(
            "error: demo data is Sandbox-only and never touches production; drop --env production.",
            file=sys.stderr,
        )
        return 2
    from budgetapp.devtools import demo

    try:
        if args.action == "list":
            names = demo.list_demos()
            if not names:
                print("No demo databases. Make one with ./run demo create NAME")
            for name in names:
                print(f"{name}  ({demo.DEMO_DB_PREFIX}{name})")
            return 0
        if args.name is None:
            print(
                f"error: demo {args.action} needs a NAME, e.g. ./run demo {args.action} budgets",
                file=sys.stderr,
            )
            return 2
        demo.demo_db_name(args.name)
    except demo.DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if args.action != "list" else 1
    try:
        if args.action == "drop":
            existed = demo.drop_demo(args.name)
            print(f"Dropped demo '{args.name}'." if existed else f"No demo named '{args.name}'.")
            return 0
        print(f"Creating demo data '{args.name}' (seed {args.seed})…")
        summary = demo.create_demo(args.name, seed=args.seed, today=args.today)
    except demo.DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in summary.lines():
        print(line)
    print(f"View it: ./run serve --demo {args.name} --port {demo.DEMO_PORT}")
    return 0
