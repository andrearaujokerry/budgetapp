"""Backups (REL-2) and restore — ARCHITECTURE §10, RESEARCH T5.5.

**Backup** (``./run backup``, the nightly launchd agent): as the environment's own role (TCP +
scram with the password from Keychain passed in the child's environment, never on a command line;
the test environment uses its peer-mapped socket role), ``pg_dump --format=custom`` writes a hidden
temporary file (``umask 077``), which is verified with ``pg_restore --list`` and a full
``pg_restore --file=/dev/null`` read (``--list`` alone accepts a truncated dump), then renamed into
place. Only verified dumps ever carry the final name ``<db>-<UTC timestamp>.dump``; the newest 30
are kept. The folder is 0700 and every dump 0600. ``--if-due`` skips when the newest verified dump
is under 20 hours old. A failed backup, or a newest dump older than 26 hours, notifies once a day
(a marker file: the database may be the thing that is down).

The copies a restore takes of the data it replaces (``<db>-<timestamp>-pre-restore.dump``) are a
separate set: they never count toward the 30 nightly/manual backups, nor toward "due" or "stale",
and the newest 5 are kept, pruned only after a restore succeeds. Taking one never deletes anything,
so a restore can never delete the backup it was asked to restore.

**Restore** (``./run restore FILE``) replaces the environment's database with a dump of that same
database, in ONE transaction, so it either completes or changes nothing:

1. the dump is verified (full read) and its database name must be this environment's (a
   production dump never lands in Sandbox or the other way round); a dump made by a newer version
   of the app (an unknown schema revision) is refused;
2. ``pg_restore`` writes the dump as SQL to a private temporary file (checked complete): from here
   on the restore no longer needs the dump file;
3. the sync lock of every Item is taken and held until the end, BEFORE any agent stops: a running
   sync is never interrupted (the restore is refused), and a sync that starts meanwhile records
   ``skipped``;
4. production: the web, sync and backup agents are stopped (and started again at the end);
5. other sessions on the database are refused (listed), or ended with ``force_disconnect``;
6. a copy of the current data is taken and verified (``…-pre-restore.dump``), unless the caller
   explicitly skips it (``safety_copy=False``: a database ``pg_dump`` can no longer read);
7. ``psql --single-transaction`` drops everything in the ``public`` schema, replays the script and
   checks the result before COMMIT. Objects are created by, and so owned by, the environment's role
   (``--no-owner``); the database itself (owner, ``CONNECT`` revoked from ``PUBLIC``) is untouched;
8. the schema guard reports whether the restored data needs an upgrade.

Callers (the CLI) ask for the typed confirmation; the functions here never prompt.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.pool import NullPool

from budgetapp import clock, db, secrets
from budgetapp.config import Settings
from budgetapp.logsetup import redact
from budgetapp.services.notify import Notifier

log = logging.getLogger(__name__)

DEFAULT_KEEP = 30
#: Copies of replaced data kept by restores (a separate set; never counted toward DEFAULT_KEEP).
PRE_RESTORE_KEEP = 5
DUE_AFTER = dt.timedelta(hours=20)
STALE_AFTER = dt.timedelta(hours=26)
#: A temporary file older than this is left over from a killed run and is removed.
PARTIAL_MAX_AGE = dt.timedelta(hours=6)
#: Seconds to wait for stopped agents' connections to close before calling them "other sessions".
AGENT_EXIT_WAIT = 10.0
DUMP_TIMEOUT = 3600  # seconds, for pg_dump / pg_restore / psql
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
PRE_RESTORE = "pre-restore"
PARTIAL_SUFFIX = ".partial"
#: ``<db>-<timestamp>[-<n>][-pre-restore].dump``; only verified dumps carry this name.
DUMP_NAME = re.compile(
    r"^(?P<db>[a-z0-9_]+)-(?P<ts>\d{8}T\d{6}Z)(?:-(?P<seq>\d{1,3}))?(?P<pre>-pre-restore)?\.dump$"
)
#: The end of a complete ``pg_restore --file`` script.
SCRIPT_COMPLETE = "-- PostgreSQL database dump complete"
MARKER_KEY = "backup-problem-{env}"
CHILD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(Exception):
    """A backup could not be made or verified (nothing was left under a final name)."""


class RestoreRefused(Exception):
    """The restore was not started, or stopped before anything changed; the message says why."""


class SafetyCopyFailed(RestoreRefused):
    """The copy of the current data could not be made, so nothing was restored.

    The CLI then offers ``--without-safety-copy`` (a database ``pg_dump`` can no longer read).
    """


class RestoreFailed(Exception):
    """The restore transaction failed and was rolled back: the database is as it was."""


# --------------------------------------------------------------------------- listing


@dataclass(frozen=True, slots=True)
class Backup:
    """One verified dump in the environment's backup folder."""

    path: Path
    database: str
    created_at: dt.datetime  # aware, UTC (from the file name)
    size: int
    pre_restore: bool = False
    sequence: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    def age(self, now: dt.datetime | None = None) -> dt.timedelta:
        return (now or clock.now()) - self.created_at


