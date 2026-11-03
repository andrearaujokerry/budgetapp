"""Settings: environment, paths, ports and database names. No secrets live here.

Three environments exist (ARCHITECTURE §2):

* ``sandbox`` (the default): Plaid Sandbox, database ``budgetapp_sandbox`` as role
  ``budgetapp_dev``, web on http://127.0.0.1:8766.
* ``production``: real data, database ``budgetapp`` as role ``budgetapp_live``, web on 8765.
  Selected only explicitly (``--env production``, ``./run live`` or the launchd agents).
* ``test``: pytest only. Database ``budgetapp_test_<pid>`` as the non-superuser ``budgetapp_test``.

``load_settings()`` picks the environment from its argument, then ``$BUDGETAPP_ENV``, then
``sandbox``. Paths can be redirected with ``BUDGETAPP_DATA_DIR``, ``BUDGETAPP_LOGS_DIR`` and
``BUDGETAPP_PG_BIN`` (the PostgreSQL 18 ``bin`` directory).

The test environment shares the real cluster (``data_dir``, its socket and data directory) but
never the places the app writes to: ``state_dir``, ``logs_dir``, ``backups_dir`` and
``install_dir`` default to a per-process scratch folder (:func:`scratch_dir_for_tests`), so test
code can never drop recovery files, daily markers, logs or an install into the real locations.
"""

from __future__ import annotations

import dataclasses
import getpass
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVIRONMENTS = ("sandbox", "production", "test")
APP_NAME = "budgetapp"
DEFAULT_PG_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")
PG_PORT = 5434
#: Shared development clusters on this Mac that must never be touched (ARCHITECTURE §2).
FORBIDDEN_PG_PORTS = frozenset({5432, 5433})
LOOPBACK = "127.0.0.1"
TEST_DB_PREFIX = "budgetapp_test_"

_DB = {
    "sandbox": ("budgetapp_sandbox", "budgetapp_dev"),
    "production": ("budgetapp", "budgetapp_live"),
}
_WEB_PORT = {"production": 8765, "sandbox": 8766, "test": 8766}


class ConfigError(Exception):
    """The requested settings are invalid or unsafe."""


class PostgresNotFound(ConfigError):
    """A PostgreSQL 18 program is missing from ``settings.pg_bin_dir``."""


def in_test_process(settings: Settings | None = None) -> bool:
    """True inside pytest (``BUDGETAPP_ENV=test``) or for test-environment settings.

    Test code never gets a superuser connection and never starts, stops or reconfigures the real
    cluster (SEC-5: database roles are the boundary between test code and real data).
    """
    if os.environ.get("BUDGETAPP_ENV") == "test":
        return True
    return settings is not None and settings.env == "test"


