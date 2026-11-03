import os
import subprocess
import sys

import pytest

from budgetapp import pgcluster
from budgetapp.config import ConfigError, PostgresNotFound, load_settings
from budgetapp.pgcluster import ClusterError


@pytest.fixture
def scratch_cluster(tmp_path):
    """Sandbox settings whose data directory is a throwaway folder (never the real cluster)."""
    real = load_settings("sandbox")
    settings = load_settings("sandbox", data_dir=tmp_path, pg_socket_dir=real.pg_socket_dir)
    settings.pg_data_dir.mkdir()
    (settings.pg_data_dir / "PG_VERSION").write_text("18\n")
    return settings


def test_config_quotes_the_private_socket_dir():
    settings = load_settings("sandbox")
    conf = pgcluster.render_conf(settings)
    assert "port = 5434" in conf
    assert "listen_addresses = 'localhost'" in conf
    assert f"""unix_socket_directories = '"{settings.pg_socket_dir}"'""" in conf
    assert "unix_socket_permissions = 0700" in conf
    assert "password_encryption = 'scram-sha-256'" in conf


def test_hba_has_peer_map_and_scram_only():
    hba = pgcluster.render_hba()
    rules = [line.split() for line in hba.splitlines() if line and not line.startswith("#")]
    assert rules == [
        ["local", "all", "all", "peer", "map=localmap"],
        ["host", "all", "all", "127.0.0.1/32", "scram-sha-256"],
        ["host", "all", "all", "::1/128", "scram-sha-256"],
    ]
    assert "trust" not in hba


def test_ident_maps_the_mac_user_to_itself_and_the_test_role():
    ident = pgcluster.render_ident("alice")
    rules = [line.split() for line in ident.splitlines() if line and not line.startswith("#")]
    assert rules == [["localmap", "alice", "alice"], ["localmap", "alice", "budgetapp_test"]]


def test_socket_path_limit(tmp_path):
    long_dir = tmp_path / ("x" * 120)
    settings = load_settings("sandbox", pg_socket_dir=long_dir)
    with pytest.raises(ClusterError, match="103"):
        pgcluster.check_socket_path(settings)
    pgcluster.check_socket_path(load_settings("sandbox"))


def test_port_owned_by_another_data_directory_is_refused(monkeypatch):
    settings = load_settings("sandbox")
    monkeypatch.setattr(pgcluster, "listener_pids", lambda port: [111])
    monkeypatch.setattr(pgcluster, "process_command", lambda pid: "/opt/x/postgres -D /elsewhere")
    assert "not the budgetapp cluster" in pgcluster.port_conflict(settings)
    monkeypatch.setattr(
        pgcluster, "process_command", lambda pid: f"/opt/x/postgres -D {settings.pg_data_dir}"
    )
    assert pgcluster.port_conflict(settings) is None
    monkeypatch.setattr(pgcluster, "listener_pids", lambda port: [])
    assert pgcluster.port_conflict(settings) is None


def test_start_refuses_a_foreign_server(monkeypatch, scratch_cluster):
    monkeypatch.setattr(pgcluster, "is_running", lambda s: False)
    monkeypatch.setattr(pgcluster, "port_conflict", lambda s: "Port 5434 is used by someone else")
    with pytest.raises(ClusterError, match="someone else"):
        pgcluster.start(scratch_cluster)
    with pytest.raises(ClusterError, match="someone else"):
        pgcluster.init_cluster(scratch_cluster)


def test_shared_cluster_ports_are_refused(scratch_cluster):
    object.__setattr__(scratch_cluster, "pg_port", 5432)  # bypass validation on purpose
    with pytest.raises(ClusterError, match="shared port"):
        pgcluster.start(scratch_cluster)
    with pytest.raises(ClusterError, match="shared port"):
        pgcluster.stop(scratch_cluster)


class TestTestProcessGuards:
    """Test code may read the cluster's status but never manage or administer the real one."""

    @pytest.mark.parametrize("env", ["test", "sandbox", "production"])
    def test_the_real_cluster_cannot_be_stopped_or_reconfigured(self, monkeypatch, env):
        settings = load_settings(env)
        monkeypatch.setattr(pgcluster, "_run", lambda *a, **k: pytest.fail("ran pg_ctl/initdb"))
        for action in (pgcluster.stop, pgcluster.reload, pgcluster.init_cluster):
            with pytest.raises(ClusterError, match="from the test environment"):
                action(settings)
        monkeypatch.setattr(pgcluster, "is_running", lambda s: False)
        with pytest.raises(ClusterError, match="from the test environment"):
            pgcluster.start(settings)

    def test_starting_a_running_cluster_is_a_harmless_no_op(self, settings):
        assert pgcluster.start(settings) == "already running"

    def test_roles_and_databases_need_the_superuser(self, settings, secrets):
        with pytest.raises(ConfigError, match="superuser"):
            pgcluster.ensure_roles_and_databases(settings, store=secrets)
        assert secrets.values == {}  # no password was generated or changed


