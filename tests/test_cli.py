import argparse

import pytest

from budgetapp import cli
from budgetapp import secrets as sec
from budgetapp.commands import NOT_BUILT, setup
from budgetapp.commands import db as db_command
from budgetapp.commands import migrate as migrate_command
from budgetapp.commands import test as test_command
from budgetapp.config import ConfigError, load_settings
from budgetapp.plaid_gateway import PlaidError

ALL_COMMANDS = {
    "start",
    "serve",
    "demo",
    "setup",
    "db",
    "migrate",
    "sync",
    "sandbox-link",
    "sandbox-refresh",
    "sandbox-reset-login",
    "import-csv",
    "recategorize",
    "backup",
    "restore",
    "doctor",
    "install-agents",
    "uninstall-agents",
    "notify-test",
    "test",
}
STUBS = {  # start/serve are built (tests/web/test_serve.py), demo too (tests/test_demo.py)
    "sync": ["--if-due", "--unattended", "--item", "3"],
    "sandbox-link": ["--user", "custom", "--institution", "ins_109508"],
    "sandbox-refresh": [],
    "sandbox-reset-login": ["--item", "1"],
    "import-csv": ["activity.csv", "--account", "2", "--dry-run"],
    "backup": ["--if-due", "--keep", "30", "--unattended"],
    "restore": ["backup.dump", "--force-disconnect"],
    "doctor": [],
    "install-agents": ["--with-web", "--dry-run"],
    "uninstall-agents": ["--dry-run"],
}


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for name in ALL_COMMANDS:
        assert name in out
    subparsers = next(
        a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert set(subparsers.choices) == ALL_COMMANDS


@pytest.mark.parametrize(("command", "options"), STUBS.items())
def test_stubs_print_not_built_and_return_2(capsys, command, options):
    assert cli.main([command, *options]) == 2
    assert capsys.readouterr().out.strip() == NOT_BUILT


def test_env_is_accepted_before_or_after_the_command():
    parser = cli.build_parser()
    args, _ = parser.parse_known_args(["start", "--env", "production"])
    assert args.env == "production"
    args, _ = parser.parse_known_args(["--env", "production", "sync"])
    assert args.env == "production"
    args, _ = parser.parse_known_args(["sync"])
    assert args.env == "test"  # $BUDGETAPP_ENV under pytest


def test_unknown_arguments_are_rejected_except_for_passthrough_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--bogus"])
    args, extra = cli.build_parser().parse_known_args(["test", "-k", "money", "--sandbox"])
    assert args.sandbox and extra == ["-k", "money"]


def test_pytest_command():
    args = argparse.Namespace(sandbox=False, extra=["-x"])
    assert test_command.pytest_command(args)[2:] == ["pytest", "-m", "not sandbox", "-x"]
    args.sandbox = True
    assert test_command.pytest_command(args)[3:5] == ["-m", "sandbox"]


def test_db_url_hides_the_password(capsys, secrets, monkeypatch):
    secrets.values["db.password.budgetapp_dev"] = "very-secret-password"
    assert cli.main(["db", "url", "--env", "sandbox"]) == 0
    out = capsys.readouterr().out
    assert "very-secret-password" not in out and "budgetapp_dev" in out and "***" in out


def test_db_psql_passes_the_password_in_the_environment_only(secrets, monkeypatch):
    secrets.values["db.password.budgetapp_live"] = "live-password-123"
    argv, env = db_command.psql_command(load_settings("production"))
    assert "live-password-123" not in " ".join(argv)
    assert env == {"PGPASSWORD": "live-password-123"}
    assert "require_auth=scram-sha-256" in argv[-1] and "user=budgetapp_live" in argv[-1]
    with pytest.raises(ConfigError, match="superuser"):
        db_command.psql_command(load_settings("sandbox"), admin=True)
    monkeypatch.setenv("BUDGETAPP_ENV", "sandbox")  # as ./run db psql --admin runs it
    admin_argv, admin_env = db_command.psql_command(load_settings("sandbox"), admin=True)
    assert admin_env == {} and "dbname=postgres" in admin_argv[-1]


def test_db_status_reports_the_running_cluster(capsys):
    assert cli.main(["db", "status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "5434" in out and "server version : 18" in out


class TestSetupNeverUpgradesProduction:
    def _fake_env(self, monkeypatch, current):
        migrated = []
        monkeypatch.setattr(setup, "make_engine", lambda settings: _Disposable())
        monkeypatch.setattr(setup, "current_revision", lambda engine: current)
        monkeypatch.setattr(
            setup, "migrate_env", lambda settings: migrated.append(settings.env) or (None, "x")
        )
        return migrated

    def test_an_existing_production_database_is_only_reported(self, monkeypatch):
        migrated = self._fake_env(monkeypatch, current="0002_seed_defaults")
        line = setup.migrate_database(load_settings("production"))
        assert migrated == [] and "never upgrades production" in line
        assert "0002_seed_defaults" in line

    def test_the_first_setup_creates_the_production_schema(self, monkeypatch):
        migrated = self._fake_env(monkeypatch, current=None)
        setup.migrate_database(load_settings("production"))
        assert migrated == ["production"]

    def test_sandbox_is_always_migrated(self, monkeypatch):
        migrated = self._fake_env(monkeypatch, current="0001_initial")
        setup.migrate_database(load_settings("sandbox"))
        assert migrated == ["sandbox"]


class _Disposable:
    def dispose(self):
        pass


def test_migrate_refuses_production_from_a_checkout(capsys, monkeypatch):
    monkeypatch.setattr(
        migrate_command, "migrate_env", lambda *a: pytest.fail("migrated production")
    )
    settings = load_settings("production")
    assert settings.repo_dir is not None
    args = argparse.Namespace(revision="head")
    assert migrate_command.run(args, settings) == 2
    assert "installed copy" in capsys.readouterr().out


class TestPlaidKeyPrompts:
    def test_shape_checks(self):
        assert setup.check_key_shape("0123456789abcdef01234567", "client_id") == (False, None)
        assert setup.check_key_shape("0123456789abcdef0123456789abcd", "secret") == (False, None)
        assert setup.check_key_shape("abc def", "secret")[0] is True
        assert setup.check_key_shape("access-sandbox-1234567890", "secret")[0] is True
        assert setup.check_key_shape("short", "secret")[0] is True
        fatal, message = setup.check_key_shape("0123456789ABCDEFGHIJ", "client_id")
        assert fatal is False and "24 hexadecimal" in message

    def test_prompts_save_keys_and_keep_existing(self, secrets, capsys):
        answers = iter(["0123456789abcdef01234567", "0123456789abcdef0123456789abcd", ""])
        assert setup.prompt_plaid_keys(input_fn=lambda _: next(answers), store=secrets)
        assert secrets.values["plaid.client_id"] == "0123456789abcdef01234567"
        assert secrets.values["plaid.secret.sandbox"] == "0123456789abcdef0123456789abcd"
        assert "plaid.secret.production" not in secrets.values
        out = capsys.readouterr().out
        assert "0123456789abcdef01234567" not in out  # never echoed
        again = iter(["", "", ""])
        assert setup.prompt_plaid_keys(input_fn=lambda _: next(again), store=secrets)
        assert "kept the saved value" in capsys.readouterr().out

    def test_unusual_shapes_need_confirmation(self, secrets):
        answers = iter(["ZZZZZZZZZZZZZZZZZZZZ", "0123456789abcdef01234567", "x" * 30, "", ""])
        confirms = iter(["n", "y"])
        ok = setup.prompt_plaid_keys(
            input_fn=lambda _: next(answers), confirm_fn=lambda _: next(confirms), store=secrets
        )
        assert ok
        assert secrets.values["plaid.client_id"] == "0123456789abcdef01234567"
        assert secrets.values["plaid.secret.sandbox"] == "x" * 30

    def test_required_key_gives_up_after_three_tries(self, secrets):
        answers = iter([""] * 7)  # 3 tries for each required key, then skip the optional one
        assert not setup.prompt_plaid_keys(input_fn=lambda _: next(answers), store=secrets)
        assert secrets.values == {}


class FakeItemGateway:
    def __init__(self, error=None):
        self.error = error

    def get_item(self, token):
        if self.error:
            raise self.error
        return {"item": {"item_id": "item-prod-1", "institution_id": "ins_10"}}


class TestAccessTokenReentry:
    TOKEN = "access-production-11111111-2222-3333-4444-555555555555"

    def _settings(self, tmp_path):
        return load_settings("production", state_dir=tmp_path / "state")

    def test_verified_token_is_saved_once(self, tmp_path, secrets, capsys):
        settings = self._settings(tmp_path)
        rc = setup.enter_access_token(
            settings, input_fn=lambda _: self.TOKEN, gateway=FakeItemGateway(), store=secrets
        )
        assert rc == 0 and secrets.values["plaid.access_token.production"] == self.TOKEN
        assert self.TOKEN not in capsys.readouterr().out
        assert (
            setup.enter_access_token(
                settings, input_fn=lambda _: self.TOKEN, gateway=FakeItemGateway(), store=secrets
            )
            == 0
        )

    def test_reentering_a_token_from_a_recovery_file_unblocks_linking(
        self, tmp_path, secrets, capsys
    ):
        settings = self._settings(tmp_path)
        secrets.fail_on[("set", "plaid.access_token.production")] = RuntimeError("Keychain")
        with pytest.raises(sec.TokenNotSavedError):
            sec.store_access_token(settings, "item-prod-1", self.TOKEN)
        assert sec.production_link_blockers(settings)
        del secrets.fail_on[("set", "plaid.access_token.production")]
        rc = setup.enter_access_token(
            settings, input_fn=lambda _: self.TOKEN, gateway=FakeItemGateway(), store=secrets
        )
        assert rc == 0 and sec.unsaved_production_tokens(settings) == []
        assert "Marked the recovery file as saved" in capsys.readouterr().out
        assert [("re-attach" in r) for r in sec.production_link_blockers(settings)] == [True]

    def test_rejected_or_conflicting_tokens_are_not_saved(self, tmp_path, secrets):
        settings = self._settings(tmp_path)
        bad = FakeItemGateway(PlaidError(400, "INVALID_INPUT", "INVALID_ACCESS_TOKEN", "bad"))
        assert (
            setup.enter_access_token(
                settings, input_fn=lambda _: self.TOKEN, gateway=bad, store=secrets
            )
            == 1
        )
        assert "plaid.access_token.production" not in secrets.values
        secrets.values["plaid.access_token.production"] = self.TOKEN + "-other"
        assert (
            setup.enter_access_token(
                settings, input_fn=lambda _: self.TOKEN, gateway=FakeItemGateway(), store=secrets
            )
            == 1
        )
        assert secrets.values["plaid.access_token.production"] == self.TOKEN + "-other"

    def test_sandbox_tokens_and_wrong_env_are_refused(self, tmp_path, secrets):
        settings = self._settings(tmp_path)
        sandbox_token = "access-sandbox-11111111-2222-3333-4444-555555555555"
        assert (
            setup.enter_access_token(
                settings, input_fn=lambda _: sandbox_token, gateway=FakeItemGateway(), store=secrets
            )
            == 1
        )
        assert (
            setup.enter_access_token(
                load_settings("sandbox"),
                input_fn=lambda _: self.TOKEN,
                gateway=FakeItemGateway(),
                store=secrets,
            )
            == 2
        )