def scratch_dir_for_tests() -> Path:
    """Per-process folder for the test environment's state, logs, backups and install paths."""
    return Path(tempfile.gettempdir()).resolve() / f"budgetapp-test-{os.getpid()}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings for one environment. Build with :func:`load_settings`."""

    env: str
    repo_dir: Path | None
    data_dir: Path
    logs_dir: Path
    state_dir: Path
    pg_bin_dir: Path
    pg_port: int
    pg_host: str
    pg_socket_dir: Path
    pg_data_dir: Path
    web_host: str
    web_port: int
    backups_dir: Path
    install_dir: Path
    db_name: str
    db_role: str
    plaid_env: str

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def app_url(self) -> str:
        """The web app's URL, e.g. ``http://127.0.0.1:8766``."""
        return f"http://{self.web_host}:{self.web_port}"

    @property
    def os_user(self) -> str:
        """The macOS account name (the cluster superuser, used only for admin operations)."""
        return getpass.getuser()

    def pg_bin(self, name: str) -> str:
        """Absolute path of a PostgreSQL 18 binary, e.g. ``settings.pg_bin("pg_dump")``."""
        return str(self.pg_bin_dir / name)

    def require_pg_bin(self, name: str) -> str:
        """Like :meth:`pg_bin`, but raise :class:`PostgresNotFound` when the program is missing."""
        path = self.pg_bin_dir / name
        if not os.access(path, os.X_OK) or not path.is_file():
            raise PostgresNotFound(
                f"PostgreSQL 18 not found: {path} does not exist. Install it with "
                "`brew install postgresql@18` (or point BUDGETAPP_PG_BIN at its bin folder)."
            )
        return str(path)

    def replace(self, **changes: Any) -> Settings:
        """A copy with some fields changed (validated like :func:`load_settings`)."""
        new = dataclasses.replace(self, **changes)
        new.validate()
        return new

    def validate(self) -> None:
        """Raise :class:`ConfigError` if the settings are unsafe (ARCHITECTURE §2, SEC-1, SEC-5)."""
        if self.env not in ENVIRONMENTS:
            raise ConfigError(f"Unknown environment {self.env!r}; use one of {ENVIRONMENTS}.")
        for name in ("pg_port", "web_port"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value < 65536:
                raise ConfigError(f"{name} must be a port number (an int), got {value!r}.")
        if self.pg_port in FORBIDDEN_PG_PORTS:
            raise ConfigError(
                f"Port {self.pg_port} belongs to a shared development cluster; budgetapp uses its "
                f"own cluster on {PG_PORT}."
            )
        if self.web_host != LOOPBACK:
            raise ConfigError("The web app may only listen on 127.0.0.1 (SEC-1).")
        if self.pg_host != LOOPBACK:
            raise ConfigError("PostgreSQL connections must use 127.0.0.1 (SEC-1).")
        if self.env == "test":
            if not self.db_name.startswith(TEST_DB_PREFIX) or self.db_role != "budgetapp_test":
                raise ConfigError(
                    "The test environment must use a budgetapp_test_* database as budgetapp_test."
                )
            if self.plaid_env != "sandbox":
                raise ConfigError("The test environment always uses the Plaid Sandbox (or a fake).")
        else:
            expected = _DB[self.env]
            if (self.db_name, self.db_role) != expected:
                raise ConfigError(
                    f"The {self.env} environment always uses database {expected[0]!r} as role "
                    f"{expected[1]!r}."
                )
            if self.plaid_env != self.env:
                raise ConfigError(f"The {self.env} environment must use the {self.env} Plaid API.")
        if self.plaid_env not in ("sandbox", "production"):
            raise ConfigError(f"Unknown Plaid environment {self.plaid_env!r}.")
        for name in (
            "data_dir",
            "logs_dir",
            "state_dir",
            "pg_socket_dir",
            "pg_data_dir",
            "backups_dir",
            "install_dir",
        ):
            check_private_path(getattr(self, name), name, repo_dir=self.repo_dir)


def protected_folders(home: Path | None = None) -> tuple[Path, ...]:
    """Folders that macOS privacy protection (TCC) or iCloud sync covers (ARCHITECTURE §2)."""
    home = home or Path.home()
    return (
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Library" / "Mobile Documents",
        home / "Library" / "CloudStorage",
    )


def _is_within(path: Path, parent: Path) -> bool:
    """``path`` is ``parent`` or below it, ignoring case (macOS volumes are case-insensitive)."""
    try:
        Path(str(path).casefold()).relative_to(Path(str(parent).casefold()))
    except ValueError:
        return False
    return True


def check_private_path(path: Path, label: str = "path", *, repo_dir: Path | None = None) -> None:
    """Refuse app data paths that are relative, inside the repository or in a protected folder.

    The comparison resolves symlinks and ignores case, because the default APFS volume is
    case-insensitive (``~/desktop`` is ``~/Desktop``).
    """
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path, got {path}.")
    resolved = path.resolve(strict=False)
    for folder in protected_folders():
        for candidate in {folder, folder.resolve(strict=False)}:
            if _is_within(resolved, candidate):
                raise ConfigError(
                    f"{label} ({path}) is inside {candidate}, which macOS protects or iCloud "
                    "syncs; app data must live in ~/Library/Application Support/budgetapp."
                )
    if repo_dir is not None and _is_within(resolved, repo_dir.resolve(strict=False)):
        raise ConfigError(f"{label} ({path}) must not be inside the git checkout ({repo_dir}).")


def find_repo_dir() -> Path | None:
    """The source checkout this code runs from, or ``None`` for the Application Support install."""
    override = os.environ.get("BUDGETAPP_REPO_DIR")
    if override:
        return Path(override).expanduser().resolve()
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "pyproject.toml").is_file() and (candidate / "budgetapp").is_dir():
        return candidate
    return None


