"""``start`` (what ``./run`` runs) and ``serve``: the web app on 127.0.0.1 (ARCHITECTURE §9, §10).

* ``./run`` = ``start``, Sandbox: start the database cluster if needed, wait for it, migrate the
  Sandbox database, warn when the Plaid keys are missing, then serve from the repository and open
  the browser at http://127.0.0.1:8766 once the server is up (``--no-browser`` skips that).
* ``./run live`` = ``start --env production``: prints the LIVE DATA banner and hands over to
  ``services.agents.launch_production`` (upgrade the installed copy if needed, then open the
  app from it).
* ``serve``: only the server (no migration, no browser). Sandbox on 8766; ``--env production`` on
  8765, only from the installed copy (the launchd web agent runs ``serve --env production
  --unattended``); ``--demo NAME [--port N]`` serves the demo database made by
  ``./run demo create NAME`` on 127.0.0.1:N (default 8800), with fake Plaid and no Keychain.

uvicorn binds ``settings.web_host``, which is always 127.0.0.1 (there is no host option), with no
``Server`` header, no proxy headers and no websockets. Its logs go through the app's redacting
handlers: warnings and errors to the terminal, everything (requests included, except in
production) to ``~/Library/Logs/budgetapp/web-<env>.log``.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import logging
import shutil
import socket
import sys
import threading
import time
import webbrowser
from types import ModuleType
from typing import TYPE_CHECKING, Any

from budgetapp import pgcluster, secrets
from budgetapp.commands import add_command
from budgetapp.config import LOOPBACK, ConfigError, Settings, in_test_process, scratch_dir_for_tests
from budgetapp.db import migrate_env, wait_for_db
from budgetapp.logsetup import setup_logging
from budgetapp.services import agents
from budgetapp.services.notify import NullNotifier

if TYPE_CHECKING:  # the web stack is imported only when serving
    import uvicorn
    from fastapi import FastAPI

log = logging.getLogger(__name__)

DEMO_PORT = 8800
#: Ports a demo server may not take: below 1024 needs root; 8765/8766 belong to the real apps.
RESERVED_PORTS = frozenset({8765, 8766})

LIVE_BANNER = """\
******************************************************************
  LIVE DATA: this is your real American Express account.
  Production database '{db}' · {url}
