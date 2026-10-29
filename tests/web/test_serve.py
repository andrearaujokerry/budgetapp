"""``start`` (``./run``) and ``serve``, including ``serve --demo`` (ARCHITECTURE §9, §10).

Nothing here starts a real server, the cluster or a browser: those calls are replaced, and
production settings are only ever handed to code paths that refuse or hand over to a fake.
"""

from __future__ import annotations

import argparse
import dataclasses
import socket
import types

import pytest
import uvicorn
from fastapi import FastAPI

from budgetapp import cli
from budgetapp import secrets as app_secrets
from budgetapp.commands import serve
from budgetapp.config import ConfigError, load_settings
from budgetapp.secrets import MemoryStore, SecretStoreError
from budgetapp.services.notify import NullNotifier


def serve_args(**values) -> argparse.Namespace:
    defaults = {"demo": None, "port": None, "unattended": False, "verbose": False}
    return argparse.Namespace(**{**defaults, **values})


@pytest.fixture
def served(monkeypatch):
    """Replace ``run_server``: record what would be served instead of serving it."""
    calls: list[dict] = []

    def fake_run_server(app, settings, *, open_browser=False, verbose=False):
        calls.append({"app": app, "settings": settings, "open_browser": open_browser})
        return 0

    monkeypatch.setattr(serve, "run_server", fake_run_server)
    monkeypatch.setattr(serve, "wait_for_db", lambda settings, timeout=0: True)
    return calls


@pytest.fixture
def demo_modules(monkeypatch):
    """Stand-ins for ``budgetapp.devtools.demo`` / ``demo_gateway`` (the demo agent's contract)."""

    class DemoError(Exception):
        pass

    class DemoPlaidGateway:
        plaid_env = "sandbox"

    requested: list[tuple[str, int]] = []

    def demo_settings(name, *, port=8800):
        requested.append((name, port))
        if name == "missing":
            raise DemoError(f"No demo database for {name!r}: run ./run demo create {name} first.")
        return load_settings("test", db_name=f"budgetapp_test_demo_{name}", web_port=port)

    demo = types.SimpleNamespace(demo_settings=demo_settings, DemoError=DemoError)
    gateway = types.SimpleNamespace(DemoPlaidGateway=DemoPlaidGateway)
    monkeypatch.setattr(serve, "load_demo_tools", lambda: (demo, gateway))
    return types.SimpleNamespace(requested=requested, gateway=DemoPlaidGateway)


# --------------------------------------------------------------------------- refusals


def test_start_and_serve_refuse_the_test_environment(capsys):
    assert cli.main(["start"]) == 2
    assert cli.main(["serve"]) == 2
    assert capsys.readouterr().err.count("./run serve --demo NAME") == 2


def test_port_works_only_with_demo(capsys):
    assert serve.run_serve(serve_args(port=9000), load_settings("sandbox")) == 2
    assert "--port works only with --demo" in capsys.readouterr().err


def test_demo_is_never_mixed_with_production(capsys):
    assert serve.run_serve(serve_args(demo="shell"), load_settings("production")) == 2
    assert "Sandbox-only" in capsys.readouterr().err


def test_production_is_never_served_from_a_checkout(capsys, served):
    settings = load_settings("production")
    assert settings.repo_dir is not None  # the tests run from the repository
    assert serve.run_serve(serve_args(), settings) == 2
    assert "Refusing to serve the PRODUCTION app from a git checkout" in capsys.readouterr().err
    assert served == []


def test_run_server_refuses_anything_but_loopback(settings):
    exposed = dataclasses.replace(settings, web_host="0.0.0.0")  # skips validation on purpose
    with pytest.raises(ConfigError, match="127.0.0.1"):
        serve.run_server(FastAPI(), exposed)


def test_run_server_reports_a_busy_port(settings, capsys, monkeypatch):
    monkeypatch.setattr(serve, "setup_logging", lambda *a, **k: None)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert not serve.port_available("127.0.0.1", port)
        assert serve.run_server(FastAPI(), settings.replace(web_port=port)) == 1
    assert f"127.0.0.1:{port} is already in use" in capsys.readouterr().err
    assert serve.port_available("127.0.0.1", port)


# --------------------------------------------------------------------------- start


def test_sandbox_start_brings_everything_up_then_serves(capsys, served, monkeypatch):
    steps = []
    monkeypatch.setattr(serve.pgcluster, "start", lambda s: steps.append("cluster") or "running")
    monkeypatch.setattr(serve, "migrate_env", lambda s: steps.append("migrate") or ("0003", "0003"))
    settings = load_settings("sandbox")
    assert serve.run_start(argparse.Namespace(no_browser=False), settings) == 0
    assert steps == ["cluster", "migrate"]
    [call] = served
    assert isinstance(call["app"], FastAPI) and call["app"].state.settings is settings
    assert call["open_browser"] is True
    out = capsys.readouterr().out
    assert "Sandbox database budgetapp_sandbox: up to date" in out
    assert "./run setup --keys-only" in out  # no Plaid keys saved (the test store is empty)


