import dataclasses
import os
from pathlib import Path

import pytest

from budgetapp.config import ConfigError, check_private_path, load_settings, resolve_env


def test_sandbox_defaults():
    s = load_settings("sandbox")
    assert (s.env, s.db_name, s.db_role, s.plaid_env) == (
        "sandbox",
        "budgetapp_sandbox",
        "budgetapp_dev",
        "sandbox",
    )
    assert (s.pg_host, s.pg_port, s.web_host, s.web_port) == ("127.0.0.1", 5434, "127.0.0.1", 8766)
    assert s.app_url == "http://127.0.0.1:8766"
    assert s.pg_socket_dir == s.data_dir / "run"
    assert s.pg_data_dir == s.data_dir / "postgres"
    assert s.state_dir == s.data_dir / "state"
    assert s.backups_dir == s.data_dir / "backups" / "sandbox"
    assert s.install_dir == s.data_dir / "venv"
    assert s.pg_bin("psql").endswith("/psql")


def test_production_defaults():
    s = load_settings("production")
    assert (s.db_name, s.db_role, s.web_port, s.plaid_env) == (
        "budgetapp",
        "budgetapp_live",
        8765,
        "production",
    )
    assert s.is_production and not s.is_test


def test_test_env_uses_a_per_process_database():
    s = load_settings("test")
    assert s.db_name == f"budgetapp_test_{os.getpid()}"
    assert s.db_role == "budgetapp_test"
    assert s.is_test


def test_env_comes_from_argument_then_variable_then_sandbox(monkeypatch):
    monkeypatch.setenv("BUDGETAPP_ENV", "production")
    assert resolve_env() == "production"
    assert resolve_env("sandbox") == "sandbox"
    monkeypatch.delenv("BUDGETAPP_ENV")
    assert resolve_env() == "sandbox"
    with pytest.raises(ConfigError):
        resolve_env("staging")


def test_data_dir_override_moves_derived_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDGETAPP_DATA_DIR", str(tmp_path / "data"))
    s = load_settings("sandbox")
    assert s.data_dir == tmp_path / "data"
    assert s.state_dir == tmp_path / "data" / "state"
    s2 = load_settings("sandbox", data_dir=tmp_path / "other", state_dir=tmp_path / "st")
    assert s2.pg_data_dir == tmp_path / "other" / "postgres"
    assert s2.state_dir == tmp_path / "st"


def test_settings_are_frozen():
    s = load_settings("sandbox")
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.env = "production"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"pg_port": 5432},
        {"pg_port": 5433},
        {"web_host": "0.0.0.0"},
        {"pg_host": "192.168.1.10"},
        {"db_name": "budgetapp"},
        {"db_role": "postgres"},
        {"plaid_env": "production"},
        {"nonsense": 1},
    ],
)
def test_unsafe_sandbox_overrides_are_refused(overrides):
    with pytest.raises(ConfigError):
        load_settings("sandbox", **overrides)


def test_test_env_database_must_be_a_test_database():
    with pytest.raises(ConfigError):
        load_settings("test", db_name="budgetapp")
    with pytest.raises(ConfigError):
        load_settings("test", db_role="budgetapp_live")
    assert load_settings("test", db_name="budgetapp_test_custom").db_name == "budgetapp_test_custom"


@pytest.mark.parametrize("folder", ["Desktop", "Documents", "Downloads"])
def test_data_in_protected_folders_is_refused(folder):
    with pytest.raises(ConfigError, match="protects or iCloud"):
        load_settings("sandbox", data_dir=Path.home() / folder / "budgetapp-data")


def test_icloud_and_relative_paths_are_refused():
    with pytest.raises(ConfigError):
        check_private_path(Path.home() / "Library" / "Mobile Documents" / "x")
    with pytest.raises(ConfigError):
        check_private_path(Path.home() / "Library" / "CloudStorage" / "Dropbox" / "x")
    with pytest.raises(ConfigError, match="absolute"):
        check_private_path(Path("relative/dir"))


def test_data_inside_the_repository_is_refused(tmp_path):
    assert load_settings("sandbox").repo_dir is not None
    with pytest.raises(ConfigError, match="git checkout"):
        check_private_path(tmp_path / "repo" / "data", repo_dir=tmp_path / "repo")
    check_private_path(tmp_path / "elsewhere", repo_dir=tmp_path / "repo")


def test_replace_validates():
    s = load_settings("sandbox")
    assert s.replace(web_port=9999).web_port == 9999
    with pytest.raises(ConfigError):
        s.replace(pg_port=5433)