def backup_dir(settings: Settings) -> Path:
    """The environment's backup folder (``backups/<env>/``)."""
    return Path(settings.backups_dir)


def parse_backup_name(path: Path) -> tuple[str, dt.datetime, int, bool] | None:
    """``(database, created_at, sequence, pre_restore)`` for a final dump name, else ``None``."""
    match = DUMP_NAME.match(path.name)
    if match is None:
        return None
    created = dt.datetime.strptime(match["ts"], TIMESTAMP_FORMAT).replace(tzinfo=dt.UTC)
    return match["db"], created, int(match["seq"] or 0), match["pre"] is not None


def list_backups(settings: Settings, *, pre_restore: bool | None = None) -> list[Backup]:
    """Verified dumps of this environment's database, newest first (temporary files ignored).

    ``pre_restore``: ``None`` lists every dump, ``False`` only the backups (nightly or
    ``./run backup``), ``True`` only the copies restores took of the data they replaced.
    """
    directory = backup_dir(settings)
    found: list[Backup] = []
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError, NotADirectoryError:
        return []
    for path in entries:
        parsed = parse_backup_name(path)
        if parsed is None or parsed[0] != settings.db_name:
            continue
        if pre_restore is not None and parsed[3] != pre_restore:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        database, created, sequence, pre = parsed
        found.append(Backup(path, database, created, stat.st_size, pre, sequence))
    found.sort(key=lambda b: (b.created_at, b.sequence, b.name), reverse=True)
    return found


def newest_backup(settings: Settings) -> Backup | None:
    """The newest backup (pre-restore copies don't count: they say nothing about whether the
    nightly backups work, and they hold the data a restore replaced)."""
    backups = list_backups(settings, pre_restore=False)
    return backups[0] if backups else None


def is_due(
    settings: Settings, *, min_age: dt.timedelta = DUE_AFTER, now: dt.datetime | None = None
) -> bool:
    """True when there is no verified backup younger than ``min_age`` (default 20 h)."""
    newest = newest_backup(settings)
    return newest is None or newest.age(now) >= min_age


def ensure_backup_dir(settings: Settings) -> Path:
    """Create the backup folder (and its parent) with mode 0700, fixing looser modes."""
    directory = backup_dir(settings)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


# --------------------------------------------------------------------------- connecting


@dataclass(frozen=True, slots=True)
class PgConnection:
    """libpq command-line arguments and the child environment for one database.

    The password (sandbox/production) travels only in ``env`` as ``PGPASSWORD``; ``args`` never
    hold a secret, so they may be printed or logged.
    """

    args: tuple[str, ...]
    env: dict[str, str] = field(repr=False)


def pg_connection(
    settings: Settings,
    *,
    store: secrets.SecretStore | None = None,
    application_name: str = "budgetapp-backup",
) -> PgConnection:
    """How pg_dump / pg_restore / psql reach the environment's database as its own role.

    Sandbox and production: TCP to 127.0.0.1 with ``scram-sha-256`` required and the password
    from Keychain. Test: the private socket with peer authentication as ``budgetapp_test``.
    """
    env = {
        "PATH": CHILD_PATH,
        "HOME": str(Path.home()),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PGAPPNAME": application_name,
        "PGCONNECT_TIMEOUT": "10",
    }
    port = str(settings.pg_port)
    if settings.env == "test":
        host = str(settings.pg_socket_dir)
    else:
        password = secrets.require_secret(
            secrets.db_password_key(settings.db_role),
            store=store,
            hint="Run ./run setup to create the database roles.",
        )
        env.update(
            {"PGPASSWORD": password, "PGREQUIREAUTH": "scram-sha-256", "PGSSLMODE": "disable"}
        )
        host = settings.pg_host
    args = ("-h", host, "-p", port, "-U", settings.db_role, "-d", settings.db_name)
    return PgConnection(args, env)