******************************************************************"""

PRODUCTION_FROM_CHECKOUT = (
    "Refusing to serve the PRODUCTION app from a git checkout ({repo}). Production runs only "
    "from the installed copy: use ./run live."
)
TEST_ENV_REFUSED = (
    "The test environment is for pytest only. Serve a demo database instead: "
    "./run serve --demo NAME (after ./run demo create NAME)."
)


def register(subparsers: Any) -> None:
    start = add_command(
        subparsers,
        "start",
        handler=run_start,
        help="start the app (what ./run does): database, migrations, web server, browser",
        description=__doc__,
    )
    start.add_argument("--no-browser", action="store_true", help="don't open the browser")
    serve = add_command(
        subparsers,
        "serve",
        handler=run_serve,
        help="serve the web app on 127.0.0.1 without opening a browser",
        description=__doc__,
    )
    serve.add_argument(
        "--demo", metavar="NAME", help="serve the demo database NAME (./run demo create NAME)"
    )
    serve.add_argument(
        "--port",
        type=int,
        metavar="N",
        help=f"port on 127.0.0.1 (--demo only; default {DEMO_PORT})",
    )
    serve.add_argument(
        "--unattended",
        action="store_true",
        help="for the launchd web agent: never show Keychain dialogs (a page explains the fix)",
    )


# --------------------------------------------------------------------------- commands


def run_start(args: argparse.Namespace, settings: Settings) -> int:
    if settings.is_test:
        print(f"error: {TEST_ENV_REFUSED}", file=sys.stderr)
        return 2
    if settings.is_production:
        print(LIVE_BANNER.format(db=settings.db_name, url=settings.app_url), flush=True)
        try:
            return int(agents.launch_production(settings) or 0)
        except NotImplementedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print("Starting Budget Tracker (Sandbox)", flush=True)
    print(f"  PostgreSQL: {pgcluster.start(settings)}", flush=True)
    if not wait_for_db(settings, timeout=60):
        print(
            f"error: the database did not become ready; see {settings.logs_dir}/postgres.log",
            file=sys.stderr,
        )
        return 1
    before, after = migrate_env(settings)
    change = "up to date" if before == after else f"{before or 'empty'} → {after}"
    print(f"  Sandbox database {settings.db_name}: {change}", flush=True)
    warning = plaid_key_warning(settings)
    if warning:
        print(warning)
    from budgetapp.web.app import create_app

    return run_server(
        create_app(settings),
        settings,
        open_browser=not args.no_browser,
        verbose=bool(getattr(args, "verbose", False)),
    )


def run_serve(args: argparse.Namespace, settings: Settings) -> int:
    verbose = bool(getattr(args, "verbose", False))
    if args.demo is not None:
        if settings.is_production:
            print("error: demo data is Sandbox-only; drop --env production.", file=sys.stderr)
            return 2
        port = DEMO_PORT if args.port is None else args.port
        return serve_demo(args.demo, port, cluster=settings, verbose=verbose)
    if args.port is not None:
        print(
            "error: --port works only with --demo (the Sandbox app always uses "
            f"{settings.web_port}).",
            file=sys.stderr,
        )
        return 2
    if settings.is_test:
        print(f"error: {TEST_ENV_REFUSED}", file=sys.stderr)
        return 2
    if settings.is_production:
        if settings.repo_dir is not None:
            print(
                f"error: {PRODUCTION_FROM_CHECKOUT.format(repo=settings.repo_dir)}", file=sys.stderr
            )
            return 2
        print(LIVE_BANNER.format(db=settings.db_name, url=settings.app_url), flush=True)
    if args.unattended:
        secrets.enable_unattended_mode()
    if not wait_for_db(settings, timeout=120 if settings.is_production else 15):
        if not settings.is_production:
            print(
                "error: the database is not running. Start it with ./run db start, or start "
                "everything with ./run",
                file=sys.stderr,
            )
            return 1
        log.warning("The database is not ready yet; serving anyway (pages say so until it is).")
    from budgetapp.web.app import create_app

    return run_server(create_app(settings), settings, verbose=verbose)


def load_demo_tools() -> tuple[ModuleType, ModuleType]:
    """``budgetapp.devtools.demo`` and ``.demo_gateway``: imported only when a demo is served."""
    return (
        importlib.import_module("budgetapp.devtools.demo"),
        importlib.import_module("budgetapp.devtools.demo_gateway"),
    )


def serve_demo(name: str, port: int, *, cluster: Settings, verbose: bool = False) -> int:
    """``serve --demo NAME``: the app on a demo database, with fake Plaid and no Keychain.

    ``cluster``: any settings for this Mac's cluster (only used to wait until it is up).
    """
    try:
        demo_tools, demo_gateway = load_demo_tools()
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("budgetapp.devtools"):
            raise
        print(f"error: demo data isn't available ({exc.name} is missing).", file=sys.stderr)
        return 2
    if port < 1024 or port in RESERVED_PORTS:
        print(
            f"error: pick another port for the demo: {port} is "
            + ("reserved for the real apps." if port in RESERVED_PORTS else "below 1024."),
            file=sys.stderr,
        )
        return 2
    if not wait_for_db(cluster, timeout=15):
        print("error: the database is not running. Start it with ./run db start", file=sys.stderr)
        return 1
    try:
        settings = demo_tools.demo_settings(name, port=port)
    except demo_tools.DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not settings.is_test:  # the contract: test-environment settings, budgetapp_test role
        raise ConfigError(f"demo_settings() returned {settings.env!r} settings")
    from budgetapp.web.app import create_app

    store = secrets.MemoryStore()
    secrets.use_store(store)  # nothing in this process reads or writes the real Keychain
    app = create_app(
        settings,
        gateway_factory=lambda _settings: demo_gateway.DemoPlaidGateway(),
        secret_store=store,
        notifier=NullNotifier(),
    )
    app.state.demo_name = name
    print(f"Demo data '{name}' (database {settings.db_name}). Nothing here is real.", flush=True)
    try:
        return run_server(app, settings, verbose=verbose)
    finally:
        if not in_test_process():  # the test-environment scratch folder belongs to this process
            shutil.rmtree(scratch_dir_for_tests(), ignore_errors=True)


# --------------------------------------------------------------------------- serving


def plaid_key_warning(settings: Settings) -> str | None:
    """A line for the terminal when the Plaid keys for ``settings.plaid_env`` are not saved."""
    accounts = (secrets.PLAID_CLIENT_ID, secrets.plaid_secret_key(settings.plaid_env))
    try:
        missing = [account for account in accounts if not secrets.get_secret(account)]
    except secrets.SecretStoreError as exc:
        return f"  ! Couldn't check the Plaid keys in Keychain: {exc}"
    if not missing:
        return None
    return (
        "  ! Plaid keys aren't saved yet, so linking and syncing won't work (everything else "
        "does). Save them with: ./run setup --keys-only"
    )


def port_available(host: str, port: int) -> bool:
    """True when nothing is listening on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def server_log_config() -> dict[str, Any]:
    """uvicorn's logging: its records go to the root logger's redacting handlers.

    Unlike ``logsetup.uvicorn_log_config`` (everything to the terminal), requests then land in
    ``web-<env>.log`` and the terminal shows only warnings and errors (all of it with ``-v``).
    """
    quiet = {"handlers": [], "level": "INFO", "propagate": True}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "loggers": {"uvicorn": quiet, "uvicorn.error": quiet, "uvicorn.access": quiet},
    }


