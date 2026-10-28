"""``db start|stop|status|psql|url`` — manage the dedicated PostgreSQL cluster.

``psql`` connects as the environment's role (TCP + scram, password from Keychain passed in the
child's environment, never on the command line); ``--admin`` connects as the cluster superuser over
the private socket. Extra arguments go to psql, e.g. ``./run db psql -c "select 1"``.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from budgetapp import pgcluster, secrets
from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.db import database_url


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "db",
        handler=run,
        help="manage the database cluster: start | stop | status | psql | url",
        accepts_extra=True,
    )
    parser.add_argument("action", choices=("start", "stop", "status", "psql", "url"))
    parser.add_argument(
        "--admin",
        action="store_true",
        help="psql: connect as the cluster superuser over the private socket",
    )


def psql_command(settings: Settings, *, admin: bool = False) -> tuple[list[str], dict[str, str]]:
    """``(argv, extra_env)`` for psql; the password only ever travels in the environment."""
    psql = settings.pg_bin("psql")
    if admin or settings.env == "test":
        user = settings.os_user if admin else settings.db_role
        dbname = "postgres" if admin else settings.db_name
        conninfo = (
            f"host='{settings.pg_socket_dir}' port={settings.pg_port} dbname={dbname} user={user}"
        )
        return [psql, "-X", conninfo], {}
    password = secrets.require_secret(
        secrets.db_password_key(settings.db_role),
        hint="Run ./run setup to create the database roles.",
    )
    conninfo = (
        f"host={settings.pg_host} port={settings.pg_port} dbname={settings.db_name} "
        f"user={settings.db_role} require_auth=scram-sha-256 sslmode=disable"
    )
    return [psql, "-X", conninfo], {"PGPASSWORD": password}


def _print_status(settings: Settings) -> int:
    status = pgcluster.status(settings)
    state = "running" if status.running else "stopped"
    print(f"PostgreSQL cluster: {state}")
    print(f"  data directory : {status.data_dir}{'' if status.initialized else ' (not created)'}")
    print(f"  port           : 127.0.0.1:{status.port} (socket {status.socket_dir})")
    if status.pid:
        print(f"  postmaster pid : {status.pid}")
    if status.server_version:
        print(f"  server version : {status.server_version}")
    print(f"  launchd agent  : {'loaded' if status.agent_loaded else 'not installed'}")
    print(f"  this env       : {settings.env} → database {settings.db_name} as {settings.db_role}")
    if status.port_conflict:
        print(f"  WARNING        : {status.port_conflict}")
    return 0 if status.running else 3


def run(args: argparse.Namespace, settings: Settings) -> int:
    action = args.action
    if args.extra and action != "psql":
        print(f"error: unexpected arguments for 'db {action}': {' '.join(args.extra)}")
        return 2
    if action == "start":
        print(f"PostgreSQL: {pgcluster.start(settings)}")
        return 0
    if action == "stop":
        print(f"PostgreSQL: {pgcluster.stop(settings)}")
        return 0
    if action == "status":
        return _print_status(settings)
    if action == "url":
        print(database_url(settings, admin=args.admin).render_as_string(hide_password=True))
        return 0
    if not pgcluster.is_running(settings):
        print("The database is not running. Start it with: ./run db start")
        return 1
    argv, extra_env = psql_command(settings, admin=args.admin)
    os.execve(argv[0], [*argv, *args.extra], {**os.environ, **extra_env})
    return 0  # pragma: no cover - execve does not return