def test_start_without_browser_and_with_a_migration(capsys, served, monkeypatch):
    monkeypatch.setattr(serve.pgcluster, "start", lambda s: "started")
    monkeypatch.setattr(serve, "migrate_env", lambda s: ("0002_seed_defaults", "0003_x"))
    assert serve.run_start(argparse.Namespace(no_browser=True), load_settings("sandbox")) == 0
    assert served[0]["open_browser"] is False
    assert "0002_seed_defaults → 0003_x" in capsys.readouterr().out


def test_start_stops_when_the_database_never_comes_up(capsys, served, monkeypatch):
    monkeypatch.setattr(serve.pgcluster, "start", lambda s: "started")
    monkeypatch.setattr(serve, "wait_for_db", lambda settings, timeout=0: False)
    assert serve.run_start(argparse.Namespace(no_browser=False), load_settings("sandbox")) == 1
    assert "did not become ready" in capsys.readouterr().err and served == []


def test_live_prints_the_banner_and_hands_over(capsys, monkeypatch):
    handed = []
    monkeypatch.setattr(serve.agents, "launch_production", lambda s: handed.append(s) or 0)
    settings = load_settings("production")
    assert serve.run_start(argparse.Namespace(no_browser=False), settings) == 0
    assert handed == [settings]
    out = capsys.readouterr().out
    assert "LIVE DATA: this is your real American Express account." in out
    assert "http://127.0.0.1:8765" in out


def test_live_explains_what_is_not_built_yet(capsys, monkeypatch):
    def not_yet(_settings):
        raise NotImplementedError("Starting the production app isn't built yet (OPS adds it).")

    monkeypatch.setattr(serve.agents, "launch_production", not_yet)
    assert serve.run_start(argparse.Namespace(no_browser=False), load_settings("production")) == 2
    assert "error: Starting the production app isn't built yet" in capsys.readouterr().err


# --------------------------------------------------------------------------- serve


def test_sandbox_serve(served):
    assert serve.run_serve(serve_args(), load_settings("sandbox")) == 0
    [call] = served
    assert call["settings"].web_port == 8766 and call["open_browser"] is False


def test_serve_needs_the_database(capsys, served, monkeypatch):
    monkeypatch.setattr(serve, "wait_for_db", lambda settings, timeout=0: False)
    assert serve.run_serve(serve_args(), load_settings("sandbox")) == 1
    assert "./run db start" in capsys.readouterr().err and served == []


def test_unattended_turns_off_keychain_dialogs(served, monkeypatch):
    calls = []
    monkeypatch.setattr(serve.secrets, "enable_unattended_mode", lambda: calls.append(1) or True)
    assert serve.run_serve(serve_args(unattended=True), load_settings("sandbox")) == 0
    assert calls == [1]


def test_plaid_key_warning(secrets):
    sandbox = load_settings("sandbox")
    assert "./run setup --keys-only" in serve.plaid_key_warning(sandbox)
    secrets.values.update({"plaid.client_id": "client", "plaid.secret.sandbox": "secret"})
    assert serve.plaid_key_warning(sandbox) is None
    secrets.fail_on[("get", None)] = SecretStoreError("locked")
    assert "Couldn't check the Plaid keys" in serve.plaid_key_warning(sandbox)


# --------------------------------------------------------------------------- serve --demo


def test_demo_serves_the_demo_database_with_no_network_and_no_keychain(
    capsys, served, demo_modules
):
    assert cli.main(["serve", "--demo", "shell", "--port", "8801"]) == 0
    assert demo_modules.requested == [("shell", 8801)]
    [call] = served
    app, settings = call["app"], call["settings"]
    assert settings.db_name == "budgetapp_test_demo_shell" and settings.web_port == 8801
    assert app.state.settings is settings
    assert app.state.demo_name == "shell"  # render() passes env="demo"
    assert isinstance(app.state.gateway_factory(settings), demo_modules.gateway)
    assert isinstance(app.state.secret_store, MemoryStore)
    assert app_secrets.get_store() is app.state.secret_store  # the process never uses Keychain
    assert isinstance(app.state.notifier, NullNotifier)
    origin_check = [m for m in app.user_middleware if m.cls.__name__ == "HostOriginMiddleware"]
    assert origin_check[0].kwargs == {"web_port": 8801}  # Origin checks follow the port
    assert call["open_browser"] is False
    assert "Nothing here is real" in capsys.readouterr().out