def open_browser_when_ready(server: uvicorn.Server, url: str, *, timeout: float = 30.0) -> bool:
    """Open ``url`` in the default browser once ``server`` accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not server.should_exit:
        if server.started:
            webbrowser.open(url)
            return True
        time.sleep(0.1)
    return False


def run_server(
    app: FastAPI, settings: Settings, *, open_browser: bool = False, verbose: bool = False
) -> int:
    """Serve ``app`` on 127.0.0.1:``settings.web_port`` until Ctrl-C (or SIGTERM)."""
    import uvicorn

    host, port, url = settings.web_host, settings.web_port, settings.app_url
    if host != LOOPBACK:
        raise ConfigError("The web app may only listen on 127.0.0.1 (SEC-1).")
    if not port_available(host, port):
        print(
            f"error: 127.0.0.1:{port} is already in use. Is the app already running? Open {url}, "
            "or stop the other program and try again.",
            file=sys.stderr,
        )
        return 1
    setup_logging(
        settings,
        log_name=None if settings.is_test else f"web-{settings.env}",
        console_level=logging.INFO if verbose else logging.WARNING,
    )
    logging.getLogger("alembic").setLevel(logging.WARNING)  # the schema check's chatter
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        server_header=False,
        proxy_headers=False,
        ws="none",
        lifespan="on",
        log_config=server_log_config(),
        access_log=not settings.is_production,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    if open_browser:
        threading.Thread(
            target=open_browser_when_ready, args=(server, url), name="open-browser", daemon=True
        ).start()
    print(f"Budget Tracker is running at {url} (press Ctrl-C to stop)", flush=True)
    with contextlib.suppress(KeyboardInterrupt):  # uvicorn re-raises Ctrl-C after a clean stop
        server.run()
    print("Budget Tracker stopped.", flush=True)
    return 0 if server.started else 1
