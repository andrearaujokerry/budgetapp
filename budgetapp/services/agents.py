"""launchd agents and the production install (ARCHITECTURE §2 "One production code copy", §10).

macOS privacy protection (TCC) keeps launchd jobs out of ``~/Desktop``, where the repository
lives, so production always runs a NON-editable install in
``~/Library/Application Support/budgetapp/venv`` (RESEARCH T5.2e):

* :func:`upgrade_install` is the only way production code changes: refuse a dirty tree → export
  the commit (``git archive``) into a private temporary folder → stop the web, sync and backup
  agents that are loaded → back up the production database → ``pip install -r requirements.txt``
  then ``pip install --no-deps`` from that export into the install → run the migrations FROM THE
  NEW INSTALL while holding the sync lock → write the install stamp (the git commit, which
  ``doctor`` compares with the repository) → start the agents again. The install is built from
  the commit itself, never from the working tree, so a file saved during the upgrade can't
  reach production under a clean stamp. When a step fails, the agents it stopped are started
  again (best effort) before the error is raised.
* :func:`launch_production` is ``./run live``: upgrade when the stamp differs from the
  repository's commit (or the production schema is behind), then open the web agent, or serve
  from the install when no web agent is installed.
* :func:`install_agents` / :func:`uninstall_agents` are ``./run install-agents`` /
  ``uninstall-agents``: they write the plists (``plistlib``, checked with ``plutil -lint``, mode
  644) and bootstrap them in the order postgres → sync → backup → web, or boot them out in the
  reverse order (``pg_ctl stop -m fast`` before postgres, so smart shutdown never waits for the
  app's pooled connections). Both have a dry run that only prints the plan. When an install
  fails part-way, the agents that were loaded before it are loaded again (best effort) and the
  output names any that still aren't, so scheduled syncs and backups never stop silently.

The agents (labels ``local.budgetapp.<name>``) run the install with ``BUDGETAPP_ENV=production``,
``LC_ALL=C.UTF-8``, a PATH with PostgreSQL 18 and Homebrew, ``HOME`` and the data paths; their
working directory and logs (``~/Library/Logs/budgetapp/<agent>.log``) are outside the protected
folders. Every path comes from a :class:`Layout`, so tests use temporary folders; in a test
process the real LaunchAgents folder, the real install, ``launchctl`` and the other external
programs are refused unless a test passes its own runner.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from budgetapp import __version__, clock
from budgetapp.config import Settings, default_data_dir, in_test_process

log = logging.getLogger(__name__)

LABEL_PREFIX = "local.budgetapp."
#: Install order; uninstall and stopping go in reverse.
AGENTS = ("postgres", "sync", "backup", "web")
#: The agents that use the database (stopped around upgrades and restores).
APP_AGENTS = ("sync", "backup", "web")
SYNC_SCHEDULE = ((0, 15), (6, 15), (12, 15), (18, 15))
BACKUP_SCHEDULE = (2, 30)
BASE_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"
STAMP_FILE = "install-stamp.json"
WRAPPER_FILE = "postgres-launchd.sh"
LAUNCHCTL = "/bin/launchctl"
PLUTIL = "/usr/bin/plutil"
DB_WAIT = 120  # seconds: every agent entry point waits this long for the database
WEB_WAIT = 30  # seconds to wait for the web app to answer before opening the browser
#: Untracked files under these paths would end up in the install, so they make the tree dirty.
INSTALLED_PATHS = ("budgetapp/", "pyproject.toml", "requirements.txt", "README.md")

Runner = Callable[..., subprocess.CompletedProcess]

DESCRIPTIONS = {
    "postgres": "the database server, at login and kept running",
    "sync": "sync at 00:15, 06:15, 12:15, 18:15 and at login (skipped if synced in the last 5 h)",
    "backup": "backup at 02:30 and at login (skipped if the newest backup is under 20 h old)",
    "web": "the production web app at http://127.0.0.1:8765, at login and kept running",
}


class AgentError(Exception):
    """An install, upgrade or agent operation could not be done; the message says what to do."""


class DirtyTreeError(AgentError):
    """The repository has changes that are not committed."""


class AgentStartError(AgentError):
    """Some agents could not be loaded; ``started`` are the ones that were, ``failed`` the rest."""

    def __init__(
        self, message: str, *, started: Sequence[str] = (), failed: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.started = tuple(started)
        self.failed = tuple(failed)


def label(name: str) -> str:
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name!r}")
    return f"{LABEL_PREFIX}{name}"


# --------------------------------------------------------------------------- layout


def default_python() -> str:
    """The base interpreter for the install: ``$BUDGETAPP_PYTHON``, Homebrew's stable opt path,
    else ``python3.14`` on PATH (the venv must not point at a versioned Cellar path)."""
    chosen = os.environ.get("BUDGETAPP_PYTHON")
    if chosen:
        return shutil.which(chosen) or chosen
    if Path(DEFAULT_PYTHON).exists():
        return DEFAULT_PYTHON
    return shutil.which("python3.14") or DEFAULT_PYTHON


@dataclass(frozen=True, slots=True)
class Layout:
    """Where the agents and the production install live (every path can be overridden)."""

    launch_agents_dir: Path
    home: Path
    uid: int
    install_dir: Path
    bin_dir: Path
    logs_dir: Path
    state_dir: Path
    data_dir: Path
    pg_bin_dir: Path
    pg_data_dir: Path
    repo_dir: Path | None
    python: str
    web_url: str

    @classmethod
    def for_settings(cls, settings: Settings, **overrides: Any) -> Layout:
        home = Path(overrides.pop("home", None) or Path.home())
        values: dict[str, Any] = {
            "launch_agents_dir": home / "Library" / "LaunchAgents",
            "home": home,
            "uid": os.getuid(),
            "install_dir": settings.install_dir,
            "bin_dir": settings.data_dir / "bin",
            "logs_dir": settings.logs_dir,
            "state_dir": settings.state_dir,
            "data_dir": settings.data_dir,
            "pg_bin_dir": settings.pg_bin_dir,
            "pg_data_dir": settings.pg_data_dir,
            "repo_dir": settings.repo_dir,
            "python": default_python(),
            "web_url": settings.app_url,
        }
        values.update(overrides)
        return cls(**values)

    def replace(self, **changes: Any) -> Layout:
        return replace(self, **changes)

    @property
    def venv_python(self) -> Path:
        return self.install_dir / "bin" / "python"

    @property
    def stamp_path(self) -> Path:
        return self.state_dir / STAMP_FILE

    @property
    def wrapper_path(self) -> Path:
        return self.bin_dir / WRAPPER_FILE

    def plist_path(self, name: str) -> Path:
        return self.launch_agents_dir / f"{label(name)}.plist"

    def log_path(self, name: str) -> Path:
        return self.logs_dir / f"{name}.log"


def default_layout(settings: Settings) -> Layout:
    """The real layout for ``settings`` (production: ``~/Library/...``)."""
    return Layout.for_settings(settings)


def _real_locations() -> tuple[Path, ...]:
    home = Path.home()
    data = default_data_dir()
    return (
        home / "Library" / "LaunchAgents",
        data / "venv",
        data / "bin",
        home / "Library" / "Application Support" / "budgetapp" / "venv",
    )


def guard_layout(layout: Layout) -> None:
    """In a test process, refuse the real LaunchAgents folder and the real install."""
    if not in_test_process():
        return
    real = {p.resolve(strict=False) for p in _real_locations()}
    for path in (layout.launch_agents_dir, layout.install_dir, layout.bin_dir):
        if path.resolve(strict=False) in real:
            raise AgentError(
                f"Refusing to use {path} from the test environment; pass temporary folders."
            )


def default_runner() -> Runner:
    """``subprocess.run``, refused in a test process (tests pass a fake runner)."""
    if in_test_process():
        raise AgentError("Tests must pass a runner: external programs are never run implicitly.")
    return subprocess.run


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    timeout: float = 120,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    try:
        return runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"{Path(argv[0]).name} did not finish within {int(timeout)} s") from exc
    except OSError as exc:
        raise AgentError(f"Could not run {argv[0]}: {exc.strerror or exc}") from exc


def _detail(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or result.stdout or "").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else f"exit {result.returncode}")[:300]


# --------------------------------------------------------------------------- launchctl


class Launchctl:
    """``launchctl`` in the user's GUI domain (``gui/<uid>``).

    Only exit statuses are used: ``launchctl print`` output is "NOT API" (RESEARCH T5.6d).
    """

    def __init__(self, uid: int, runner: Runner | None = None) -> None:
        self.uid = uid
        self.runner = runner if runner is not None else default_runner()

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def target(self, name: str) -> str:
        return f"{self.domain}/{label(name)}"

    def _call(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return _run(self.runner, [LAUNCHCTL, *args], timeout=timeout)

    def is_loaded(self, name: str) -> bool:
        return self._call("print", self.target(name), timeout=15).returncode == 0

    def disabled(self) -> set[str]:
        """Agent names that are disabled (``"local.budgetapp.<x>" => disabled``)."""
        result = self._call("print-disabled", self.domain, timeout=15)
        names = set()
        for name in AGENTS:
            if f'"{label(name)}" => disabled' in (result.stdout or ""):
                names.add(name)
        return names

    def enable(self, name: str) -> None:
        self._call("enable", self.target(name))

    def bootstrap(self, name: str, plist: Path) -> None:
        result = self._call("bootstrap", self.domain, str(plist))
        if result.returncode != 0:
            raise AgentError(
                f"launchctl bootstrap {label(name)} failed ({_detail(result)}). If it says the "
                "service is disabled, allow Budget Tracker in System Settings → General → Login "
                "Items & Extensions."
            )

    def bootout(self, name: str) -> bool:
        """Unload an agent (its process is stopped); ``False`` when it was not loaded."""
        result = self._call("bootout", self.target(name), timeout=180)
        return result.returncode == 0

    def kickstart(self, name: str) -> None:
        self._call("kickstart", self.target(name))


class AgentController:
    """Stops and restarts the app agents around a restore or an upgrade."""

    def __init__(self, layout: Layout, launchctl: Launchctl) -> None:
        self.layout = layout
        self.launchctl = launchctl

    def stop_app_agents(self) -> list[str]:
        stopped = []
        for name in reversed(APP_AGENTS):  # web, backup, sync
            try:
                if self.launchctl.is_loaded(name) and self.launchctl.bootout(name):
                    stopped.append(name)
            except AgentError as exc:
                log.warning("could not stop the %s agent: %s", name, exc)
        return stopped

    def start_agents(self, names: Sequence[str]) -> list[str]:
        """Bootstrap ``names`` (in install order) that have a plist and aren't loaded.

        Every agent is tried; when some fail, :class:`AgentStartError` names them (and the ones
        that did start) once all were tried.
        """
        started: list[str] = []
        failed: list[str] = []
        problems: list[str] = []
        for name in AGENTS:
            if name not in names or not self.layout.plist_path(name).exists():
                continue
            try:
                if self.launchctl.is_loaded(name):
                    continue
                self.launchctl.bootstrap(name, self.layout.plist_path(name))
            except AgentError as exc:
                failed.append(name)
                problems.append(str(exc))
                continue
            started.append(name)
        if failed:
            raise AgentStartError(
                f"Could not start the {', '.join(failed)} agent(s): {'; '.join(problems)}",
                started=started,
                failed=failed,
            )
        return started


# --------------------------------------------------------------------------- plists


def agent_environment(layout: Layout) -> dict[str, str]:
    """``EnvironmentVariables`` for every agent (launchd expands nothing: all paths absolute)."""
    return {
        "BUDGETAPP_ENV": "production",
        "BUDGETAPP_DATA_DIR": str(layout.data_dir),
        "BUDGETAPP_LOGS_DIR": str(layout.logs_dir),
        "BUDGETAPP_PG_BIN": str(layout.pg_bin_dir),
        "PATH": f"{layout.pg_bin_dir}:{BASE_PATH}",
        "LC_ALL": "C.UTF-8",
        "HOME": str(layout.home),
    }


def program_arguments(name: str, layout: Layout) -> list[str]:
    if name == "postgres":
        return ["/bin/sh", str(layout.wrapper_path)]
    app = [str(layout.venv_python), "-I", "-u", "-m", "budgetapp"]
    commands = {
        "sync": ["sync", "--env", "production", "--if-due", "--unattended"],
        "backup": ["backup", "--env", "production", "--if-due", "--unattended"],
        "web": ["serve", "--env", "production", "--unattended"],
    }
    return [*app, *commands[name]]


def _calendar(hour: int, minute: int) -> dict[str, int]:
    return {"Hour": hour, "Minute": minute}


def build_plist(name: str, layout: Layout) -> dict[str, Any]:
    """The launchd job for one agent (RESEARCH T5.2a–c)."""
    plist: dict[str, Any] = {
        "Label": label(name),
        "ProgramArguments": program_arguments(name, layout),
        "EnvironmentVariables": agent_environment(layout),
        "WorkingDirectory": str(layout.data_dir),
        "StandardOutPath": str(layout.log_path(name)),
        "StandardErrorPath": str(layout.log_path(name)),
        "RunAtLoad": True,
        "Umask": 63,  # 0o077, as a decimal integer (launchd.plist(5))
    }
    if name == "postgres":
        plist.update(
            {
                "KeepAlive": {"SuccessfulExit": False},  # pg_ctl stop -m fast exits 0: stays down
                "ThrottleInterval": 30,
                "ExitTimeOut": 120,
                "ProcessType": "Standard",
            }
        )
    elif name == "sync":
        plist.update(
            {
                "StartCalendarInterval": [_calendar(h, m) for h, m in SYNC_SCHEDULE],
                "ProcessType": "Background",
                "LowPriorityIO": True,
                "ExitTimeOut": 60,
            }
        )
    elif name == "backup":
        plist.update(
            {
                "StartCalendarInterval": _calendar(*BACKUP_SCHEDULE),
                "ProcessType": "Background",
                "LowPriorityIO": True,
                "ExitTimeOut": 60,
            }
        )
    else:
        plist.update(
            {
                "KeepAlive": True,
                "ThrottleInterval": 30,
                "ProcessType": "Standard",
                "ExitTimeOut": 30,
            }
        )
    return plist


def render_plist(plist: dict[str, Any]) -> bytes:
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)


def wrapper_script(layout: Layout) -> str:
    """The ``/bin/sh`` wrapper the postgres agent runs (stale-lock removal, then ``exec``).

    After an unclean shutdown ``postmaster.pid`` may name a PID that macOS has given to another
    process; PostgreSQL then refuses to start ("lock file already exists") and launchd retries
    forever. The file counts as live only when its PID is a running ``postgres`` whose ``-D`` is
    this data directory; any other PID (dead, another program, another cluster) makes it stale.
    """
    return f"""#!/bin/sh