def test_demo_port_defaults_to_8800(served, demo_modules):
    assert serve.run_serve(serve_args(demo="shell"), load_settings("sandbox")) == 0
    assert demo_modules.requested == [("shell", 8800)]


@pytest.mark.parametrize("port", [8765, 8766, 80])
def test_demo_cannot_take_the_real_apps_ports(capsys, served, demo_modules, port):
    assert serve.run_serve(serve_args(demo="shell", port=port), load_settings("sandbox")) == 2
    assert "pick another port" in capsys.readouterr().err and served == []


def test_demo_explains_a_missing_database(capsys, served, demo_modules):
    assert serve.run_serve(serve_args(demo="missing"), load_settings("sandbox")) == 1
    assert "./run demo create missing first" in capsys.readouterr().err and served == []


def test_demo_checks_the_cluster_first(capsys, served, demo_modules, monkeypatch):
    monkeypatch.setattr(serve, "wait_for_db", lambda settings, timeout=0: False)
    assert serve.run_serve(serve_args(demo="shell"), load_settings("sandbox")) == 1
    assert "./run db start" in capsys.readouterr().err and demo_modules.requested == []


def test_demo_without_devtools_says_so(capsys, served, monkeypatch):
    def missing():
        name = "budgetapp.devtools.demo"
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(serve, "load_demo_tools", missing)
    assert serve.run_serve(serve_args(demo="shell"), load_settings("sandbox")) == 2
    assert "budgetapp.devtools.demo is missing" in capsys.readouterr().err


def test_demo_does_not_hide_other_import_errors(served, monkeypatch):
    def broken():
        raise ModuleNotFoundError("No module named 'faker'", name="faker")

    monkeypatch.setattr(serve, "load_demo_tools", broken)
    with pytest.raises(ModuleNotFoundError, match="faker"):
        serve.run_serve(serve_args(demo="shell"), load_settings("sandbox"))


# --------------------------------------------------------------------------- the server itself


def test_uvicorn_settings(settings, monkeypatch):
    seen = {}

    def fake_run(self, sockets=None):
        seen["config"] = self.config
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(serve, "setup_logging", lambda *a, **k: seen.setdefault("logging", k))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    assert serve.run_server(FastAPI(), settings.replace(web_port=port)) == 0
    config = seen["config"]
    assert (config.host, config.port) == ("127.0.0.1", port)
    assert config.server_header is False and config.proxy_headers is False
    assert config.ws == "none" and config.lifespan == "on"
    assert config.log_config == serve.server_log_config()
    assert seen["logging"]["log_name"] is None  # test settings never write the real log folder


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_ctrl_c_is_a_normal_stop(settings, capsys, monkeypatch):
    def run_until_ctrl_c(self, sockets=None):
        self.started = True
        raise KeyboardInterrupt  # what uvicorn re-raises after its graceful shutdown

    monkeypatch.setattr(uvicorn.Server, "run", run_until_ctrl_c)
    monkeypatch.setattr(serve, "setup_logging", lambda *a, **k: None)
    assert serve.run_server(FastAPI(), settings.replace(web_port=free_port())) == 0
    assert capsys.readouterr().out.endswith("Budget Tracker stopped.\n")


def test_server_logs_go_through_the_redacting_root_handlers():
    loggers = serve.server_log_config()["loggers"]
    assert set(loggers) == {"uvicorn", "uvicorn.error", "uvicorn.access"}
    assert all(
        logger == {"handlers": [], "level": "INFO", "propagate": True}
        for logger in loggers.values()
    )


def test_load_demo_tools_imports_the_devtools_modules(monkeypatch):
    imported = []
    monkeypatch.setattr(
        serve.importlib, "import_module", lambda name: imported.append(name) or name
    )
    assert serve.load_demo_tools() == ("budgetapp.devtools.demo", "budgetapp.devtools.demo_gateway")


def test_the_browser_opens_once_the_server_is_up(monkeypatch):
    opened = []
    monkeypatch.setattr(serve.webbrowser, "open", opened.append)
    server = types.SimpleNamespace(started=True, should_exit=False)
    assert serve.open_browser_when_ready(server, "http://127.0.0.1:8766") is True
    assert opened == ["http://127.0.0.1:8766"]
    stopped = types.SimpleNamespace(started=False, should_exit=True)
    assert serve.open_browser_when_ready(stopped, "http://127.0.0.1:8766", timeout=1) is False
    assert opened == ["http://127.0.0.1:8766"]