def _tool_env() -> dict[str, str]:
    """Environment for tools that read only a local file (pg_restore --list / --file)."""
    return {"PATH": CHILD_PATH, "HOME": str(Path.home()), "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


def _first_error_line(result: subprocess.CompletedProcess) -> str:
    text_out = (result.stderr or result.stdout or "").strip()
    lines = [line for line in text_out.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower() or "fatal" in line.lower()]
    chosen = (errors or lines or ["no detail"])[0]
    return redact(chosen.strip())[:300]


def _execute(
    runner: Runner,
    argv: Sequence[str],
    *,
    env: dict[str, str],
    timeout: float = DUMP_TIMEOUT,
    error: type[Exception] = BackupError,
) -> subprocess.CompletedProcess:
    """Run a PostgreSQL tool with ``umask 077``; OS errors and timeouts raise ``error``."""
    try:
        return runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
            umask=0o077,
        )
    except subprocess.TimeoutExpired as exc:
        raise error(f"{Path(argv[0]).name} did not finish within {int(timeout)} s") from exc
    except OSError as exc:
        raise error(f"Could not run {argv[0]}: {exc.strerror or exc}") from exc


# --------------------------------------------------------------------------- dump files


@dataclass(frozen=True, slots=True)
class DumpInfo:
    """The header of a custom-format dump (``pg_restore --list``)."""

    database: str | None
    created: str | None
    server_version: str | None
    dumped_by: str | None
    format: str | None
    toc_entries: int | None


_HEADER_FIELDS = {
    "dbname": "database",
    "Dumped from database version": "server_version",
    "Dumped by pg_dump version": "dumped_by",
    "Format": "format",
    "TOC Entries": "toc_entries",
}


def parse_dump_header(listing: str) -> DumpInfo:
    """Read the ``;``-comment header that ``pg_restore --list`` prints before the TOC."""
    values: dict[str, Any] = {name: None for name in _HEADER_FIELDS.values()}
    values["created"] = None
    for raw in listing.splitlines():
        if not raw.startswith(";"):
            if raw.strip():
                break  # the TOC entries start: the header is over
            continue
        line = raw.lstrip(";").strip()
        if line.startswith("Archive created at "):
            values["created"] = line.removeprefix("Archive created at ").strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        name = _HEADER_FIELDS.get(key.strip())
        if name is None or values[name] is not None:
            continue
        value = value.strip()
        if name == "toc_entries":
            values[name] = int(value) if value.isdigit() else None
        else:
            values[name] = value or None
    return DumpInfo(**values)


def verify_dump(settings: Settings, path: Path, *, runner: Runner = subprocess.run) -> DumpInfo:
    """Check a dump can be read completely; returns its header.

    ``pg_restore --list`` reads only the table of contents (it accepts a truncated file), so a
    full ``pg_restore --file=/dev/null`` read of every data block follows (RESEARCH T5.5).
    """
    pg_restore = settings.require_pg_bin("pg_restore")
    listing = _execute(runner, [pg_restore, "--list", str(path)], env=_tool_env())
    if listing.returncode != 0:
        raise BackupError(f"{path.name} is not a readable dump: {_first_error_line(listing)}")
    info = parse_dump_header(listing.stdout or "")
    if info.format and info.format.upper() != "CUSTOM":
        raise BackupError(f"{path.name} is a {info.format} dump; expected the custom format.")
    full = _execute(runner, [pg_restore, "--file=/dev/null", str(path)], env=_tool_env())
    if full.returncode != 0:
        raise BackupError(f"{path.name} is incomplete or damaged: {_first_error_line(full)}")
    return info


def _final_path(directory: Path, database: str, when: dt.datetime, *, pre_restore: bool) -> Path:
    stamp = when.astimezone(dt.UTC).strftime(TIMESTAMP_FORMAT)
    suffix = f"-{PRE_RESTORE}" if pre_restore else ""
    for sequence in range(1000):
        middle = f"-{sequence}" if sequence else ""
        path = directory / f"{database}-{stamp}{middle}{suffix}.dump"
        if not path.exists():
            return path
    raise BackupError("too many backups with the same timestamp")


def _remove_stale_partials(directory: Path, now: dt.datetime) -> list[Path]:
    removed = []
    for path in directory.glob(f".*{PARTIAL_SUFFIX}"):
        try:
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
            if now - modified > PARTIAL_MAX_AGE:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_all_but(
    backups: Sequence[Backup], keep: int, protect: Sequence[Path] = ()
) -> list[Path]:
    if keep < 1:
        raise ValueError("keep at least one backup")
    protected = {Path(p).resolve(strict=False) for p in protect}
    removed = []
    for backup in backups[keep:]:
        if backup.path.resolve(strict=False) in protected:
            continue
        try:
            backup.path.unlink()
        except FileNotFoundError:
            continue
        removed.append(backup.path)
    return removed