# Managed by budgetapp (./run install-agents); rewritten on every install. Starts the budgetapp
# PostgreSQL 18 cluster in the foreground for launchd (local.budgetapp.postgres).
PGDATA={shlex.quote(str(layout.pg_data_dir))}
PGBIN={shlex.quote(str(layout.pg_bin_dir))}
LC_ALL=C.UTF-8
export LC_ALL
umask 077
pidfile="$PGDATA/postmaster.pid"
if [ -f "$pidfile" ]; then
    pid=$(/usr/bin/head -n 1 "$pidfile" 2>/dev/null | /usr/bin/tr -cd '0-9')
    live=no
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        command=$(/bin/ps -o command= -p "$pid" 2>/dev/null)
        if [ -z "$command" ]; then
            live=unknown
        else
            program=${{command%% *}}
            program=${{program##*/}}
            if [ "$program" = postgres ]; then
                case "$command" in
                    *" -D $PGDATA" | *" -D $PGDATA "*) live=yes ;;
                esac
            fi
        fi
    fi
    if [ "$live" = no ]; then
        /bin/rm -f "$pidfile"
        echo "budgetapp: removed a stale postmaster.pid (pid ${{pid:-none}} is not ours)" >&2
    fi
fi
exec "$PGBIN/postgres" -D "$PGDATA"
"""


def _write_file(path: Path, data: bytes, mode: int) -> None:
    """Write atomically (temporary file, then rename) with ``mode``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def lint_plist(path: Path, runner: Runner) -> None:
    result = _run(runner, [PLUTIL, "-lint", str(path)], timeout=30)
    if result.returncode != 0:
        raise AgentError(f"{path.name} did not pass plutil -lint: {_detail(result)}")


def write_agent_files(layout: Layout, names: Sequence[str], runner: Runner) -> list[Path]:
    """Write the wrapper and the plists for ``names`` (plists 644, checked with plutil)."""
    guard_layout(layout)
    for folder in (layout.bin_dir, layout.logs_dir):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
    layout.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "postgres" in names:
        _write_file(layout.wrapper_path, wrapper_script(layout).encode(), 0o700)
        written.append(layout.wrapper_path)
    for name in names:
        path = layout.plist_path(name)
        _write_file(path, render_plist(build_plist(name, layout)), 0o644)
        lint_plist(path, runner)
        written.append(path)
    return written


# --------------------------------------------------------------------------- git and stamp


def _git(layout: Layout, runner: Runner, *args: str) -> subprocess.CompletedProcess:
    if layout.repo_dir is None:
        raise AgentError(
            "This needs the git checkout: run it with ./run from the budgetapp folder."
        )
    git = shutil.which("git") or "/usr/bin/git"
    return _run(runner, [git, "-C", str(layout.repo_dir), *args], timeout=60)


def repo_commit(layout: Layout, runner: Runner) -> str:
    result = _git(layout, runner, "rev-parse", "HEAD")
    commit = (result.stdout or "").strip()
    if result.returncode != 0 or not commit:
        raise AgentError(f"Could not read the repository's commit: {_detail(result)}")
    return commit


def dirty_paths(layout: Layout, runner: Runner) -> list[str]:
    """Uncommitted changes that would reach the install (untracked files elsewhere are fine)."""
    result = _git(layout, runner, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    if result.returncode != 0:
        raise AgentError(f"Could not read the repository's status: {_detail(result)}")
    dirty = []
    entries = (result.stdout or "").split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":  # renames and copies carry the original path next
            index += 1
        if status == "??" and not path.startswith(INSTALLED_PATHS):
            continue
        dirty.append(path)
    return dirty


@dataclass(frozen=True, slots=True)
class InstallStamp:
    """What the Application Support install was built from (``state/install-stamp.json``)."""

    commit: str
    installed_at: str
    app_version: str
    python: str
    repo_dir: str

    @property
    def short(self) -> str:
        return self.commit[:12]


def read_stamp(layout: Layout) -> InstallStamp | None:
    try:
        data = json.loads(layout.stamp_path.read_text(encoding="utf-8"))
        return InstallStamp(**{k: str(data[k]) for k in InstallStamp.__slots__})
    except FileNotFoundError, ValueError, KeyError, TypeError, OSError:
        return None


def write_stamp(layout: Layout, commit: str, python_version: str) -> InstallStamp:
    stamp = InstallStamp(
        commit=commit,
        installed_at=clock.now().isoformat(timespec="seconds"),
        app_version=__version__,
        python=python_version,
        repo_dir=str(layout.repo_dir),
    )
    layout.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_file(layout.stamp_path, (json.dumps(asdict(stamp), indent=2) + "\n").encode(), 0o600)
    return stamp


# --------------------------------------------------------------------------- upgrade


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    commit: str
    previous_commit: str | None
    backup_path: Path | None
    migration: str
    stopped: tuple[str, ...]
    restarted: tuple[str, ...]


def _install_env(layout: Layout) -> dict[str, str]:
    """A clean environment for pip and the install's own commands (no PYTHON*, no repo path)."""
    env = agent_environment(layout)
    env.pop("BUDGETAPP_ENV")
    return env


def _default_backup(settings: Settings) -> Path | None:
    from budgetapp.services import backup

    _refuse_production_in_tests(settings)
    try:
        return backup.run_backup(settings).path
    except backup.BackupError as exc:  # ./run live reports AgentError only
        raise AgentError(
            f"The backup before the upgrade failed ({exc}), so nothing was upgraded. "
            "Check it with ./run doctor --env production."
        ) from exc


@contextlib.contextmanager
def _default_lock(settings: Settings) -> Iterator[None]:
    """Hold every Item's sync lock (the restore helper does exactly this)."""
    from sqlalchemy.pool import NullPool

    from budgetapp import db
    from budgetapp.services import backup

    _refuse_production_in_tests(settings)
    engine = db.make_engine(settings, poolclass=NullPool)
    try:
        try:
            with backup.hold_sync_locks(engine):
                yield
        except backup.RestoreRefused as exc:
            raise AgentError(
                "A sync is running right now; try again in a few minutes. The install was "
                "updated but the database was not migrated yet."
            ) from exc
    finally:
        engine.dispose()


def _default_revision(settings: Settings) -> str | None:
    """The production database's Alembic revision; raises when it cannot be read."""
    from sqlalchemy.pool import NullPool

    from budgetapp import db

    _refuse_production_in_tests(settings)
    engine = db.make_engine(settings, poolclass=NullPool)
    try:
        return db.current_revision(engine)
    finally:
        engine.dispose()


def _refuse_production_in_tests(settings: Settings) -> None:
    if in_test_process() and settings.is_production:
        raise AgentError("Tests never touch the production database; pass fakes.")


def _python_version(layout: Layout, runner: Runner) -> str:
    result = _run(
        runner, [str(layout.venv_python), "-I", "-c", "import sys; print(sys.version.split()[0])"]
    )
    return (result.stdout or "").strip() if result.returncode == 0 else "unknown"


def ensure_venv(layout: Layout, runner: Runner, say: Callable[[str], None]) -> None:
    """Create the install's virtual environment when it is missing or broken."""
    if layout.venv_python.exists():
        check = _run(runner, [str(layout.venv_python), "-I", "-c", "pass"], timeout=60)
        if check.returncode == 0:
            return
        say(f"The install's Python does not run ({_detail(check)}); recreating it.")
    layout.install_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = _run(
        runner, [layout.python, "-m", "venv", "--clear", str(layout.install_dir)], timeout=300
    )
    if result.returncode != 0:
        raise AgentError(f"Could not create {layout.install_dir}: {_detail(result)}")
    os.chmod(layout.install_dir, 0o700)
    say(f"Created the virtual environment {layout.install_dir} ({layout.python}).")


@contextlib.contextmanager
def export_commit(layout: Layout, runner: Runner, commit: str) -> Iterator[Path]:
    """The repository's files at ``commit`` (``git archive``), in a private temporary folder.

    pip builds the install from this copy, never from the working tree: whatever is saved in the
    checkout while the upgrade runs (minutes of ``pip install``), production gets exactly the
    commit the install stamp records.
    """
    with tempfile.TemporaryDirectory(prefix="budgetapp-install-") as tmp:
        archive = Path(tmp) / "source.tar"
        result = _git(layout, runner, "archive", "--format=tar", f"--output={archive}", commit)
        if result.returncode != 0:
            raise AgentError(f"Could not export commit {commit[:12]}: {_detail(result)}")
        source = Path(tmp) / "source"
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(source, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise AgentError(f"Could not unpack commit {commit[:12]}: {exc}") from exc
        archive.unlink()
        yield source


def pip_install(layout: Layout, runner: Runner, say: Callable[[str], None], source: Path) -> None:
    """The pinned dependencies, then the app itself (never editable), both from ``source``: the
    commit exported by :func:`export_commit`."""
    pip = [str(layout.venv_python), "-I", "-m", "pip", "install", "--disable-pip-version-check"]
    pip += ["--no-input", "--quiet"]
    steps = (
        ("the pinned dependencies", [*pip, "-r", str(source / "requirements.txt")]),
        ("the app", [*pip, "--no-deps", "--force-reinstall", str(source)]),
    )
    for what, argv in steps:
        result = _run(runner, argv, timeout=900, env=_install_env(layout), cwd=layout.data_dir)
        if result.returncode != 0:
            raise AgentError(f"pip could not install {what}: {_detail(result)}")
        say(f"Installed {what} into {layout.install_dir}.")


def migrate_from_install(layout: Layout, runner: Runner) -> str:
    """``budgetapp migrate --env production`` run by the NEW install's own code."""
    argv = [str(layout.venv_python), "-I", "-m", "budgetapp", "migrate", "--env", "production"]
    result = _run(runner, argv, timeout=900, env=_install_env(layout), cwd=layout.data_dir)
    if result.returncode != 0:
        raise AgentError(f"The migrations failed: {_detail(result)}")
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else "migrated"


def start_again(
    controller: AgentController, names: Sequence[str], say: Callable[[str], None]
) -> list[str]:
    """After a failure: start ``names`` again, best effort; says what happened and never raises
    (the original error is the one to report)."""
    try:
        started = controller.start_agents(names)
    except AgentStartError as exc:
        started = list(exc.started)
        say(
            f"{str(exc).rstrip('.')}. Start them with ./run install-agents once the problem below "
            "is fixed."
        )
    except Exception as exc:
        log.warning("could not start the agents again: %s", exc)
        say(f"Could not start the agents again ({exc}); run ./run install-agents.")
        return []
    if started:
        say(f"Started the {', '.join(started)} agent(s) again.")
    return started


def upgrade_install(
    settings: Settings,
    *,
    layout: Layout | None = None,
    runner: Runner | None = None,
    launchctl: Launchctl | None = None,
    backup: Callable[[Settings], Path | None] | None = None,
    lock: Callable[[Settings], contextlib.AbstractContextManager[Any]] | None = None,
    revision: Callable[[Settings], str | None] | None = None,
    restart: bool = True,
    say: Callable[[str], None] = print,
) -> UpgradeResult:
    """Upgrade the Application Support install from the repository (see the module docstring).

    The only way production code and the production schema change. Raises :class:`AgentError`
    (or :class:`DirtyTreeError`) before anything changes when a precondition fails. When a later
    step fails, the agents it stopped are started again before the error is raised, whatever
    ``restart`` says: ``restart=False`` only leaves them stopped after a SUCCESSFUL upgrade, for
    a caller (``install_agents``) that reloads them itself.
    """
    if not settings.is_production:
        raise AgentError("The install always runs production; pass production settings.")
    layout = layout or default_layout(settings)
    guard_layout(layout)
    runner = runner if runner is not None else default_runner()
    launchctl = launchctl or Launchctl(layout.uid, runner)
    backup = backup or _default_backup
    lock = lock or _default_lock
    revision = revision or _default_revision

    dirty = dirty_paths(layout, runner)
    if dirty:
        shown = ", ".join(dirty[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        raise DirtyTreeError(
            f"The repository has uncommitted changes ({shown}). Commit or stash them first: "
            "production only runs committed code."
        )
    commit = repo_commit(layout, runner)
    previous = read_stamp(layout)
    try:
        current = revision(settings)
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(
            f"The production database can't be read ({type(exc).__name__}). Start it with "
            "./run db start (or check ./run doctor --env production), then try again."
        ) from exc
    from budgetapp.services.backup import known_revisions

    if current is not None and current not in known_revisions():
        raise AgentError(
            f"The production database is at schema {current}, which this code doesn't know (it "
            "is newer). Check out the newer code instead; nothing was changed."
        )

    controller = AgentController(layout, launchctl)
    with export_commit(layout, runner, commit) as source:
        say(f"Building the install from commit {commit[:12]} (git archive, not the working tree).")
        stopped = controller.stop_app_agents()
        if stopped:
            say(f"Stopped the {', '.join(stopped)} agent(s) for the upgrade.")
        try:
            backup_path = backup(settings)
            say(f"Backed up the production database: {backup_path}")
            ensure_venv(layout, runner, say)
            pip_install(layout, runner, say, source)
            with lock(settings):
                migration = migrate_from_install(layout, runner)
            say(f"Migrations (from the install, under the sync lock): {migration}")
            stamp = write_stamp(layout, commit, _python_version(layout, runner))
            say(f"Install stamp: commit {stamp.short}.")
        except BaseException:
            if stopped:
                start_again(controller, stopped, say)
            raise
    restarted: list[str] = []
    if restart and stopped:
        restarted = controller.start_agents(stopped)
        if restarted:
            say(f"Started the {', '.join(restarted)} agent(s) again.")
    return UpgradeResult(
        commit=commit,
        previous_commit=previous.commit if previous else None,
        backup_path=backup_path,
        migration=migration,
        stopped=tuple(stopped),
        restarted=tuple(restarted),
    )


def upgrade_reason(
    settings: Settings,
    layout: Layout,
    runner: Runner,
    revision: Callable[[Settings], str | None] | None = None,
) -> str | None:
    """Why ``./run live`` must upgrade first, or ``None`` when the install is current."""
    stamp = read_stamp(layout)
    if stamp is None or not layout.venv_python.exists():
        return "the production app isn't installed yet"
    commit = repo_commit(layout, runner)
    if stamp.commit != commit:
        return f"the code changed ({stamp.short} → {commit[:12]})"
    from budgetapp.db import code_head

    try:
        current = (revision or _default_revision)(settings)
    except Exception:  # unreachable: the app explains it; nothing to decide here
        return None
    if current != code_head():
        return f"the database is at schema {current or 'nothing'}; this code needs {code_head()}"
    return None


# --------------------------------------------------------------------------- ./run live


def http_ready(url: str, *, timeout: float = WEB_WAIT, interval: float = 0.5) -> bool:
    """True once ``url`` answers with any HTTP status (the app may show an error page)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3):  # noqa: S310 - loopback only
                return True
        except urllib.error.HTTPError:
            return True
        except urllib.error.URLError, OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


class Cluster:
    """Starting and stopping the dedicated cluster (``pgcluster``); tests pass a fake."""

    def start(self, settings: Settings) -> str:
        from budgetapp import pgcluster

        return pgcluster.start(settings)

    def stop(self, settings: Settings) -> str:
        from budgetapp import pgcluster

        return pgcluster.stop(settings)

    def is_running(self, settings: Settings) -> bool:
        from budgetapp import pgcluster

        return pgcluster.is_running(settings)

    def wait(self, settings: Settings, timeout: float = DB_WAIT) -> bool:
        from budgetapp import db

        return db.wait_for_db(settings, timeout=timeout)


def ensure_database(
    settings: Settings, layout: Layout, launchctl: Launchctl, cluster: Cluster
) -> str:
    """Start PostgreSQL the way it is managed and say how.

    While the postgres agent is loaded, ``launchctl kickstart`` starts it: ``pg_ctl start`` would
    run a server launchd does not supervise, and the agent's own start would then fail on the
    lock file (RESEARCH T5 gotchas). Without the agent, ``pg_ctl`` (``pgcluster.start``) does it.
    """
    if layout.plist_path("postgres").exists() and launchctl.is_loaded("postgres"):
        if cluster.is_running(settings):
            return "already running (launchd agent)"
        launchctl.kickstart("postgres")
        if not cluster.wait(settings, DB_WAIT):
            raise AgentError(
                f"PostgreSQL did not start under launchd within {DB_WAIT} s; see "
                f"{layout.log_path('postgres')}."
            )
        return "started by its launchd agent"
    return cluster.start(settings)


def launch_production(
    settings: Settings,
    *,
    layout: Layout | None = None,
    runner: Runner | None = None,
    launchctl: Launchctl | None = None,
    cluster: Cluster | None = None,
    upgrade: Callable[..., UpgradeResult] | None = None,
    revision: Callable[[Settings], str | None] | None = None,
    ready: Callable[[str], bool] = http_ready,
    open_browser: Callable[[str], Any] = webbrowser.open,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    say: Callable[[str], None] = print,
) -> int:
    """``./run live``: upgrade the install when needed, then open the production app from it.

    Returns the process exit code. Never runs production code from the repository.
    """
    layout = layout or default_layout(settings)
    try:
        guard_layout(layout)
        if layout.repo_dir is None:
            say("error: ./run live runs from the budgetapp folder (the git checkout).")
            return 2
        runner = runner if runner is not None else default_runner()
        launchctl = launchctl or Launchctl(layout.uid, runner)
        cluster = cluster or Cluster()
        say(f"  PostgreSQL: {ensure_database(settings, layout, launchctl, cluster)}")
        reason = upgrade_reason(settings, layout, runner, revision)
        if reason:
            say(f"Upgrading the production install first: {reason}.")
            (upgrade or upgrade_install)(
                settings,
                layout=layout,
                runner=runner,
                launchctl=launchctl,
                revision=revision,
                say=say,
            )
        web_plist = layout.plist_path("web")
        if web_plist.exists():
            if launchctl.is_loaded("web"):
                launchctl.kickstart("web")
            else:
                launchctl.bootstrap("web", web_plist)
            if not ready(layout.web_url):
                say(
                    f"error: the web agent didn't answer within {WEB_WAIT} s; see "
                    f"{layout.log_path('web')}."
                )
                return 1
            open_browser(layout.web_url)
            say(f"Budget Tracker (live) is open at {layout.web_url} (the web agent keeps it up).")
            return 0
    except AgentError as exc:
        say(f"error: {exc}")
        return 1
    return _serve_from_install(settings, layout, ready, open_browser, popen, say)


def _serve_from_install(
    settings: Settings,
    layout: Layout,
    ready: Callable[[str], bool],
    open_browser: Callable[[str], Any],
    popen: Callable[..., subprocess.Popen],
    say: Callable[[str], None],
) -> int:
    """No web agent: run ``serve --env production`` from the install until Ctrl-C."""
    argv = [str(layout.venv_python), "-I", "-u", "-m", "budgetapp", "serve", "--env", "production"]
    child = popen(argv, env=_install_env(layout), cwd=str(layout.data_dir))

    def opener() -> None:
        if ready(layout.web_url) and child.poll() is None:
            open_browser(layout.web_url)

    threading.Thread(target=opener, name="open-browser", daemon=True).start()
    try:
        return int(child.wait())
    except KeyboardInterrupt:  # the child got Ctrl-C too and is shutting down
        return int(child.wait())


# --------------------------------------------------------------------------- install / uninstall


@dataclass(frozen=True, slots=True)
class PlanStep:
    text: str
    action: Callable[[], Any] | None = None


def _wanted(with_web: bool) -> list[str]:
    return [name for name in AGENTS if name != "web" or with_web]


def install_agents(
    settings: Settings,
    *,
    with_web: bool = False,
    dry_run: bool = False,
    layout: Layout | None = None,
    runner: Runner | None = None,
    launchctl: Launchctl | None = None,
    cluster: Cluster | None = None,
    upgrade: Callable[..., UpgradeResult] | None = None,
    say: Callable[[str], None] = print,
) -> list[str]:
    """``./run install-agents``: upgrade the install, write and (re)load the agents.

    Returns the plan's lines (also printed through ``say``). With ``dry_run`` nothing changes:
    only ``git`` and ``launchctl print`` are read to make the plan. When a step fails, the
    agents that were loaded before the install are loaded again (best effort), PostgreSQL is
    started again if the install stopped it, and ``say`` names whatever is still not running;
    then the error is raised.
    """
    if not settings.is_production:
        raise AgentError("The agents always run production; pass production settings.")
    layout = layout or default_layout(settings)
    guard_layout(layout)
    runner = runner if runner is not None else default_runner()
    launchctl = launchctl or Launchctl(layout.uid, runner)
    cluster = cluster or Cluster()
    upgrade = upgrade or upgrade_install
    wanted = _wanted(with_web)
    loaded = {name: launchctl.is_loaded(name) for name in AGENTS}
    commit = repo_commit(layout, runner)
    progress = {"database_up": False}

    def start_database() -> None:
        say(f"     PostgreSQL: {ensure_database(settings, layout, launchctl, cluster)}")
        progress["database_up"] = True

    steps: list[PlanStep] = [
        PlanStep(
            "Make sure PostgreSQL is running (the upgrade backs up and migrates the database)",
            start_database,
        ),
        PlanStep(
            f"Upgrade the production install in {layout.install_dir} to commit {commit[:12]}: "
            "export that commit with git archive, stop the sync/backup/web agents, back up the "
            "production database, pip install -r requirements.txt and pip install --no-deps from "
            "the export (never the working tree, never editable), run the migrations from the "
            "install under the sync lock, write the install stamp",
            lambda: upgrade(
                settings, layout=layout, runner=runner, launchctl=launchctl, restart=False, say=say
            ),
        ),
        PlanStep(
            f"Write {layout.wrapper_path} and "
            + ", ".join(layout.plist_path(n).name for n in wanted)
            + f" in {layout.launch_agents_dir} (mode 644, checked with plutil -lint)",
            lambda: write_agent_files(layout, wanted, runner),
        ),
    ]
    if "web" not in wanted and (loaded["web"] or layout.plist_path("web").exists()):
        steps.append(
            PlanStep(
                "Remove the web agent (not requested; add --with-web to keep it)",
                lambda: _remove_agent("web", layout, launchctl),
            )
        )
    steps.append(
        PlanStep(
            "Stop PostgreSQL with pg_ctl stop -m fast"
            + (" and boot out the postgres agent" if loaded["postgres"] else "")
            + " so launchd starts it from the new plist",
            lambda: _hand_over_postgres(settings, launchctl, cluster, loaded["postgres"]),
        )
    )
    for name in wanted:
        steps.append(
            PlanStep(
                f"launchctl bootstrap {launchctl.domain} "
                f"{shlex.quote(str(layout.plist_path(name)))} — {DESCRIPTIONS[name]}",
                lambda name=name: _bootstrap(settings, name, layout, launchctl, cluster),
            )
        )
    lines = [step.text for step in steps]
    say("Plan (dry run, nothing changes):" if dry_run else "Installing the launchd agents:")
    if dry_run:
        for number, step in enumerate(steps, 1):
            say(f"  {number}. {step.text}")
        return lines
    try:
        for number, step in enumerate(steps, 1):
            say(f"  {number}. {step.text}")
            assert step.action is not None
            step.action()
    except BaseException:
        _recover_failed_install(
            settings,
            layout,
            launchctl,
            cluster,
            was_loaded=loaded,
            wanted=wanted,
            database_was_up=progress["database_up"],
            say=say,
        )
        raise
    say(
        "Done. macOS shows “Background Items Added”: keep Budget Tracker allowed in System "
        "Settings → General → Login Items & Extensions. Check everything with "
        "./run doctor --env production."
    )
    return lines


def _recover_failed_install(
    settings: Settings,
    layout: Layout,
    launchctl: Launchctl,
    cluster: Cluster,
    *,
    was_loaded: dict[str, bool],
    wanted: Sequence[str],
    database_was_up: bool,
    say: Callable[[str], None],
) -> None:
    """After a failed install: put back what ran before it, best effort, and say what doesn't.

    Every agent that was loaded before the install and isn't now is bootstrapped again
    (postgres first) from the plist on disk; the app agents cope with a database that isn't up
    yet, so they are loaded whatever PostgreSQL's state. A web agent removed on purpose (no
    ``--with-web``) stays removed. PostgreSQL is started again with ``pg_ctl`` only when the
    install had it running, no agent manages it now and it is down. Never raises: the install's
    own error is the one to report.
    """
    started: list[str] = []
    down: list[str] = []
    for name in AGENTS:
        if not was_loaded[name]:
            continue
        plist = layout.plist_path(name)
        try:
            if launchctl.is_loaded(name):
                continue
            if not plist.exists():
                if name in wanted:  # it was loaded from a plist that is gone: can't put it back
                    down.append(name)
                continue
            launchctl.bootstrap(name, plist)
            started.append(name)
        except Exception as exc:
            log.warning("could not load the %s agent again: %s", name, exc)
            down.append(name)
    if database_was_up:
        try:
            if not launchctl.is_loaded("postgres") and not cluster.is_running(settings):
                cluster.start(settings)
                say("Started PostgreSQL again (it was running before the install).")
        except Exception as exc:
            log.warning("could not start PostgreSQL again: %s", exc)
            say(f"PostgreSQL is not running ({exc}). Start it with ./run db start.")
    if started:
        say(f"Started the {', '.join(started)} agent(s) again (loaded before the install).")
    if down:
        say(
            f"NOT running now: the {', '.join(down)} agent(s), which ran before the install, so "
            "scheduled syncs and backups may be off. Fix the problem below, then run "
            "./run install-agents again."
        )


def _hand_over_postgres(
    settings: Settings, launchctl: Launchctl, cluster: Cluster, was_loaded: bool
) -> None:
    if cluster.is_running(settings):
        cluster.stop(settings)  # exit 0, so KeepAlive {SuccessfulExit: false} won't restart it
    if was_loaded:
        launchctl.bootout("postgres")


def _bootstrap(
    settings: Settings, name: str, layout: Layout, launchctl: Launchctl, cluster: Cluster
) -> None:
    if launchctl.is_loaded(name):
        launchctl.bootout(name)  # re-install: bootout, then bootstrap
    launchctl.enable(name)  # clears an earlier `launchctl disable`; harmless otherwise
    launchctl.bootstrap(name, layout.plist_path(name))
    if name == "postgres" and not cluster.wait(settings, DB_WAIT):
        raise AgentError(
            f"PostgreSQL did not start under launchd within {DB_WAIT} s; see "
            f"{layout.log_path('postgres')}."
        )


def _remove_agent(name: str, layout: Layout, launchctl: Launchctl) -> None:
    if launchctl.is_loaded(name):
        launchctl.bootout(name)
    with contextlib.suppress(FileNotFoundError):
        layout.plist_path(name).unlink()


def uninstall_agents(
    settings: Settings,
    *,
    dry_run: bool = False,
    layout: Layout | None = None,
    runner: Runner | None = None,
    launchctl: Launchctl | None = None,
    cluster: Cluster | None = None,
    say: Callable[[str], None] = print,
) -> list[str]:
    """``./run uninstall-agents``: boot out web → backup → sync → postgres and delete the plists.

    PostgreSQL is stopped with ``pg_ctl stop -m fast`` before its agent is booted out. The
    install, the data and the backups are kept.
    """
    layout = layout or default_layout(settings)
    guard_layout(layout)
    runner = runner if runner is not None else default_runner()
    launchctl = launchctl or Launchctl(layout.uid, runner)
    cluster = cluster or Cluster()
    steps: list[PlanStep] = []
    for name in reversed(AGENTS):
        loaded = launchctl.is_loaded(name)
        exists = layout.plist_path(name).exists()
        if not (loaded or exists):
            continue
        if name == "postgres" and loaded:
            steps.append(
                PlanStep(
                    "Stop PostgreSQL with pg_ctl stop -m fast (so shutdown doesn't wait)",
                    lambda: cluster.is_running(settings) and cluster.stop(settings),
                )
            )
        steps.append(
            PlanStep(
                (f"launchctl bootout {launchctl.target(name)}; " if loaded else "")
                + f"delete {shlex.quote(str(layout.plist_path(name)))}",
                lambda name=name: _remove_agent(name, layout, launchctl),
            )
        )
    if layout.wrapper_path.exists():
        steps.append(
            PlanStep(f"Delete {layout.wrapper_path}", lambda: layout.wrapper_path.unlink())
        )
    if not steps:
        say("No budgetapp launchd agents are installed.")
        return []
    say("Plan (dry run, nothing changes):" if dry_run else "Removing the launchd agents:")
    for number, step in enumerate(steps, 1):
        say(f"  {number}. {step.text}")
        if not dry_run and step.action is not None:
            step.action()
    say(
        f"Kept: the install ({layout.install_dir}), the database and the backups. Without the "
        "postgres agent, start the database with ./run db start (./run does it for you)."
    )
    return [step.text for step in steps]


def installed_agents(layout: Layout) -> list[str]:
    """Agents whose plist is in the LaunchAgents folder."""
    return [name for name in AGENTS if layout.plist_path(name).exists()]


def stamp_age(stamp: InstallStamp, now: dt.datetime | None = None) -> dt.timedelta | None:
    try:
        installed = dt.datetime.fromisoformat(stamp.installed_at)
    except ValueError:
        return None
    return (now or clock.now()) - installed