class TestStalePidFile:
    @pytest.fixture
    def stranger(self):
        """A process of ours that is not postgres (it reports SIGINT if it gets one)."""
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, lambda *a: (print('got SIGINT', flush=True), "
            "sys.exit(9)))\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "ready"
        yield proc
        proc.kill()
        proc.wait()
        proc.stdout.close()

    def _write_pid(self, settings, pid):
        (settings.pg_data_dir / "postmaster.pid").write_text(f"{pid}\n{settings.pg_data_dir}\n")

    def test_stop_never_signals_a_process_that_reused_the_pid(self, scratch_cluster, stranger):
        self._write_pid(scratch_cluster, stranger.pid)
        assert pgcluster.stop(scratch_cluster, timeout=3) == "not running (stale postmaster.pid)"
        assert stranger.poll() is None  # still running: no SIGINT was sent

    def test_start_removes_a_stale_pid_file_first(self, scratch_cluster, stranger, monkeypatch):
        self._write_pid(scratch_cluster, stranger.pid)
        calls = []
        monkeypatch.setattr(pgcluster, "is_running", lambda s: False)
        monkeypatch.setattr(pgcluster, "port_conflict", lambda s: None)
        monkeypatch.setattr(pgcluster, "agent_loaded", lambda label=None: False)
        monkeypatch.setattr(pgcluster, "wait_for_db", lambda s, timeout=0: True)
        monkeypatch.setattr(pgcluster, "_run", lambda args, **k: calls.append(args))
        scratch_cluster.logs_dir.mkdir(parents=True, exist_ok=True)
        assert pgcluster.start(scratch_cluster) == "started"
        assert not (scratch_cluster.pg_data_dir / "postmaster.pid").exists()
        assert calls and calls[0][-1] == "start"
        assert stranger.poll() is None

    def test_an_unknown_live_process_is_never_called_stale(self, scratch_cluster, monkeypatch):
        self._write_pid(scratch_cluster, os.getpid())
        monkeypatch.setattr(pgcluster, "process_command", lambda pid: None)  # ps failed
        assert pgcluster.remove_stale_pid_file(scratch_cluster) is False
        assert (scratch_cluster.pg_data_dir / "postmaster.pid").exists()

    def test_only_the_postgres_program_counts_as_ours(self, scratch_cluster, monkeypatch):
        data = scratch_cluster.pg_data_dir
        commands = {
            1: f"/opt/homebrew/opt/postgresql@18/bin/postgres -D {data}",
            2: f"postgres -D {data}",
            3: f"/usr/bin/tail -f {data}/log/postgresql.log",
            4: "/opt/homebrew/opt/postgresql@18/bin/postgres -D /elsewhere",
        }
        monkeypatch.setattr(pgcluster, "process_command", commands.get)
        assert [pgcluster.is_our_postgres(pid, scratch_cluster) for pid in commands] == [
            True,
            True,
            False,
            False,
        ]

    def test_a_pid_file_of_a_dead_process_is_stale(self, scratch_cluster):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self._write_pid(scratch_cluster, dead.pid)
        assert pgcluster.stop(scratch_cluster) == "not running"
        assert pgcluster.remove_stale_pid_file(scratch_cluster) is True
        assert pgcluster.remove_stale_pid_file(scratch_cluster) is False


class TestIncludeLine:
    def test_include_fails_closed_and_replaces_the_old_line_in_place(self):
        old = "a = 1\n#port = 5432\ninclude_if_exists = 'budgetapp.conf'\nb = 2\n"
        new = pgcluster.include_main_conf(old)
        assert new.splitlines() == ["a = 1", "#port = 5432", "include = 'budgetapp.conf'", "b = 2"]
        assert pgcluster.include_main_conf(new) == new  # idempotent

    def test_a_missing_line_is_appended_once(self):
        new = pgcluster.include_main_conf("a = 1\n")
        assert new.count("include = 'budgetapp.conf'") == 1
        assert pgcluster.include_main_conf(new) == new

    def test_write_config_on_a_scratch_cluster(self, scratch_cluster):
        main = scratch_cluster.pg_data_dir / "postgresql.conf"
        main.write_text("#port = 5432\ninclude_if_exists = 'budgetapp.conf'\n")
        assert pgcluster.write_config(scratch_cluster) is True
        assert "include = 'budgetapp.conf'" in main.read_text()
        assert "include_if_exists" not in main.read_text()
        assert pgcluster.write_config(scratch_cluster) is False


class TestMissingPostgres:
    def test_friendly_errors_instead_of_tracebacks(self, scratch_cluster, tmp_path):
        missing = scratch_cluster.replace(pg_bin_dir=tmp_path / "nobin")
        for action in (pgcluster.init_cluster, pgcluster.start, pgcluster.status):
            with pytest.raises(PostgresNotFound, match="brew install postgresql@18"):
                action(missing)

    def test_run_turns_os_errors_into_cluster_errors(self, tmp_path):
        with pytest.raises(ClusterError, match="Could not run"):
            pgcluster._run([str(tmp_path / "nothing-here")])


def test_the_real_cluster_is_ours_and_running(settings):
    status = pgcluster.status(settings)
    assert status.initialized and status.running and status.pid
    assert status.port == 5434 and status.port_conflict is None
    assert status.server_version.startswith("18")  # read as budgetapp_test, not the superuser
    assert pgcluster.is_our_postgres(status.pid, settings)
    assert pgcluster.port_conflict(settings) is None
    assert os.environ["BUDGETAPP_ENV"] == "test"