def default_data_dir() -> Path:
    override = os.environ.get("BUDGETAPP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def default_logs_dir() -> Path:
    override = os.environ.get("BUDGETAPP_LOGS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / APP_NAME


def resolve_env(env: str | None = None) -> str:
    """The environment name from the argument, ``$BUDGETAPP_ENV`` or the ``sandbox`` default."""
    chosen = env or os.environ.get("BUDGETAPP_ENV") or "sandbox"
    if chosen not in ENVIRONMENTS:
        raise ConfigError(f"Unknown environment {chosen!r}; use one of {ENVIRONMENTS}.")
    return chosen


def load_settings(env: str | None = None, **overrides: Any) -> Settings:
    """Build validated :class:`Settings` for ``env`` (default: ``$BUDGETAPP_ENV`` or sandbox).

    ``overrides`` replace individual fields (tests use them to point paths at temp dirs). When
    ``data_dir`` is overridden, the paths derived from it follow unless overridden too.

    For ``env="test"`` without a ``data_dir`` override, ``state_dir``, ``logs_dir``,
    ``backups_dir`` and ``install_dir`` default to :func:`scratch_dir_for_tests` (never the real
    folders); the cluster paths stay real so tests can reach the cluster.
    """
    env = resolve_env(env)
    field_names = {f.name for f in dataclasses.fields(Settings)}
    unknown = set(overrides) - field_names
    if unknown:
        raise ConfigError(f"Unknown settings: {', '.join(sorted(unknown))}")

    data_dir_override = overrides.pop("data_dir", None)
    data_dir = Path(data_dir_override or default_data_dir())
    if env == "test":
        db_name, db_role = f"{TEST_DB_PREFIX}{os.getpid()}", "budgetapp_test"
        plaid_env = "sandbox"
    else:
        db_name, db_role = _DB[env]
        plaid_env = env
    pg_bin = os.environ.get("BUDGETAPP_PG_BIN")
    values: dict[str, Any] = {
        "env": env,
        "repo_dir": find_repo_dir(),
        "data_dir": data_dir,
        "logs_dir": default_logs_dir(),
        "state_dir": data_dir / "state",
        "pg_bin_dir": Path(pg_bin).expanduser() if pg_bin else DEFAULT_PG_BIN,
        "pg_port": PG_PORT,
        "pg_host": LOOPBACK,
        "pg_socket_dir": data_dir / "run",
        "pg_data_dir": data_dir / "postgres",
        "web_host": LOOPBACK,
        "web_port": _WEB_PORT[env],
        "backups_dir": data_dir / "backups" / env,
        "install_dir": data_dir / "venv",
        "db_name": db_name,
        "db_role": db_role,
        "plaid_env": plaid_env,
    }
    if env == "test":
        scratch = scratch_dir_for_tests()
        values["logs_dir"] = scratch / "logs"
        if not data_dir_override:
            values["state_dir"] = scratch / "state"
            values["backups_dir"] = scratch / "backups"
            values["install_dir"] = scratch / "venv"
    values.update(overrides)
    settings = Settings(**values)
    settings.validate()
    return settings
