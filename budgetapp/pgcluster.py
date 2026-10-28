"""The app's dedicated PostgreSQL 18 cluster (ARCHITECTURE §2).

Data directory ``~/Library/Application Support/budgetapp/postgres``, port 5434, a private 0700
Unix-socket directory, peer authentication (with an ident map) on the socket and scram-sha-256 on
loopback TCP. This module never touches the shared development clusters on ports 5432/5433 and
refuses to start or initialise when port 5434 belongs to a different data directory.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from psycopg import sql

from budgetapp import secrets
from budgetapp.config import FORBIDDEN_PG_PORTS, Settings
from budgetapp.db import admin_connect, is_ready, wait_for_db

log = logging.getLogger(__name__)

AGENT_LABEL = "local.budgetapp.postgres"
APP_ROLES = ("budgetapp_dev", "budgetapp_live")
TEST_ROLE = "budgetapp_test"
#: database → owner role
DATABASES = {"budgetapp_sandbox": "budgetapp_dev", "budgetapp": "budgetapp_live"}
SOCKET_PATH_MAX = 103  # macOS limit for a Unix socket path, in bytes
INCLUDE_FILE = "budgetapp.conf"
PG_MAJOR = "18"

INITDB_FLAGS = (
    "-E",
    "UTF8",
    "--locale-provider=builtin",
    "--builtin-locale=C.UTF-8",
    "--locale=C.UTF-8",
    "--auth-local=peer",
    "--auth-host=scram-sha-256",
)


class ClusterError(Exception):
    """The cluster cannot be initialised, started or stopped safely."""


@dataclass(frozen=True, slots=True)
class ClusterStatus:
    """What ``./run db status`` reports."""

    data_dir: Path
    socket_dir: Path
    port: int
    initialized: bool
    running: bool
    pid: int | None
    agent_loaded: bool
    port_conflict: str | None
    server_version: str | None


# --------------------------------------------------------------------------- helpers


def _server_env() -> dict[str, str]:
    """A minimal environment for initdb/pg_ctl. LC_ALL is required by Homebrew's PG 18 on macOS."""
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }


