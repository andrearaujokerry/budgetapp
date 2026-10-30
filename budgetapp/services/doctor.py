"""``./run doctor``: security (SEC-1..6) and health checks for one environment.

Every check reports PASS, WARN or FAIL (INFO for facts that need no action) with a one-line fix,
and the command exits non-zero when anything FAILs. Doctor only reads: it never changes a file,
the database, launchd or the Keychain. External programs run through one runner (tests replace
it), and only stable outputs are parsed (RESEARCH T5.6): ``fdesetup isactive`` plus the status
line, ``launchctl`` exit statuses (``print`` output is not an API), ``security
show-keychain-info`` on STDERR, ``lsof``/``netstat`` for listeners, ``tmutil`` and ``diskutil
-plist`` for Time Machine.

Run in Terminal it reads every Keychain item of BOTH environments, so after a ``brew upgrade`` of
Python macOS can ask once per item and the user clicks *Always Allow* (ARCHITECTURE §3). Values
are never shown.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import os
import plistlib
import re
import subprocess
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from budgetapp import clock, secrets
from budgetapp.config import Settings, in_test_process, protected_folders
from budgetapp.services import agents
from budgetapp.services import backup as backups

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
STATUSES = (PASS, WARN, FAIL, INFO)
SOCKET_PATH_MAX = 103
SCRIPT_EDITOR = "com.apple.ScriptEditor2"
STALE_LOCK_LINE = 'lock file "postmaster.pid" already exists'
LOG_TAIL_BYTES = 256 * 1024
#: A Plaid access or public token (``access-sandbox-<uuid>``); the UUID must look random.
TOKEN_PATTERN = (
    r"(access|public)-(sandbox|production|development)-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
LOOPBACK_LISTEN = frozenset({"localhost", "127.0.0.1", "::1"})
HBA_RULES = frozenset(
    {
        ("local", "all", "all", "peer", "map=localmap"),
        ("host", "all", "all", "127.0.0.1/32", "scram-sha-256"),
        ("host", "all", "all", "::1/128", "scram-sha-256"),
    }
)

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True, slots=True)
class Check:
    """One line of the report."""

    section: str
    status: str
    title: str
    detail: str | None = None
    fix: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")


@dataclass(frozen=True, slots=True)
class Report:
    env: str
    database: str
    checks: tuple[Check, ...]

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def count(self, status: str) -> int:
        return sum(1 for c in self.checks if c.status == status)


@dataclass(frozen=True, slots=True)
class Result:
    """What a program printed (``returncode`` 127: could not run; 124: timed out)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class DoctorContext:
    """Everything the checks read; tests replace the runner, store, database and paths."""

    settings: Settings
    layout: agents.Layout
    runner: Runner
    store: secrets.SecretStore | None = None
    session_factory: Callable[[], Session] | None = None
    now: dt.datetime = field(default_factory=clock.now)
    user: str = field(default_factory=getpass.getuser)
    http_status: Callable[[str], int | None] | None = None
    _db: tuple[bool, str | None] | None = field(default=None, repr=False)
    _db_fix: str = field(default="./run db start", repr=False)
    _pg_ready: bool | None = field(default=None, repr=False)

    @classmethod
    def for_settings(cls, settings: Settings, **overrides: Any) -> DoctorContext:
        layout = overrides.pop("layout", None) or agents.Layout.for_settings(settings)
        runner = overrides.pop("runner", None)
        if runner is None:
            if in_test_process():
                raise RuntimeError("tests must pass a runner to doctor")
            runner = subprocess.run
        return cls(settings=settings, layout=layout, runner=runner, **overrides)

    @property
    def home(self) -> Path:
        return self.layout.home

    def run(self, argv: Sequence[str], *, timeout: float = 20) -> Result:
        """Run a read-only program; never raises."""
        try:
            done = self.runner(
                list(argv), capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return Result(124, "", f"{Path(argv[0]).name} timed out")
        except OSError as exc:
            return Result(127, "", f"{Path(argv[0]).name}: {exc.strerror or exc}")
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    # ---------------------------------------------------------------- database

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        if self.session_factory is None:
            if in_test_process() and not self.settings.is_test:
                raise RuntimeError("tests must pass a session factory for non-test settings")
            from sqlalchemy.pool import NullPool

            from budgetapp import db

            engine = db.make_engine(self.settings, poolclass=NullPool)
            self.session_factory = db.make_session_factory(engine)
        session = self.session_factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def database(self) -> tuple[bool, str | None]:
        """``(reachable, problem)``, checked once (a Keychain prompt may appear here)."""
        if self._db is None:
            try:
                with self.session() as session:
                    session.execute(text("SELECT 1"))
                self._db = (True, None)
            except secrets.KeychainApprovalNeeded:
                self._db = (False, "macOS didn't allow reading the database password from Keychain")
                self._db_fix = "Run ./run doctor in Terminal again and click Always Allow."
            except secrets.MissingSecretError:
                self._db = (False, "the database password isn't in Keychain")
                self._db_fix = "./run setup"
            except Exception as exc:  # any connection problem is reported, never raised
                problem = _first_line(str(getattr(exc, "orig", None) or exc))
                self._db = (False, problem)
                if "password authentication failed" in problem or "does not exist" in problem:
                    self._db_fix = "./run setup (it creates the database and resets its password)"
        return self._db

    @property
    def db_fix(self) -> str:
        """The one-line fix for the database problem :meth:`database` found."""
        return self._db_fix

    def pg_ready(self) -> bool:
        if self._pg_ready is None:
            result = self.run(
                [
                    self.settings.pg_bin("pg_isready"),
                    "-q",
                    "-h",
                    str(self.settings.pg_socket_dir),
                    "-p",
                    str(self.settings.pg_port),
                    "-t",
                    "5",
                ]
            )
            self._pg_ready = result.returncode == 0
        return self._pg_ready


def _first_line(value: str) -> str:
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    return (lines[0] if lines else "no detail")[:200]


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def _short(path: Path | str, home: Path) -> str:
    text_path = str(path)
    home_text = str(home)
    return "~" + text_path[len(home_text) :] if text_path.startswith(home_text + "/") else text_path


def _within(path: Path, parent: Path) -> bool:
    try:
        Path(str(path).casefold()).relative_to(Path(str(parent).casefold()))
    except ValueError:
        return False
    return True


def _mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _hours(delta: dt.timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(1, int(delta.total_seconds() // 60))} min"
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours // 24)} days"


# =========================================================================== checks
# Each check is ``check(ctx) -> Iterable[Check]``; CHECKS lists them in report order.


def check_filevault(ctx: DoctorContext) -> Iterable[Check]:
    section = "This Mac"
    active = ctx.run(["/usr/bin/fdesetup", "isactive"])
    status = ctx.run(["/usr/bin/fdesetup", "status"])
    first = _first_line(status.stdout) if status.stdout.strip() else ""
    is_active = active.returncode == 0 and active.stdout.strip() == "true"
    if is_active and first == "FileVault is On.":
        yield Check(section, PASS, "FileVault is on (SEC-6)")
    elif is_active:
        yield Check(
            section,
            WARN,
            "FileVault is changing state",
            detail=status.stdout.strip() or None,
            fix="Wait until System Settings → Privacy & Security → FileVault says it is on.",
        )
    else:
        yield Check(
            section,
            FAIL,
            "FileVault is off (SEC-6)",
            detail=first or None,
            fix="Turn it on in System Settings → Privacy & Security → FileVault.",
        )


def check_other_users(ctx: DoctorContext) -> Iterable[Check]:
    section = "This Mac"
    result = ctx.run(["/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"])
    if result.returncode != 0:
        yield Check(section, WARN, "Couldn't list the Mac's user accounts", detail=result.stderr)
        return
    others = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) >= 501 and parts[0] != ctx.user:
            others.append(parts[0])
    if others:
        yield Check(
            section,
            WARN,
            f"Other macOS accounts exist: {', '.join(sorted(others))}",
            detail="The app has no login: anyone logged in on this Mac can open it on 127.0.0.1.",
            fix="Remove accounts nobody uses (System Settings → Users & Groups).",
        )
    else:
        yield Check(section, PASS, "No other macOS user accounts on this Mac")


def _app_paths(ctx: DoctorContext) -> list[tuple[str, Path]]:
    s, layout = ctx.settings, ctx.layout
    return [
        ("data folder", s.data_dir),
        ("database cluster", s.pg_data_dir),
        ("socket folder", s.pg_socket_dir),
        ("backup folder", s.backups_dir),
        ("production install", s.install_dir),
        ("log folder", s.logs_dir),
        ("state folder", s.state_dir),
        ("agent scripts folder", layout.bin_dir),
    ]


def check_paths(ctx: DoctorContext) -> Iterable[Check]:
    section = "This Mac"
    folders = protected_folders(ctx.home)
    bad = []
    for name, path in _app_paths(ctx):
        real = Path(os.path.realpath(path))
        for folder in folders:
            if _within(real, folder) or _within(real, Path(os.path.realpath(folder))):
                bad.append((name, path, folder))
                break
    for name, path, folder in bad:
        yield Check(
            section,
            FAIL,
            f"The {name} is inside {_short(folder, ctx.home)}",
            detail=f"{path}: macOS blocks launchd jobs there, and iCloud may sync it.",
            fix="Use ~/Library/Application Support/budgetapp (unset BUDGETAPP_DATA_DIR / "
            "BUDGETAPP_LOGS_DIR), then ./run setup.",
        )
    if not bad:
        yield Check(
            section, PASS, "App data is outside Desktop, Documents, Downloads and iCloud folders"
        )
    repo = ctx.layout.repo_dir
    if repo is None:
        return
    for folder_name, key in (
        ("Desktop", "FXICloudDriveDesktop"),
        ("Documents", "FXICloudDriveDocuments"),
    ):
        if not _within(Path(os.path.realpath(repo)), ctx.home / folder_name):
            continue
        value = ctx.run(["/usr/bin/defaults", "read", "com.apple.finder", key])
        if value.returncode == 0 and value.stdout.strip() == "1":
            yield Check(
                section,
                WARN,
                f"The code folder is in ~/{folder_name}, which iCloud syncs",
                detail=f"{repo} (Desktop & Documents sync is on). Your data is not there, but the "
                "code and any CSV you save next to it are uploaded.",
                fix=f"Move the folder out of ~/{folder_name} (e.g. ~/Developer), or turn off "
                "Desktop & Documents in System Settings → Apple Account → iCloud → iCloud Drive.",
            )


def check_permissions(ctx: DoctorContext) -> Iterable[Check]:
    section = "This Mac"
    s = ctx.settings
    problems = []
    for name, path in (
        ("data folder", s.data_dir),
        ("database cluster", s.pg_data_dir),
        ("state folder", s.state_dir),
        ("logs folder", s.logs_dir),
    ):
        mode = _mode(path)
        if mode is not None and mode & 0o077:
            problems.append((name, path, mode))
    for name, path, mode in problems:
        yield Check(
            section,
            FAIL,
            f"The {name} is readable by others (mode {mode:o})",
            detail=str(path),
            fix=f"chmod 700 {_quote(path)}",
        )
    if not problems:
        yield Check(section, PASS, "App folders are private to you (0700)")


# --------------------------------------------------------------------------- PostgreSQL


def check_postgres_running(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    if ctx.pg_ready():
        yield Check(section, PASS, f"PostgreSQL accepts connections on port {ctx.settings.pg_port}")
    else:
        fix = "./run db start"
        if ctx.settings.is_production and ctx.layout.plist_path("postgres").exists():
            fix = (
                f"launchctl kickstart gui/{ctx.layout.uid}/{agents.label('postgres')} "
                "(or ./run db start)"
            )
        yield Check(
            section,
            FAIL,
            "PostgreSQL isn't running",
            detail=f"Logs: {ctx.settings.logs_dir / 'postgres.log'}",
            fix=fix,
        )


def check_database(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    reachable, problem = ctx.database()
    s = ctx.settings
    if reachable:
        yield Check(section, PASS, f"Connected to {s.db_name} as {s.db_role}")
        return
    yield Check(
        section,
        FAIL,
        f"Can't connect to {s.db_name} as {s.db_role}",
        detail=problem,
        fix=ctx.db_fix,
    )


def _show(ctx: DoctorContext, name: str) -> str | None:
    with ctx.session() as session:
        return session.execute(text(f"SHOW {name}")).scalar()


def _conf_value(ctx: DoctorContext, name: str) -> str | None:
    try:
        content = (ctx.settings.pg_data_dir / "budgetapp.conf").read_text()
    except OSError:
        return None
    match = re.search(rf"^\s*{name}\s*=\s*'?([^'\n#]*)'?", content, re.M)
    return match.group(1).strip() if match else None


def check_postgres_settings(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    reachable, _ = ctx.database()
    source = "running server" if reachable else "budgetapp.conf"
    getter = (lambda n: _show(ctx, n)) if reachable else (lambda n: _conf_value(ctx, n))
    listen = getter("listen_addresses")
    permissions = getter("unix_socket_permissions")
    fix = "./run setup (it rewrites budgetapp.conf), then ./run db stop && ./run db start"
    if listen is None:
        yield Check(section, WARN, "Couldn't read listen_addresses", detail=f"from the {source}")
    else:
        addresses = {a.strip() for a in listen.split(",") if a.strip()}
        if addresses <= LOOPBACK_LISTEN:
            yield Check(
                section, PASS, f"PostgreSQL listens on loopback only ({listen or 'socket only'})"
            )
        else:
            yield Check(
                section,
                FAIL,
                f"PostgreSQL listens on {listen} (SEC-1)",
                detail="Other computers could try to connect.",
                fix=fix,
            )
    if permissions is None:
        yield Check(
            section, WARN, "Couldn't read unix_socket_permissions", detail=f"from the {source}"
        )
    elif permissions.lstrip("0") == "700":
        yield Check(section, PASS, "The database socket is private (0700)")
    else:
        yield Check(section, FAIL, f"unix_socket_permissions is {permissions}", fix=fix)


def check_pg_hba(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    path = ctx.settings.pg_data_dir / "pg_hba.conf"
    try:
        content = path.read_text()
    except OSError as exc:
        yield Check(section, WARN, "Couldn't read pg_hba.conf", detail=f"{path}: {exc.strerror}")
        return
    rules = []
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rules.append(tuple(line.split()))
    fix = "./run setup rewrites it; then ./run db stop && ./run db start"
    trust = [r for r in rules if "trust" in r]
    unexpected = [r for r in rules if r not in HBA_RULES]
    if trust:
        yield Check(
            section,
            FAIL,
            "pg_hba.conf allows passwordless 'trust' logins",
            detail="; ".join(" ".join(r) for r in trust),
            fix=fix,
        )
    elif unexpected:
        yield Check(
            section,
            FAIL,
            "pg_hba.conf has rules other than the socket peer map and loopback scram",
            detail="; ".join(" ".join(r) for r in unexpected),
            fix=fix,
        )
    elif ("local", "all", "all", "peer", "map=localmap") not in rules:
        yield Check(section, FAIL, "pg_hba.conf has no peer rule for the socket", fix=fix)
    else:
        yield Check(
            section, PASS, "pg_hba.conf: peer (mapped) on the socket, scram-sha-256 on loopback"
        )


def check_socket_dir(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    s = ctx.settings
    mode = _mode(s.pg_socket_dir)
    socket = s.pg_socket_dir / f".s.PGSQL.{s.pg_port}"
    length = len(str(socket).encode())
    if mode is None:
        yield Check(
            section,
            FAIL,
            "The private socket folder is missing",
            detail=str(s.pg_socket_dir),
            fix="./run setup",
        )
        return
    if mode != 0o700:
        yield Check(
            section,
            FAIL,
            f"The socket folder is mode {mode:o}, not 700",
            detail=str(s.pg_socket_dir),
            fix=f"chmod 700 {_quote(s.pg_socket_dir)}",
        )
    elif length > SOCKET_PATH_MAX:
        yield Check(
            section,
            FAIL,
            f"The socket path is {length} bytes; macOS allows {SOCKET_PATH_MAX}",
            fix="Use a shorter BUDGETAPP_DATA_DIR.",
        )
    else:
        yield Check(section, PASS, "Private socket folder (0700)")


def _netstat_listeners(ctx: DoctorContext, port: int) -> list[str] | None:
    """Local addresses LISTENing on ``port`` (``None`` when netstat failed)."""
    result = ctx.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"])
    if result.returncode != 0:
        return None
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[5] == "LISTEN" and parts[3].endswith(f".{port}"):
            found.append(parts[3])
    return found


def _loopback(address: str, port: int) -> bool:
    return address in (f"127.0.0.1.{port}", f"::1.{port}")


def _postmaster_pid(ctx: DoctorContext) -> int | None:
    try:
        first = (ctx.settings.pg_data_dir / "postmaster.pid").read_text().splitlines()[0]
        return int(first.strip())
    except FileNotFoundError, IndexError, ValueError, OSError:
        return None


def check_postgres_listeners(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    port = ctx.settings.pg_port
    if not ctx.pg_ready():
        yield Check(section, INFO, "Listener check skipped: PostgreSQL isn't running")
        return
    bad: list[str] = []
    pid = _postmaster_pid(ctx)
    if pid is not None:
        lsof = ctx.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"]
        )
        for line in lsof.stdout.splitlines():
            if line.startswith("n"):
                address = line[1:]
                if address not in (f"127.0.0.1:{port}", f"[::1]:{port}"):
                    bad.append(address)
    listeners = _netstat_listeners(ctx, port)
    if listeners is None:
        yield Check(section, WARN, "Couldn't list network listeners (netstat failed)")
        return
    bad += [a for a in listeners if not _loopback(a, port)]
    if bad:
        yield Check(
            section,
            FAIL,
            f"Port {port} is open beyond this Mac: {', '.join(sorted(set(bad)))} (SEC-1)",
            fix="./run setup (listen_addresses = 'localhost'), then ./run db stop && "
            "./run db start",
        )
    else:
        shown = ", ".join(listeners) if listeners else "socket only"
        yield Check(section, PASS, f"Only this Mac can reach port {port} ({shown})")


_VERSION = re.compile(r"(\d+)\.(\d+)")


def _version(textual: str | None) -> tuple[int, int] | None:
    match = _VERSION.search(textual or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_postgres_versions(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    reachable, _ = ctx.database()
    if not reachable:
        yield Check(section, INFO, "Version check skipped: no database connection")
        return
    server = _version(_show(ctx, "server_version"))
    binary = _version(ctx.run([ctx.settings.pg_bin("postgres"), "--version"]).stdout)
    dump = _version(ctx.run([ctx.settings.pg_bin("pg_dump"), "--version"]).stdout)
    if server is None or binary is None:
        yield Check(section, WARN, "Couldn't compare the server and program versions")
        return
    server_text, binary_text = f"{server[0]}.{server[1]}", f"{binary[0]}.{binary[1]}"
    if server[0] != binary[0]:
        yield Check(
            section,
            FAIL,
            f"The server runs PostgreSQL {server_text} but {binary_text} is installed",
            detail="A major upgrade needs a dump and restore (or pg_upgrade).",
            fix="Reinstall postgresql@18 (brew install postgresql@18).",
        )
    elif server != binary:
        yield Check(
            section,
            WARN,
            f"The server still runs {server_text}; {binary_text} is installed (brew upgrade)",
            fix="Restart the database: ./run db stop && ./run db start (with the agent: pg_ctl "
            "stop -m fast, then launchctl kickstart).",
        )
    else:
        yield Check(
            section, PASS, f"PostgreSQL {server_text}: the server matches the installed programs"
        )
    if dump is None or dump[0] < server[0]:
        yield Check(
            section,
            FAIL,
            "pg_dump can't be run, so backups would fail"
            if dump is None
            else "pg_dump is older than the server, so backups would fail",
            fix="Check BUDGETAPP_PG_BIN: it must be the PostgreSQL 18 bin folder "
            f"({ctx.settings.pg_bin_dir}).",
        )


def check_stale_pid(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    pidfile = ctx.settings.pg_data_dir / "postmaster.pid"
    log_path = ctx.settings.logs_dir / "postgres.log"
    mentioned = False
    try:
        with log_path.open("rb") as fh:
            fh.seek(max(0, log_path.stat().st_size - LOG_TAIL_BYTES))
            mentioned = STALE_LOCK_LINE in fh.read().decode("utf-8", "replace")
    except OSError:
        pass
    running = ctx.pg_ready()
    pid = _postmaster_pid(ctx)
    if not running and pidfile.exists():
        command = ""
        if pid is not None:
            command = ctx.run(["/bin/ps", "-o", "command=", "-p", str(pid)]).stdout.strip()
        program = Path(command.split(" ", 1)[0]).name if command else ""
        ours = program == "postgres" and str(ctx.settings.pg_data_dir) in command
        if not ours:
            who = f"pid {pid} is now {program or 'not running'}" if pid else "it holds no pid"
            yield Check(
                section,
                FAIL,
                f"A stale postmaster.pid keeps PostgreSQL from starting ({who})",
                detail=str(pidfile),
                fix="./run db start (it removes the stale file); the launchd agent removes it "
                "at its next start.",
            )
            return
    if mentioned and running:
        yield Check(
            section,
            INFO,
            "postgres.log mentions a stale lock file from an earlier start; the server runs now",
        )
    elif mentioned:
        yield Check(
            section,
            WARN,
            "postgres.log says a lock file blocked the start",
            detail=str(log_path),
            fix="./run db start",
        )
    else:
        yield Check(section, PASS, "No stale postmaster.pid")


def check_migrations(ctx: DoctorContext) -> Iterable[Check]:
    section = "Database"
    reachable, _ = ctx.database()
    if not reachable:
        yield Check(section, INFO, "Schema check skipped: no database connection")
        return
    from budgetapp.db import code_head, current_revision

    with ctx.session() as session:
        current = current_revision(session.connection())
    head = code_head()
    if current == head:
        yield Check(section, PASS, f"The {ctx.settings.env} schema is current ({head})")
        return
    if ctx.settings.is_production:
        fix = "./run live (or ./run install-agents): it backs up, then upgrades"
    else:
        fix = "./run migrate"
    yield Check(
        section,
        FAIL,
        f"The {ctx.settings.env} database is at schema {current or 'nothing'}; "
        f"this code needs {head}",
        fix=fix,
    )


# --------------------------------------------------------------------------- web


def _http_status(url: str) -> int | None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback only
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError, OSError:
        return None


def check_web(ctx: DoctorContext) -> Iterable[Check]:
    section = "Web app"
    port = ctx.settings.web_port
    url = ctx.settings.app_url
    listeners = _netstat_listeners(ctx, port)
    agent_loaded = False
    if ctx.settings.is_production and ctx.layout.plist_path("web").exists():
        agent_loaded = agents.Launchctl(ctx.layout.uid, ctx.runner).is_loaded("web")
    if listeners is None:
        yield Check(section, WARN, "Couldn't list network listeners (netstat failed)")
        return
    if not listeners:
        if agent_loaded:
            yield Check(
                section,
                FAIL,
                f"The web agent is loaded but nothing listens on {url}",
                fix=f"See {ctx.settings.logs_dir / 'web.log'}; ./run install-agents --with-web",
            )
        else:
            yield Check(section, INFO, f"The web app isn't running on {url}")
        return
    bad = [a for a in listeners if not _loopback(a, port)]
    if bad:
        yield Check(
            section,
            FAIL,
            f"Port {port} listens beyond this Mac: {', '.join(bad)} (SEC-1)",
            fix="Stop that program; the app only ever binds 127.0.0.1.",
        )
        return
    status = (ctx.http_status or _http_status)(f"{url}/")
    if status is None:
        yield Check(
            section,
            FAIL if agent_loaded else WARN,
            f"Something listens on {url} but doesn't answer",
            fix=f"See {ctx.settings.logs_dir / 'web.log'}",
        )
    elif status >= 500:
        yield Check(
            section,
            WARN,
            f"The web app at {url} answers {status} (it shows an error page)",
            fix="Open it to read the message (often ./run db start or an upgrade).",
        )
    else:
        yield Check(section, PASS, f"The web app answers on {url} and listens on loopback only")


# --------------------------------------------------------------------------- Keychain


@dataclass(frozen=True, slots=True)
class _Item:
    account: str
    env: str  # "sandbox", "production" or "both"
    label: str


def _keychain_items(ctx: DoctorContext) -> list[_Item]:
    items = [
        _Item(secrets.PLAID_CLIENT_ID, "both", "Plaid client ID"),
        _Item(secrets.plaid_secret_key("sandbox"), "sandbox", "Plaid Sandbox secret"),
        _Item(secrets.plaid_secret_key("production"), "production", "Plaid Production secret"),
        _Item(secrets.db_password_key("budgetapp_dev"), "sandbox", "Sandbox database password"),
        _Item(
            secrets.db_password_key("budgetapp_live"), "production", "production database password"
        ),
        _Item(secrets.PRODUCTION_ACCESS_TOKEN, "production", "production access token"),
    ]
    return items


def _item_count(ctx: DoctorContext) -> int | None:
    reachable, _ = ctx.database()
    if not reachable:
        return None
    with ctx.session() as session:
        return int(session.execute(text("SELECT count(*) FROM plaid_items")).scalar() or 0)


def check_keychain_items(ctx: DoctorContext) -> Iterable[Check]:
    section = "Keychain"
    env = ctx.settings.env if not ctx.settings.is_test else "sandbox"
    linked = (_item_count(ctx) or 0) > 0 if ctx.settings.is_production else False
    present: dict[str, bool] = {}
    readable = 0
    problems: list[Check] = []
    items = _keychain_items(ctx)
    try:
        sandbox_ids = secrets.sandbox_item_ids(store=ctx.store)
    except secrets.SecretStoreError:
        sandbox_ids = []
    items += [
        _Item(secrets.sandbox_access_token_key(i), "sandbox", f"Sandbox access token ({i})")
        for i in sandbox_ids
    ]
    for item in items:
        try:
            value = secrets.get_secret(item.account, store=ctx.store)
        except secrets.KeychainApprovalNeeded:
            problems.append(
                Check(
                    section,
                    FAIL,
                    f"macOS didn't allow reading the {item.label}",
                    fix="Run ./run doctor in Terminal again; click Always Allow when macOS asks.",
                )
            )
            continue
        except secrets.SecretStoreError as exc:
            problems.append(
                Check(section, FAIL, f"Couldn't read the {item.label}", detail=str(exc))
            )
            continue
        present[item.account] = bool(value)
        if value:
            readable += 1
    role_key = {
        "sandbox": secrets.db_password_key("budgetapp_dev"),
        "production": secrets.db_password_key("budgetapp_live"),
    }
    for item in items:
        if present.get(item.account, True):
            continue
        mine = item.env in (env, "both")
        if item.account in role_key.values():
            status = FAIL if item.account == role_key[env] else WARN
            problems.append(
                Check(section, status, f"The {item.label} is missing", fix="./run setup")
            )
        elif item.account == secrets.PRODUCTION_ACCESS_TOKEN:
            if linked:
                problems.append(
                    Check(
                        section,
                        FAIL,
                        "The production access token is missing, but Amex is linked",
                        fix="Re-enter your saved token: "
                        "./run setup --env production --access-token",
                    )
                )
        elif item.account.startswith("plaid.access_token.sandbox."):
            problems.append(
                Check(section, WARN, f"The {item.label} is missing", fix="./run sandbox-link")
            )
        else:
            needed = env == "production" and linked and mine
            problems.append(
                Check(
                    section,
                    FAIL if needed else (WARN if mine else INFO),
                    f"The {item.label} isn't saved",
                    fix="./run setup --keys-only",
                )
            )
    if (
        env == "production"
        and not linked
        and present.get(secrets.PRODUCTION_ACCESS_TOKEN)
        and _item_count(ctx) == 0
    ):
        problems.append(
            Check(
                section,
                WARN,
                "A production access token is saved but the database has no connection",
                fix="Connection page → Re-attach existing connection (never link again).",
            )
        )
    yield from problems
    if readable:
        yield Check(section, PASS, f"{readable} Keychain item(s) readable (values never shown)")


def check_keychain_lock(ctx: DoctorContext) -> Iterable[Check]:
    section = "Keychain"
    keychain = ctx.home / "Library" / "Keychains" / "login.keychain-db"
    result = ctx.run(["/usr/bin/security", "show-keychain-info", str(keychain)])
    output = f"{result.stderr}\n{result.stdout}"  # the answer is on STDERR
    if result.returncode != 0 or 'Keychain "' not in output:
        yield Check(
            section,
            WARN,
            "Couldn't read the login keychain's lock settings",
            detail=_first_line(output),
        )
        return
    locks = [
        word for word in (" lock-on-sleep", " timeout=", "use-lock-interval") if word in output
    ]
    if locks:
        yield Check(
            section,
            WARN,
            "The login keychain locks itself",
            detail="Background sync and backups can't read a locked keychain: "
            + ", ".join(w.strip() for w in locks),
            fix="Keychain Access → Edit → Change Settings for Keychain “login”: untick "
            "“Lock after … minutes” and “Lock when sleeping”.",
        )
    else:
        yield Check(section, PASS, "The login keychain stays unlocked while you are logged in")


# --------------------------------------------------------------------------- agents and install


def check_agents(ctx: DoctorContext) -> Iterable[Check]:
    section = "Background agents"
    layout = ctx.layout
    launchctl = agents.Launchctl(layout.uid, ctx.runner)
    installed = agents.installed_agents(layout)
    if not ctx.settings.is_production:
        loaded = [n for n in installed if launchctl.is_loaded(n)]
        yield Check(
            section,
            INFO,
            "The launchd agents run only the production app"
            + (f" (loaded: {', '.join(loaded)})" if loaded else " (none installed)"),
        )
        return
    if not installed:
        yield Check(
            section,
            WARN,
            "The launchd agents aren't installed (no automatic sync or nightly backup)",
            fix="./run install-agents (a go-live step)",
        )
        return
    disabled = launchctl.disabled()
    problems = []
    for name in ("postgres", "sync", "backup"):
        if name not in installed:
            problems.append(
                Check(section, FAIL, f"The {name} agent is missing", fix="./run install-agents")
            )
    for name in installed:
        path = layout.plist_path(name)
        mode = _mode(path)
        if mode is not None and mode & 0o022:
            problems.append(
                Check(
                    section,
                    FAIL,
                    f"{path.name} can be changed by others (mode {mode:o})",
                    fix=f"chmod 644 {_quote(path)}",
                )
            )
        if name in disabled:
            problems.append(
                Check(
                    section,
                    FAIL,
                    f"The {name} agent is disabled",
                    fix="Allow Budget Tracker in System Settings → General → Login Items & "
                    f"Extensions, or: launchctl enable {launchctl.target(name)}; then "
                    "./run install-agents",
                )
            )
        elif not launchctl.is_loaded(name):
            problems.append(
                Check(
                    section,
                    FAIL,
                    f"The {name} agent is installed but not loaded",
                    fix="./run install-agents (and keep it allowed in System Settings → General → "
                    "Login Items & Extensions)",
                )
            )
    yield from problems
    if not problems:
        yield Check(section, PASS, f"Agents loaded: {', '.join(installed)}")


def check_install(ctx: DoctorContext) -> Iterable[Check]:
    section = "Background agents"
    layout = ctx.layout
    stamp = agents.read_stamp(layout)
    if not ctx.settings.is_production:
        if stamp:
            yield Check(section, INFO, f"The production install is at commit {stamp.short}")
        return
    if stamp is None or not layout.venv_python.exists():
        status = FAIL if agents.installed_agents(layout) else WARN
        yield Check(
            section,
            status,
            "The production app isn't installed in Application Support",
            fix="./run install-agents (or ./run live)",
        )
        return
    if layout.repo_dir is None:
        yield Check(section, INFO, f"The production install is at commit {stamp.short}")
        return
    head = ctx.run(["git", "-C", str(layout.repo_dir), "rev-parse", "HEAD"]).stdout.strip()
    if not head:
        yield Check(section, WARN, "Couldn't read the repository's commit")
    elif head != stamp.commit:
        yield Check(
            section,
            WARN,
            f"The production install is at {stamp.short}; the repository is at {head[:12]}",
            fix="./run install-agents (or ./run live): it backs up, then upgrades",
        )
    else:
        yield Check(
            section,
            PASS,
            f"The production install matches the repository (commit {stamp.short})",
        )


def check_notifications(ctx: DoctorContext) -> Iterable[Check]:
    section = "Background agents"
    result = ctx.run(["/usr/bin/defaults", "export", "com.apple.ncprefs", "-"])
    fix = "./run notify-test, then allow Script Editor in System Settings → Notifications"
    try:
        prefs = plistlib.loads(result.stdout.encode()) if result.returncode == 0 else None
    except plistlib.InvalidFileException, ValueError, TypeError:
        prefs = None
    if not isinstance(prefs, dict):
        yield Check(section, WARN, "Couldn't read the notification settings", fix=fix)
        return
    apps = prefs.get("apps") or []
    registered = any(isinstance(a, dict) and a.get("bundle-id") == SCRIPT_EDITOR for a in apps)
    if registered:
        yield Check(
            section,
            PASS,
            "Script Editor is registered for notifications",
            detail="macOS doesn't say whether they are allowed: check System Settings → "
            "Notifications → Script Editor (style: Alerts).",
        )
    else:
        yield Check(
            section, WARN, "Mac notifications aren't set up yet (alerts are dropped)", fix=fix
        )


# --------------------------------------------------------------------------- backups


def check_backups(ctx: DoctorContext) -> Iterable[Check]:
    section = "Backups"
    s = ctx.settings
    directory = backups.backup_dir(s)
    production = s.is_production
    if directory.is_dir():
        mode = _mode(directory)
        if mode is not None and mode != 0o700:
            yield Check(
                section,
                FAIL,
                f"The backup folder is mode {mode:o}, not 700",
                detail=str(directory),
                fix=f"chmod 700 {_quote(directory)}",
            )
    found = backups.list_backups(s)  # every dump, pre-restore copies included
    loose = [b for b in found if (_mode(b.path) or 0) & 0o077]
    if loose:
        yield Check(
            section,
            FAIL,
            f"{len(loose)} dump(s) are readable by others",
            fix=f"chmod 600 {_quote(directory)}/*.dump",
        )
    # Freshness is about the nightly backups: a restore's copy of replaced data doesn't count.
    regular = [b for b in found if not b.pre_restore]
    copies = len(found) - len(regular)
    newest = regular[0] if regular else None
    if newest is None:
        yield Check(
            section,
            FAIL if production else INFO,
            f"No verified {s.env} backup yet",
            fix=f"./run backup --env {s.env}"
            + (" (the backup agent runs it nightly)" if production else ""),
        )
        return
    age = newest.age(ctx.now)
    if age > backups.STALE_AFTER:
        yield Check(
            section,
            FAIL if production else INFO,
            f"The newest {s.env} backup is {_hours(age)} old",
            detail=str(newest.path),
            fix=f"./run backup --env {s.env}; then read {s.logs_dir / 'backup.log'}",
        )
    else:
        extra = f", plus {copies} pre-restore copy(ies)" if copies else ""
        yield Check(
            section,
            PASS,
            f"Newest verified backup {_hours(age)} ago "
            f"({len(regular)} kept in {_short(directory, ctx.home)}{extra})",
        )


def _tm_destinations(ctx: DoctorContext) -> list[dict[str, Any]] | None:
    result = ctx.run(["/usr/bin/tmutil", "destinationinfo", "-X"])
    if result.returncode != 0:
        return None
    try:
        data = plistlib.loads(result.stdout.encode())
    except plistlib.InvalidFileException, ValueError:
        return None
    destinations = data.get("Destinations") if isinstance(data, dict) else None
    return [d for d in destinations or [] if isinstance(d, dict)]


def _encrypted(ctx: DoctorContext, destination: dict[str, Any]) -> bool | None:
    mount = destination.get("MountPoint") or destination.get("Mount Point")
    if not mount:
        return None
    result = ctx.run(["/usr/sbin/diskutil", "info", "-plist", str(mount)])
    if result.returncode != 0:
        return None
    try:
        info = plistlib.loads(result.stdout.encode())
    except plistlib.InvalidFileException, ValueError:
        return None
    if not isinstance(info, dict):
        return None
    # "FileVault" is the volume's own encryption; "Encryption" is true for any Apple silicon
    # volume (hardware), so it says nothing about a disk that leaves the Mac (RESEARCH T5.6a).
    return info.get("FileVault") is True


def _excluded(ctx: DoctorContext, path: Path) -> bool | None:
    result = ctx.run(["/usr/bin/tmutil", "isexcluded", str(path)])
    if "[Excluded]" in result.stdout:
        return True
    if "[Included]" in result.stdout:
        return False
    return None


def check_time_machine(ctx: DoctorContext) -> Iterable[Check]:
    section = "Backups"
    s = ctx.settings
    data = _excluded(ctx, s.pg_data_dir)
    if data is True:
        yield Check(section, PASS, "Time Machine skips the live database folder")
    elif data is False:
        yield Check(
            section,
            FAIL,
            "Time Machine backs up the live database folder (not a usable backup)",
            fix=f"tmutil addexclusion {_quote(s.pg_data_dir)} (./run setup does it)",
        )
    else:
        yield Check(section, WARN, "Couldn't ask Time Machine about the database folder")
    destinations = _tm_destinations(ctx)
    directory = backups.backup_dir(s)
    if destinations is None:
        yield Check(section, WARN, "Couldn't read the Time Machine destinations")
        return
    if not destinations:
        yield Check(section, INFO, "Time Machine has no destinations: the dumps stay on this Mac")
        return
    if _excluded(ctx, directory) is True:
        yield Check(section, INFO, "Time Machine skips the dumps: they stay on this Mac")
        return
    fix = (
        "Encrypt the destination (Time Machine settings → choose the disk → Encrypt backups), or "
        f"keep the dumps on this Mac: tmutil addexclusion {_quote(directory)}"
    )
    unencrypted, unknown = [], []
    for destination in destinations:
        name = str(destination.get("Name") or destination.get("ID") or "a destination")
        state = _encrypted(ctx, destination)
        if state is False:
            unencrypted.append(name)
        elif state is None:
            unknown.append(name)
    if unencrypted:
        yield Check(
            section,
            FAIL,
            "Time Machine copies the dumps to an unencrypted disk: "
            f"{', '.join(unencrypted)} (SEC-6)",
            fix=fix,
        )
    elif unknown:
        yield Check(
            section,
            WARN,
            f"Can't tell whether {', '.join(unknown)} is encrypted (not mounted or a network disk)",
            fix=fix,
        )
    else:
        yield Check(section, PASS, "Time Machine copies the dumps only to encrypted disks")


# --------------------------------------------------------------------------- code


def _looks_random(token: str) -> bool:
    body = token.rsplit("-", 5)[-5:]
    hexes = "".join(body)
    return len(set(hexes)) >= 8


def check_repository(ctx: DoctorContext) -> Iterable[Check]:
    section = "Code"
    repo = ctx.layout.repo_dir
    if repo is None:
        yield Check(section, INFO, "Running from the install: no repository to scan")
        return
    git = ["git", "-C", str(repo)]
    grep = ctx.run([*git, "grep", "-n", "-I", "-o", "-E", TOKEN_PATTERN], timeout=60)
    if grep.returncode not in (0, 1):
        yield Check(
            section,
            WARN,
            "Couldn't scan the repository for tokens",
            detail=_first_line(grep.stderr),
        )
    else:
        hits = []
        for line in grep.stdout.splitlines():
            path, _, rest = line.partition(":")
            number, _, token = rest.partition(":")
            if _looks_random(token):
                hits.append(f"{path}:{number}")
        if hits:
            yield Check(
                section,
                FAIL,
                f"A Plaid token is committed in {', '.join(hits[:5])} (SEC-2)",
                detail="The value is not shown. Anything in git history may be published.",
                fix="Remove it from the file and from git history, and treat it as leaked.",
            )
        else:
            yield Check(section, PASS, "No Plaid tokens in files tracked by git")
    ignored = ctx.run([*git, "check-ignore", "-q", "--no-index", "activity.csv"])
    if ignored.returncode == 0:
        yield Check(section, PASS, ".gitignore keeps CSV downloads out of git")
    elif ignored.returncode == 1:
        yield Check(
            section, FAIL, ".gitignore doesn't cover CSV downloads", fix="Add *.csv to .gitignore."
        )
    else:
        yield Check(section, WARN, "Couldn't check .gitignore", detail=_first_line(ignored.stderr))


# --------------------------------------------------------------------------- Plaid connection


def check_connection(ctx: DoctorContext) -> Iterable[Check]:
    section = "Plaid connection"
    reachable, _ = ctx.database()
    if not reachable:
        yield Check(section, INFO, "Connection check skipped: no database connection")
        return
    from budgetapp.services.status import connection_status

    with ctx.session() as session:
        status = connection_status(session, now=ctx.now)
    if not status.items:
        yield Check(section, INFO, f"Nothing is linked in {ctx.settings.env} yet")
        return
    for item in status.items:
        name = item.institution_name or "The connection"
        due = item.consent_due_at.astimezone().date()
        if item.item_status == "needs_reconnect":
            yield Check(
                section,
                FAIL,
                f"{name} needs you to reconnect",
                detail=item.error_code,
                fix="Connection page → Reconnect",
            )
        elif item.item_status == "error":
            yield Check(
                section,
                WARN,
                f"The last sync of {name} failed ({item.error_code})",
                fix="Connection page → Sync now; see the log if it repeats",
            )
        if item.consent_days_left < 0:
            yield Check(
                section,
                FAIL,
                f"{name} consent expired on {due:%b %-d, %Y}",
                fix="Connection page → Renew",
            )
        elif item.consent_warning:
            yield Check(
                section,
                WARN,
                f"Renew {name} consent by {due:%b %-d, %Y} ({item.consent_days_left} days left)",
                fix="Connection page → Renew",
            )
        else:
            yield Check(section, PASS, f"{name} consent is valid until {due:%b %-d, %Y}")
        liabilities = item.liabilities_status
        if liabilities == "ok":
            yield Check(section, PASS, "Liabilities (statement balance, due date) are synced")
        elif liabilities == "not_consented":
            yield Check(
                section,
                WARN,
                "Liabilities wasn't consented (no statement balance or due date)",
                fix="Connection page → add Liabilities (a short Plaid update session)",
            )
        elif liabilities == "error":
            yield Check(
                section,
                WARN,
                "Liabilities failed at the last sync",
                fix="Connection page → Sync now",
            )
        elif liabilities == "unsupported":
            yield Check(section, INFO, "Plaid doesn't offer Liabilities for this card")
        else:
            yield Check(section, INFO, "Liabilities hasn't been fetched yet")
        if item.stale:
            since = item.last_sync_success_at or item.linked_at
            yield Check(
                section,
                WARN,
                f"No successful sync for {_hours(ctx.now - since)}",
                fix=f"./run sync --env {ctx.settings.env}; check the sync agent with ./run doctor",
            )


CHECKS: tuple[Callable[[DoctorContext], Iterable[Check]], ...] = (
    check_filevault,
    check_other_users,
    check_paths,
    check_permissions,
    check_postgres_running,
    check_database,
    check_postgres_settings,
    check_pg_hba,
    check_socket_dir,
    check_postgres_listeners,
    check_postgres_versions,
    check_stale_pid,
    check_migrations,
    check_web,
    check_keychain_items,
    check_keychain_lock,
    check_agents,
    check_install,
    check_notifications,
    check_backups,
    check_time_machine,
    check_repository,
    check_connection,
)


def run_checks(
    ctx: DoctorContext, checks: Sequence[Callable[[DoctorContext], Iterable[Check]]] = CHECKS
) -> Report:
    """Run every check; a check that crashes becomes a WARN line instead of stopping the rest."""
    results: list[Check] = []
    for check in checks:
        try:
            results.extend(check(ctx))
        except Exception as exc:  # report and carry on: doctor must always finish
            name = check.__name__.removeprefix("check_").replace("_", " ")
            results.append(
                Check(
                    "Doctor",
                    WARN,
                    f"The {name} check failed: {type(exc).__name__}",
                    detail=_first_line(str(exc)),
                )
            )
    return Report(ctx.settings.env, ctx.settings.db_name, tuple(results))


def format_report(report: Report, *, title: str | None = None) -> str:
    """The report as plain text: one line per check, its detail and fix indented below."""
    lines = [title or f"Budget Tracker doctor · {report.env} · database {report.database}"]
    section = None
    for check in report.checks:
        if check.section != section:
            section = check.section
            lines += ["", section]
        lines.append(f"  {check.status}  {check.title}")
        if check.detail:
            lines.append(f"        {check.detail}")
        if check.fix and check.status != PASS:
            lines.append(f"        Fix: {check.fix}")
    lines.append("")
    summary = (
        f"{report.count(PASS)} passed, {report.count(WARN)} warning(s), "
        f"{report.count(FAIL)} failed, {report.count(INFO)} info"
    )
    lines.append(("Problems found: " if report.failed else "All good: ") + summary)
    return "\n".join(lines)