def rotate(
    settings: Settings, keep: int = DEFAULT_KEEP, *, protect: Sequence[Path] = ()
) -> list[Path]:
    """Delete all but the ``keep`` newest backups; returns the removed paths.

    Pre-restore copies are never touched (see :func:`prune_pre_restore`), nor are ``protect``ed
    paths.
    """
    return _remove_all_but(list_backups(settings, pre_restore=False), keep, protect)


def prune_pre_restore(
    settings: Settings, keep: int = PRE_RESTORE_KEEP, *, protect: Sequence[Path] = ()
) -> list[Path]:
    """Delete all but the ``keep`` newest pre-restore copies, never a ``protect``ed path (the
    file a restore just read may itself be a pre-restore copy: an undo)."""
    return _remove_all_but(list_backups(settings, pre_restore=True), keep, protect)


def fix_permissions(settings: Settings) -> list[Path]:
    """Make the folder 0700 and every dump in it 0600; returns the paths that were changed."""
    changed = []
    directory = backup_dir(settings)
    if directory.is_dir() and directory.stat().st_mode & 0o777 != 0o700:
        os.chmod(directory, 0o700)
        changed.append(directory)
    for backup in list_backups(settings):
        if backup.path.stat().st_mode & 0o777 != 0o600:
            os.chmod(backup.path, 0o600)
            changed.append(backup.path)
    return changed


# --------------------------------------------------------------------------- backup


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What ``run_backup`` did. ``status`` is ``created`` or ``skipped`` (``--if-due``)."""

    status: str
    path: Path | None = None
    size: int = 0
    seconds: float = 0.0
    info: DumpInfo | None = None
    removed: tuple[Path, ...] = ()
    newest: Backup | None = None
    kept: int = 0

    @property
    def created(self) -> bool:
        return self.status == "created"


def run_backup(
    settings: Settings,
    *,
    keep: int = DEFAULT_KEEP,
    if_due: bool = False,
    min_age: dt.timedelta = DUE_AFTER,
    pre_restore: bool = False,
    runner: Runner = subprocess.run,
    store: secrets.SecretStore | None = None,
    now: dt.datetime | None = None,
) -> BackupResult:
    """Dump the environment's database into its backup folder (see the module docstring).

    A backup then rotates the backups (newest ``keep`` stay). A ``pre_restore`` copy deletes
    nothing: it is taken while a restore may be reading the oldest backup, and the restore prunes
    its own copies once it has succeeded.

    Raises :class:`BackupError` when the dump or its verification fails; the temporary file is
    removed, so nothing unverified ever carries a final name.
    """
    if keep < 1:
        raise ValueError("keep at least one backup")
    now = now or clock.now()
    directory = ensure_backup_dir(settings)
    if if_due:
        newest = newest_backup(settings)
        if newest is not None and newest.age(now) < min_age:
            return BackupResult("skipped", newest=newest, kept=len(list_backups(settings)))
    _remove_stale_partials(directory, now)
    pg_dump = settings.require_pg_bin("pg_dump")
    connection = pg_connection(settings, store=store)
    final = _final_path(directory, settings.db_name, now, pre_restore=pre_restore)
    temporary = directory / f".{final.stem}{PARTIAL_SUFFIX}"
    started = time.monotonic()
    try:
        result = _execute(
            runner,
            [pg_dump, "--format=custom", "--no-password", f"--file={temporary}", *connection.args],
            env=connection.env,
        )
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {_first_error_line(result)}")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise BackupError("pg_dump wrote no data.")
        os.chmod(temporary, 0o600)
        info = verify_dump(settings, temporary, runner=runner)
        if info.database is not None and info.database != settings.db_name:
            raise BackupError(
                f"The dump names database {info.database!r}, expected {settings.db_name!r}."
            )
        os.replace(temporary, final)
        _fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    seconds = time.monotonic() - started
    removed = () if pre_restore else tuple(rotate(settings, keep))
    size = final.stat().st_size
    log.info(
        "backup %s (%d bytes, %.1f s); removed %d old", final.name, size, seconds, len(removed)
    )
    return BackupResult(
        "created",
        path=final,
        size=size,
        seconds=seconds,
        info=info,
        removed=removed,
        newest=newest_backup(settings),
        kept=len(list_backups(settings, pre_restore=pre_restore)),
    )


# --------------------------------------------------------------------------- alerts


def backup_health(
    settings: Settings, *, now: dt.datetime | None = None
) -> tuple[bool, Backup | None]:
    """``(stale, newest)``: stale when there is no verified dump or the newest is over 26 h old."""
    newest = newest_backup(settings)
    return newest is None or newest.age(now) > STALE_AFTER, newest


def notify_backup_problem(
    settings: Settings,
    notifier: Notifier,
    message: str,
    *,
    title: str = "Budget Tracker backup problem",
    today: dt.date | None = None,
) -> bool:
    """Notify about a failed or stale backup at most once a day (a marker file, not the DB).

    Returns ``True`` when the notification was handed to macOS.
    """
    key = MARKER_KEY.format(env=settings.env)
    try:
        claimed = secrets.claim_daily_marker(settings.state_dir, key, today=today)
    except OSError:
        log.warning("could not write the backup alert marker; notifying anyway")
        claimed = True
    if not claimed:
        return False
    result = notifier.notify(title, message)
    if not result.ok:
        log.warning("backup notification failed: %s", result.error)
    return result.ok


def stale_message(settings: Settings, newest: Backup | None, now: dt.datetime | None = None) -> str:
    if newest is None:
        return (
            f"There is no verified {settings.env} backup yet. Run ./run doctor --env "
            f"{settings.env} in Terminal."
        )
    hours = int(newest.age(now).total_seconds() // 3600)
    return (
        f"The newest {settings.env} backup is {hours} hours old. Run ./run doctor --env "
        f"{settings.env} in Terminal to see why backups stopped."
    )


# --------------------------------------------------------------------------- restore


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Another connection to the database (``pg_stat_activity``)."""

    pid: int
    user: str | None
    application: str | None
    client: str | None
    state: str | None

    def describe(self) -> str:
        who = self.application or "unknown program"
        where = f" from {self.client}" if self.client else " (local socket)"
        return f"pid {self.pid}: {who}{where}, {self.state or 'unknown state'}"


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """A verified dump that may be restored into ``settings``' database."""

    path: Path
    info: DumpInfo
    revision: str | None
    code_head: str
    size: int

    @property
    def needs_upgrade(self) -> bool:
        return self.revision != self.code_head


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What :func:`restore` did. ``pre_restore`` is ``None`` when the copy was skipped."""

    path: Path
    pre_restore: BackupResult | None
    seconds: float
    schema_ok: bool
    schema_message: str
    counts: dict[str, int]
    stopped_agents: tuple[str, ...]
    restarted_agents: tuple[str, ...]
    terminated: tuple[SessionInfo, ...]
    pruned: tuple[Path, ...] = ()


class AgentControl(Protocol):
    """Stops and restarts the production launchd agents around a restore (``services.agents``)."""

    def stop_app_agents(self) -> list[str]: ...

    def start_agents(self, names: Sequence[str]) -> list[str]: ...


def known_revisions() -> set[str]:
    """Every Alembic revision shipped with this code."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(db.alembic_config())
    return {revision.revision for revision in script.walk_revisions()}


