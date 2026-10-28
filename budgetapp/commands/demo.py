"""``demo create|drop|list`` — demo databases with realistic fake data (STUB).

The web-shell stage (F2b, demo agent) replaces this module wholesale, keeping these options:

* ``./run demo create NAME [--seed N]`` (Sandbox only): (re)creates ``budgetapp_test_demo_<NAME>``
  as ``budgetapp_test``, migrates it and fills it with about two years of consistent fake data.
* ``./run demo drop NAME`` and ``./run demo list``.

View one with ``./run serve --demo NAME [--port N]``.
"""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "demo",
        handler=not_built,
        help="create, drop and list demo databases with realistic fake data",
        description=__doc__,
    )
    parser.add_argument("action", choices=("create", "drop", "list"))
    parser.add_argument("name", nargs="?", help="demo name: a-z, 0-9 and _ (create and drop)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (create; default 1)")
