import pytest

from budgetapp import pgcluster
from budgetapp.config import load_settings
from budgetapp.pgcluster import ClusterError


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


def test_start_refuses_a_foreign_server(monkeypatch):
    settings = load_settings("sandbox")
    monkeypatch.setattr(pgcluster, "is_running", lambda s: False)
    monkeypatch.setattr(pgcluster, "port_conflict", lambda s: "Port 5434 is used by someone else")
    with pytest.raises(ClusterError, match="someone else"):
        pgcluster.start(settings)
    with pytest.raises(ClusterError, match="someone else"):
        pgcluster.init_cluster(settings)


def test_shared_cluster_ports_are_refused():
    settings = load_settings("sandbox")
    object.__setattr__(settings, "pg_port", 5432)  # bypass validation on purpose
    with pytest.raises(ClusterError):
        pgcluster.start(settings)
    with pytest.raises(ClusterError):
        pgcluster.stop(settings)


def test_the_real_cluster_is_ours_and_running(settings):
    status = pgcluster.status(settings)
    assert status.initialized and status.running and status.pid
    assert status.port == 5434 and status.port_conflict is None
    assert status.server_version is None or status.server_version.startswith("18")
    assert pgcluster.is_our_postgres(status.pid, settings)
    assert pgcluster.port_conflict(settings) is None