def dump_revision(settings: Settings, path: Path, *, runner: Runner = subprocess.run) -> str | None:
    """The Alembic revision stored in a dump (``None`` when it has no ``alembic_version`` row)."""
    pg_restore = settings.require_pg_bin("pg_restore")
    result = _execute(
        runner,
        [pg_restore, "--data-only", "--table=alembic_version", "--file=-", str(path)],
        env=_tool_env(),
    )
    if result.returncode != 0:
        raise BackupError(f"Could not read the schema version: {_first_error_line(result)}")
    lines = (result.stdout or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("COPY ") and "alembic_version" in line and "FROM stdin" in line:
            for value in lines[index + 1 :]:
                if value == "\\.":
                    break
                if value.strip():
                    return value.strip()
            return None
    return None


def inspect_backup(
    settings: Settings, path: Path, *, runner: Runner = subprocess.run
) -> RestorePlan:
    """Verify a dump and check it belongs to this environment (raises :class:`RestoreRefused`)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise RestoreRefused(f"There is no backup file at {path}.")
    try:
        info = verify_dump(settings, path, runner=runner)
        revision = dump_revision(settings, path, runner=runner)
    except BackupError as exc:
        raise RestoreRefused(f"{exc} Nothing was changed.") from exc
    if info.database != settings.db_name:
        raise RestoreRefused(
            f"This backup is of the database {info.database or '(unknown)'!r}, but the "
            f"{settings.env} database is {settings.db_name!r}. Restore a backup of "
            f"{settings.db_name!r} (they are in {backup_dir(settings)}). Nothing was changed."
        )
    head = db.code_head()
    if revision is not None and revision not in known_revisions():
        raise RestoreRefused(
            f"This backup was made by a newer version of the app (schema {revision}); this code "
            f"knows up to {head}. Restore it with that version. Nothing was changed."
        )
    if revision is None:
        raise RestoreRefused(
            f"{path.name} has no schema version (alembic_version), so it is not a backup of "
            "this app's database. Nothing was changed."
        )
    return RestorePlan(path, info, revision, head, path.stat().st_size)


def sync_lock_holders(engine: Engine) -> list[int]:
    """PIDs of other connections holding a sync lock in this database (a sync is running)."""
    query = text(
        "SELECT DISTINCT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
        "AND classid = :namespace AND pid <> pg_backend_pid() "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "ORDER BY pid"
    )
    with engine.connect() as conn:
        return [int(pid) for pid in conn.execute(query, {"namespace": db.LOCK_NS_SYNC}).scalars()]


def other_sessions(engine: Engine, *, exclude: Sequence[int] = ()) -> list[SessionInfo]:
    """Client connections to the engine's database other than this one (and ``exclude``: the
    restore's own lock connection)."""
    query = text(
        "SELECT pid, usename, application_name, client_addr::text, state "
        "FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend' ORDER BY pid"
    )
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    skip = set(exclude)
    return [
        SessionInfo(int(r[0]), r[1], r[2] or None, r[3], r[4])
        for r in rows
        if int(r[0]) not in skip
    ]


def terminate_sessions(
    engine: Engine, sessions: Sequence[SessionInfo], *, wait: float = 10.0
) -> list[SessionInfo]:
    """End other connections (``pg_terminate_backend``: the same role may end its own); waits
    until they are gone. Raises :class:`RestoreRefused` if one cannot be ended."""
    if not sessions:
        return []
    with engine.connect() as conn:
        for session in sessions:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": session.pid})
    deadline = time.monotonic() + wait
    targets = {s.pid for s in sessions}
    while True:
        remaining = [s for s in other_sessions(engine) if s.pid in targets]
        if not remaining:
            return list(sessions)
        if time.monotonic() > deadline:
            raise RestoreRefused(
                "These connections could not be ended: "
                + "; ".join(s.describe() for s in remaining)
                + ". Nothing was changed."
            )
        time.sleep(0.2)


def item_ids(engine: Engine) -> list[int]:
    """Database ids of every Plaid Item (none when the table does not exist)."""
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('public.plaid_items')")).scalar() is None:
            return []
        rows = conn.execute(text("SELECT id FROM plaid_items ORDER BY id")).scalars()
        return [int(item_id) for item_id in rows]


@dataclass(frozen=True, slots=True)
class SyncLocks:
    """The sync locks :func:`hold_sync_locks` holds: the Items, and the connection holding them."""

    items: tuple[int, ...]
    backend_pid: int | None  # None when there are no Items (nothing to hold)

    @property
    def pids(self) -> tuple[int, ...]:
        return () if self.backend_pid is None else (self.backend_pid,)


SYNC_RUNNING = "A sync is running right now. Try again in a few minutes. Nothing was changed."


def _release_sync_locks(conn: Connection, held: Sequence[int]) -> None:
    try:
        for item_id in held:
            if not conn.execute(select(func.pg_advisory_unlock(db.LOCK_NS_SYNC, item_id))).scalar():
                conn.invalidate()  # closing the session releases whatever it still holds
                break
    except Exception:
        log.warning("could not release the sync locks cleanly; discarding the connection")
        conn.invalidate()
    finally:
        conn.close()


@contextlib.contextmanager
def hold_sync_locks(engine: Engine) -> Iterator[SyncLocks]:
    """Hold the sync lock of every Item (ARCHITECTURE §6) for the ``with`` block.

    The same keys as ``db.sync_lock``, all on ONE dedicated autocommit connection whose pid is in
    the result, so the restore never counts it as another session. Raises
    :class:`RestoreRefused` when a sync is running (a lock is busy); nothing is held then.
    """
    ids = item_ids(engine)
    if not ids:
        yield SyncLocks((), None)
        return
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    held: list[int] = []
    try:
        pid = int(conn.execute(select(func.pg_backend_pid())).scalar_one())
        for item_id in ids:
            lock = func.pg_try_advisory_lock(db.LOCK_NS_SYNC, int(item_id))
            if not conn.execute(select(lock)).scalar():
                raise RestoreRefused(SYNC_RUNNING)
            held.append(item_id)
        yield SyncLocks(tuple(held), pid)
    finally:
        _release_sync_locks(conn, held)


#: Runs first inside the restore transaction: drop every object in ``public`` (also objects a
#: newer migration created that the dump does not know), so the script starts from empty.
_PRELUDE = """\
SET lock_timeout = '60s';
DO $restore$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                    WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END AS kind,
               c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        ORDER BY CASE c.relkind WHEN 'v' THEN 0 WHEN 'm' THEN 1 ELSE 2 END, c.relname
    LOOP
        EXECUTE format('DROP %s IF EXISTS public.%I CASCADE', item.kind, item.name);
    END LOOP;
    FOR item IN
        SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS args, p.prokind
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
    LOOP
        EXECUTE format('DROP %s IF EXISTS public.%I(%s) CASCADE',
                       CASE item.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE'
                            ELSE 'FUNCTION' END,
                       item.name, item.args);
    END LOOP;
    FOR item IN
        SELECT t.typname AS name, t.typtype
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'c', 'r')
          AND (t.typrelid = 0 OR (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c')
    LOOP
        EXECUTE format('DROP %s IF EXISTS public.%I CASCADE',
                       CASE item.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, item.name);
    END LOOP;
END
$restore$;
"""

#: Runs last inside the restore transaction; an exception rolls everything back.
_CHECK = """\
DO $check$
BEGIN
    IF to_regclass('public.alembic_version') IS NULL THEN
        RAISE EXCEPTION 'the restored data has no alembic_version table';
    END IF;
    IF (SELECT count(*) FROM public.alembic_version) <> 1 THEN
        RAISE EXCEPTION 'the restored data has no single schema version';
    END IF;
END
$check$;
"""


def write_restore_script(
    settings: Settings, plan: RestorePlan, script: Path, *, runner: Runner = subprocess.run
) -> None:
    """``pg_restore --no-owner --file=<script>``; checks the script is complete."""
    pg_restore = settings.require_pg_bin("pg_restore")
    result = _execute(
        runner,
        [pg_restore, "--no-owner", f"--file={script}", str(plan.path)],
        env=_tool_env(),
        error=RestoreRefused,
    )
    if result.returncode != 0:
        raise RestoreRefused(
            f"Could not read {plan.path.name}: {_first_error_line(result)}. Nothing was changed."
        )
    try:
        with script.open("rb") as fh:
            fh.seek(max(0, script.stat().st_size - 4096))
            tail = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        raise RestoreRefused(f"Could not read the restore script: {exc}") from exc
    if SCRIPT_COMPLETE not in tail:
        raise RestoreRefused(f"{plan.path.name} did not convert completely. Nothing was changed.")


def apply_restore_script(
    settings: Settings,
    script: Path,
    *,
    runner: Runner = subprocess.run,
    store: secrets.SecretStore | None = None,
) -> None:
    """Drop, replay and check in ONE transaction (``psql --single-transaction``).

    Raises :class:`RestoreFailed` when psql reports an error: the transaction rolled back.
    """
    psql = settings.require_pg_bin("psql")
    connection = pg_connection(settings, store=store, application_name="budgetapp-restore")
    argv = [
        psql,
        "-X",
        "-q",
        "--no-password",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        *connection.args,
        "-c",
        _PRELUDE,
        "-f",
        str(script),
        "-c",
        _CHECK,
    ]
    result = _execute(runner, argv, env=connection.env, error=RestoreFailed)
    if result.returncode != 0:
        raise RestoreFailed(
            f"The restore failed and was rolled back, so the database is unchanged: "
            f"{_first_error_line(result)}"
        )


def _counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in ("transactions", "accounts", "plaid_items", "budgets", "statements"):
            if conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar() is None:
                continue
            counts[table] = int(conn.execute(text(f"SELECT count(*) FROM public.{table}")).scalar())
    return counts


def upgrade_hint(settings: Settings) -> str:
    """What to run when the restored data is older than the code."""
    if settings.is_production:
        return "./run install-agents (or ./run live): it backs up, then upgrades the database"
    if settings.is_test:
        return "the test harness migrates its own databases"
    return "./run migrate"


def _close_other_sessions(
    settings: Settings,
    engine: Engine,
    locks: SyncLocks,
    *,
    force_disconnect: bool,
    wait: float,
    say: Callable[[str], None],
) -> list[SessionInfo]:
    """Refuse while other connections are open, or end them with ``force_disconnect``.

    Stopped agents get ``wait`` seconds to close their pools first. The restore's own lock
    connection never counts.
    """
    sessions = other_sessions(engine, exclude=locks.pids)
    deadline = time.monotonic() + wait
    while sessions and time.monotonic() < deadline:
        time.sleep(0.5)
        sessions = other_sessions(engine, exclude=locks.pids)
    if sessions and not force_disconnect:
        raise RestoreRefused(
            f"{len(sessions)} other connection(s) to {settings.db_name} are open: "
            + "; ".join(s.describe() for s in sessions)
            + ". Close the app (and any psql session), or pass --force-disconnect to end "
            "them. Nothing was changed."
        )
    if not sessions:
        return []
    terminated = terminate_sessions(engine, sessions)
    say(f"Ended {len(terminated)} other connection(s) to {settings.db_name}.")
    return terminated


def restore(
    settings: Settings,
    plan: RestorePlan,
    *,
    force_disconnect: bool = False,
    safety_copy: bool = True,
    agents: AgentControl | None = None,
    say: Callable[[str], None] = print,
    runner: Runner = subprocess.run,
    store: secrets.SecretStore | None = None,
    engine: Engine | None = None,
    keep_pre_restore: int = PRE_RESTORE_KEEP,
) -> RestoreResult:
    """Replace the environment's database with ``plan`` (see the module docstring).

    The caller has already shown the plan and received the typed confirmation. ``say`` reports
    each step as it happens. ``safety_copy=False`` skips the copy of the current data (for a
    database ``pg_dump`` can no longer read; the caller warns that it is not saved).

    Raises :class:`RestoreRefused` (the database was not changed; :class:`SafetyCopyFailed` when
    the copy of the current data failed) or :class:`RestoreFailed` (rolled back); either way
    agents that were stopped are started again. Nothing in the backup folder is deleted unless
    the restore succeeded, and never ``plan.path``.
    """
    started = time.monotonic()
    if settings.is_production and agents is None:
        raise RestoreRefused("A production restore needs the launchd agent control.")
    script = ensure_backup_dir(settings) / f".restore-{os.getpid()}.sql{PARTIAL_SUFFIX}"
    own_engine = engine is None
    if engine is None:
        engine = db.make_engine(settings, poolclass=NullPool)
    stopped: list[str] = []
    restarted: list[str] = []
    terminated: list[SessionInfo] = []
    pre: BackupResult | None = None
    try:
        # The whole dump becomes the restore script first: from here on nothing needs the file.
        write_restore_script(settings, plan, script, runner=runner)
        # The locks come BEFORE any agent stops: a running sync refuses the restore instead of
        # being killed by the bootout, and a sync an agent starts meanwhile records "skipped".
        with hold_sync_locks(engine) as locks:
            if locks.items:
                say(f"Holding the sync lock of {len(locks.items)} Item(s) until the restore ends.")
            if agents is not None:
                stopped = agents.stop_app_agents()
                if stopped:
                    say(f"Stopped the {', '.join(stopped)} agent(s) for the restore.")
            terminated = _close_other_sessions(
                settings,
                engine,
                locks,
                force_disconnect=force_disconnect,
                wait=AGENT_EXIT_WAIT if stopped else 0,
                say=say,
            )
            if safety_copy:
                try:
                    pre = run_backup(settings, pre_restore=True, runner=runner, store=store)
                except BackupError as exc:
                    raise SafetyCopyFailed(
                        f"The copy of the current data failed ({exc}), so nothing was restored "
                        "and the database was not changed."
                    ) from exc
                assert pre.path is not None
                say(f"Saved the current data first (verified): {pre.path}")
            else:
                say("Not saving the current data first (--without-safety-copy).")
            say(f"Restoring {plan.path.name} into {settings.db_name} (one transaction)…")
            apply_restore_script(settings, script, runner=runner, store=store)
        say(f"Restored {plan.path.name} into {settings.db_name}.")
        try:
            db.schema_guard(engine)
            schema_ok, schema_message = True, f"The schema is current ({plan.code_head})."
        except db.SchemaMismatch as exc:
            schema_ok = False
            schema_message = (
                f"The restored data is at schema {exc.db_revision}; this code expects "
                f"{exc.code_revision}. Upgrade it with {upgrade_hint(settings)}."
            )
        counts = _counts(engine)
        pruned = prune_pre_restore(settings, keep_pre_restore, protect=(plan.path,))
        if pruned:
            say(
                f"Removed {len(pruned)} older pre-restore copy(ies); the newest "
                f"{keep_pre_restore} are kept."
            )
    finally:
        with contextlib.suppress(OSError):
            script.unlink()
        if agents is not None and stopped:
            try:
                restarted = agents.start_agents(stopped)
                if restarted:
                    say(f"Started the {', '.join(restarted)} agent(s) again.")
            except Exception as exc:  # report, never hide the restore's own outcome
                restarted = list(getattr(exc, "started", ()))  # agents.AgentStartError
                say(f"Could not start the agents again ({exc}); run ./run install-agents.")
        if own_engine:
            engine.dispose()
    return RestoreResult(
        path=plan.path,
        pre_restore=pre,
        seconds=time.monotonic() - started,
        schema_ok=schema_ok,
        schema_message=schema_message,
        counts=counts,
        stopped_agents=tuple(stopped),
        restarted_agents=tuple(restarted),
        terminated=tuple(terminated),
        pruned=tuple(pruned),
    )
