"""``start`` (what ``./run`` runs) and ``serve`` — built by the web-shell package (F2b).

``start``: start the cluster if needed, wait for the database, migrate the Sandbox database, serve
from the repository and open the browser; with ``--env production`` (``./run live``) it calls
``services.agents.launch_production``. ``serve``: uvicorn on ``settings.web_host:web_port`` only;
``serve --demo NAME [--port N]`` serves a demo database (``./run demo create NAME``) on
127.0.0.1:N (default 8800).
"""

from __future__ import annotations

from typing import Any

from budgetapp.commands import add_command, not_built


def register(subparsers: Any) -> None:
    start = add_command(
        subparsers,
        "start",
        handler=not_built,
        help="start the app (what ./run does): database, migrations, web server, browser",
    )
    start.add_argument("--no-browser", action="store_true", help="don't open the browser")
    serve = add_command(
        subparsers,
        "serve",
        handler=not_built,
        help="serve the web app on 127.0.0.1 without opening a browser",
    )
    serve.add_argument(
        "--demo", metavar="NAME", help="serve the demo database NAME (./run demo create NAME)"
    )
    serve.add_argument(
        "--port", type=int, metavar="N", help="port on 127.0.0.1 (--demo only; default 8800)"
    )