def _run(
    args: list[str], *, timeout: float = 120, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, env=_server_env(), check=False
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClusterError(f"{Path(args[0]).name} failed (exit {result.returncode}): {detail}")
    return result


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _check_port(settings: Settings) -> None:
    if settings.pg_port in FORBIDDEN_PG_PORTS:
        raise ClusterError(f"Refusing to manage a cluster on shared port {settings.pg_port}.")


def socket_path(settings: Settings) -> Path:
    return settings.pg_socket_dir / f".s.PGSQL.{settings.pg_port}"


def check_socket_path(settings: Settings) -> None:
    """macOS limits Unix socket paths to 103 bytes."""
    length = len(str(socket_path(settings)).encode())
    if length > SOCKET_PATH_MAX:
        raise ClusterError(
            f"The socket path {socket_path(settings)} is {length} bytes; macOS allows "
            f"{SOCKET_PATH_MAX}. Use a shorter BUDGETAPP_DATA_DIR."
        )


def is_initialized(settings: Settings) -> bool:
    return (settings.pg_data_dir / "PG_VERSION").is_file()


def postmaster_pid(settings: Settings) -> int | None:
    """PID recorded in ``postmaster.pid`` (``None`` if there is no such file)."""
    try:
        first = (settings.pg_data_dir / "postmaster.pid").read_text().splitlines()[0]
        return int(first.strip())
    except FileNotFoundError, IndexError, ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command(pid: int) -> str | None:
    """The command line of ``pid`` (``None`` when it is not running)."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    command = result.stdout.strip()
    return command or None


def listener_pids(port: int) -> list[int]:
    """PIDs listening on TCP ``port`` (via ``lsof``; empty if none or lsof is unavailable)."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return []
    return sorted({int(line) for line in result.stdout.split() if line.strip().isdigit()})


def is_our_postgres(pid: int, settings: Settings) -> bool:
    """True when ``pid`` is a postgres process started with our data directory."""
    command = process_command(pid)
    return bool(command and "postgres" in command and str(settings.pg_data_dir) in command)


def port_conflict(settings: Settings) -> str | None:
    """A description of a foreign server holding our port, or ``None``."""
    pids = listener_pids(settings.pg_port)
    if not pids:
        return None
    ours = [pid for pid in pids if is_our_postgres(pid, settings)]
    if ours and len(ours) == len(pids):
        return None
    foreign = [pid for pid in pids if pid not in ours]
    return (
        f"Port {settings.pg_port} is already in use by process(es) {foreign}, which are not the "
        f"budgetapp cluster at {settings.pg_data_dir}. Stop that server or free the port first."
    )


def agent_loaded(label: str = AGENT_LABEL) -> bool:
    """True when the launchd agent ``label`` is loaded in the GUI session."""
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------- configuration


def _quote_conf(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_conf(settings: Settings) -> str:
    """Contents of ``budgetapp.conf``, included from ``postgresql.conf``."""
    socket_dir = '"' + str(settings.pg_socket_dir).replace('"', '\\"') + '"'
    return (
        "# Managed by budgetapp: ./run setup rewrites this file. Do not edit by hand.\n"
        f"port = {settings.pg_port}\n"
        "listen_addresses = 'localhost'\n"
        f"unix_socket_directories = {_quote_conf(socket_dir)}\n"
        "unix_socket_permissions = 0700\n"
        "password_encryption = 'scram-sha-256'\n"
    )


def render_hba() -> str:
    """``pg_hba.conf``: peer (mapped) on the socket, scram on loopback TCP, nothing else."""
    return (
        "# Managed by budgetapp: ./run setup rewrites this file. Do not edit by hand.\n"
        "# TYPE  DATABASE  USER  ADDRESS        METHOD\n"
        "local   all       all                  peer map=localmap\n"
        "host    all       all   127.0.0.1/32   scram-sha-256\n"
        "host    all       all   ::1/128        scram-sha-256\n"
    )


def render_ident(os_user: str) -> str:
    """``pg_ident.conf``: the macOS user maps to itself (superuser) and to budgetapp_test."""
    return (
        "# Managed by budgetapp: ./run setup rewrites this file. Do not edit by hand.\n"
        "# MAPNAME   SYSTEM-USERNAME   PG-USERNAME\n"
        f"localmap    {os_user}    {os_user}\n"
        f"localmap    {os_user}    {TEST_ROLE}\n"
    )


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.read_text() == content:
            return False
    except FileNotFoundError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


def write_config(settings: Settings) -> bool:
    """Write our config files into the data directory; returns ``True`` if anything changed."""
    data = settings.pg_data_dir
    changed = _write_if_changed(data / INCLUDE_FILE, render_conf(settings))
    changed |= _write_if_changed(data / "pg_hba.conf", render_hba())
    changed |= _write_if_changed(data / "pg_ident.conf", render_ident(settings.os_user))
    main_conf = data / "postgresql.conf"
    include_line = f"include_if_exists = '{INCLUDE_FILE}'"
    text = main_conf.read_text()
    if include_line not in text.splitlines():
        with main_conf.open("a") as fh:
            fh.write(f"\n# budgetapp settings (port, sockets, auth)\n{include_line}\n")
        changed = True
    return changed


# --------------------------------------------------------------------------- lifecycle


def init_cluster(settings: Settings) -> bool:
    """Create the cluster if needed and (re)write its config. Idempotent.

    Returns ``True`` when a new cluster was created. Refuses when the port belongs to another data
    directory, or when an existing directory holds a different PostgreSQL major version.
    """
    _check_port(settings)
    check_socket_path(settings)
    conflict = port_conflict(settings)
    if conflict:
        raise ClusterError(conflict)
    _ensure_private_dir(settings.data_dir)
    _ensure_private_dir(settings.pg_socket_dir)
    _ensure_private_dir(settings.logs_dir)
    created = False
    if is_initialized(settings):
        version = (settings.pg_data_dir / "PG_VERSION").read_text().strip()
        if version != PG_MAJOR:
            raise ClusterError(
                f"{settings.pg_data_dir} holds a PostgreSQL {version} cluster; expected {PG_MAJOR}."
            )
    else:
        if settings.pg_data_dir.exists() and any(settings.pg_data_dir.iterdir()):
            raise ClusterError(
                f"{settings.pg_data_dir} exists, is not empty and is not a PostgreSQL cluster."
            )
        log.info("initialising the cluster at %s", settings.pg_data_dir)
        _run(
            [
                settings.pg_bin("initdb"),
                "-D",
                str(settings.pg_data_dir),
                "-U",
                settings.os_user,
                *INITDB_FLAGS,
            ],
            timeout=300,
        )
        created = True
    os.chmod(settings.pg_data_dir, 0o700)
    changed = write_config(settings)
    if changed and not created and is_running(settings):
        reload(settings)
    return created


def is_running(settings: Settings) -> bool:
    """True when our cluster accepts connections on its private socket (``pg_isready``)."""
    return is_ready(settings)


def start(settings: Settings, *, timeout: int = 60) -> str:
    """Start the cluster if it is not running; returns what happened.

    Prefers ``launchctl kickstart`` when the ``local.budgetapp.postgres`` agent is loaded, else runs
    ``pg_ctl -w start`` with ``LC_ALL=C.UTF-8`` and logs to ``<logs_dir>/postgres.log``.
    """
    _check_port(settings)
    if is_running(settings):
        return "already running"
    conflict = port_conflict(settings)
    if conflict:
        raise ClusterError(conflict)
    if not is_initialized(settings):
        raise ClusterError(f"No cluster at {settings.pg_data_dir}. Run ./run setup first.")
    check_socket_path(settings)
    _ensure_private_dir(settings.pg_socket_dir)
    _ensure_private_dir(settings.logs_dir)
    if agent_loaded():
        subprocess.run(
            ["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{AGENT_LABEL}"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if wait_for_db(settings, timeout=timeout):
            return "started (launchd agent)"
        raise ClusterError(
            "The launchd postgres agent did not start the cluster; see postgres.log."
        )
    log_file = settings.logs_dir / "postgres.log"
    if not log_file.exists():
        os.close(os.open(log_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600))
    _run(
        [
            settings.pg_bin("pg_ctl"),
            "-D",
            str(settings.pg_data_dir),
            "-l",
            str(log_file),
            "-w",
            "-t",
            str(timeout),
            "start",
        ],
        timeout=timeout + 30,
    )
    if not wait_for_db(settings, timeout=15):
        raise ClusterError(f"The cluster did not become ready; see {log_file}.")
    return "started"


def stop(settings: Settings, *, mode: str = "fast", timeout: int = 60) -> str:
    """Stop the cluster with ``pg_ctl stop -m fast`` (exit 0: launchd's KeepAlive won't restart)."""
    _check_port(settings)
    pid = postmaster_pid(settings)
    if not is_initialized(settings) or pid is None or not _pid_alive(pid):
        return "not running"
    _run(
        [
            settings.pg_bin("pg_ctl"),
            "-D",
            str(settings.pg_data_dir),
            "-m",
            mode,
            "-w",
            "-t",
            str(timeout),
            "stop",
        ],
        timeout=timeout + 30,
    )
    return "stopped"


def reload(settings: Settings) -> None:
    """Reload the configuration (``pg_hba.conf``/``pg_ident.conf`` changes take effect)."""
    _run([settings.pg_bin("pg_ctl"), "-D", str(settings.pg_data_dir), "reload"], timeout=30)


def server_version(settings: Settings) -> str | None:
    """``SHOW server_version`` via the admin connection (``None`` when unavailable)."""
    try:
        with admin_connect(settings) as conn:
            row = conn.execute("SHOW server_version").fetchone()
    except Exception:
        return None
    return str(row[0]) if row else None


def status(settings: Settings) -> ClusterStatus:
    """A snapshot of the cluster's state for ``./run db status`` and doctor."""
    running = is_running(settings)
    return ClusterStatus(
        data_dir=settings.pg_data_dir,
        socket_dir=settings.pg_socket_dir,
        port=settings.pg_port,
        initialized=is_initialized(settings),
        running=running,
        pid=postmaster_pid(settings),
        agent_loaded=agent_loaded(),
        port_conflict=None if running else port_conflict(settings),
        server_version=server_version(settings) if running else None,
    )


# --------------------------------------------------------------------------- roles and databases


def _role_exists(conn, role: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def _database_exists(conn, name: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None
    )


def ensure_roles_and_databases(
    settings: Settings, *, store: secrets.SecretStore | None = None
) -> list[str]:
    """Create or repair roles and databases; returns a list of the actions taken. Idempotent.

    * ``budgetapp_dev`` / ``budgetapp_live``: LOGIN, no other privileges, with a generated password
      kept in Keychain (``db.password.<role>``). The password is hashed client-side
      (``PGconn.encrypt_password``) before ``ALTER ROLE``, so the plaintext never reaches the
      server, its logs or a process argument.
    * ``budgetapp_test``: LOGIN CREATEDB, not superuser, no password (peer-mapped).
    * ``budgetapp_sandbox`` owned by ``budgetapp_dev`` and ``budgetapp`` owned by
      ``budgetapp_live``, with ``CONNECT`` revoked from ``PUBLIC``.
    """
    actions: list[str] = []
    attributes = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    with admin_connect(settings) as conn:
        for role in APP_ROLES:
            verb = "ALTER" if _role_exists(conn, role) else "CREATE"
            conn.execute(sql.SQL(f"{verb} ROLE {{}} {attributes}").format(sql.Identifier(role)))
            if verb == "CREATE":
                actions.append(f"created role {role}")
            key = secrets.db_password_key(role)
            password = secrets.get_secret(key, store=store)
            if not password:
                password = secrets.generate_password()
                secrets.set_secret(key, password, store=store)
                actions.append(f"saved a new password for {role} in Keychain")
            hashed = conn.pgconn.encrypt_password(
                password.encode(), role.encode(), b"scram-sha-256"
            ).decode()
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(hashed)
                )
            )

        test_attributes = "LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        verb = "ALTER" if _role_exists(conn, TEST_ROLE) else "CREATE"
        conn.execute(
            sql.SQL(f"{verb} ROLE {{}} {test_attributes}").format(sql.Identifier(TEST_ROLE))
        )
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD NULL").format(sql.Identifier(TEST_ROLE)))
        if verb == "CREATE":
            actions.append(f"created role {TEST_ROLE}")

        for name, owner in DATABASES.items():
            if _database_exists(conn, name):
                conn.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(name), sql.Identifier(owner)
                    )
                )
            else:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                        sql.Identifier(name), sql.Identifier(owner)
                    )
                )
                actions.append(f"created database {name} (owner {owner})")
            conn.execute(
                sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(name)
                )
            )
    return actions


def exclude_from_time_machine(path: Path) -> bool:
    """Exclude ``path`` from Time Machine (sticky ``tmutil addexclusion``); ``False`` on failure."""
    try:
        subprocess.run(
            ["/usr/bin/tmutil", "addexclusion", str(path)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        result = subprocess.run(
            ["/usr/bin/tmutil", "isexcluded", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return "[Excluded]" in result.stdout
