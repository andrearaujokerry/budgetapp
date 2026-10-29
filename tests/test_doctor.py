"""``./run doctor`` (SEC-1..6 and health checks; RESEARCH T5.6).

Every external program is faked (:class:`FakeMac`), the app folders are temporary, and the
database is the rolled-back test database (``session_factory``), even for production settings:
no test reads the Keychain, launchd, the real cluster's files or the production database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import os
import plistlib
import re
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from budgetapp import pgcluster
from budgetapp import secrets as app_secrets
from budgetapp.commands import doctor as doctor_command
from budgetapp.config import load_settings, scratch_dir_for_tests
from budgetapp.db import code_head
from budgetapp.services import agents, doctor
from budgetapp.services.doctor import FAIL, INFO, PASS, WARN
from tests.factories import make_item

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OLD_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
NOW = dt.datetime(2026, 10, 29, 15, 0, tzinfo=dt.UTC)
USER = "andre"
ALL_ITEMS = (
    app_secrets.PLAID_CLIENT_ID,
    "plaid.secret.sandbox",
    "plaid.secret.production",
    "db.password.budgetapp_dev",
    "db.password.budgetapp_live",
)
_SOCKETS = itertools.count()


def short_socket_dir() -> Path:
    """A socket folder short enough for macOS's 103-byte limit (pytest's tmp_path is not): in
    this process's scratch folder, which conftest removes."""
    return scratch_dir_for_tests() / f"s{next(_SOCKETS)}"


def store_with(*accounts: str, **failures) -> app_secrets.MemoryStore:
    return app_secrets.MemoryStore(
        values={a: f"value-of-{a}" for a in accounts},
        fail_on={("get", a): exc for a, exc in failures.items()},
    )


class RecordingStore:
    """A secret store that remembers which accounts were read."""

    def __init__(self, store: app_secrets.MemoryStore) -> None:
        self.store = store
        self.reads: list[str] = []

    def get(self, account: str) -> str | None:
        self.reads.append(account)
        return self.store.get(account)

    def set(self, account: str, value: str) -> None:  # pragma: no cover - doctor never writes
        raise AssertionError("doctor must not write to the Keychain")

    def delete(self, account: str) -> None:  # pragma: no cover
        raise AssertionError("doctor must not delete from the Keychain")


def netstat(*addresses: str) -> str:
    """``netstat -anv -p tcp`` output with a LISTEN line per local address."""
    header = (
        "Active Internet connections (including servers)\n"
        "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          "
        "rxbytes      txbytes  rhiwat  shiwat    pid   epid state  options\n"
    )
    lines = []
    for address in addresses:
        proto = "tcp6" if ":" in address else "tcp4"
        lines.append(
            f"{proto}       0      0  {address:<22} *.*                    LISTEN"
            "                 0            0  131072  131072   4242      0 00000 00000006"
        )
    lines.append(
        "tcp4       0      0  192.168.1.20.50123     17.57.146.20.443       ESTABLISHED"
        "     100          200  131072  131072    999      0 00102 00000008"
    )
    return header + "\n".join(lines) + "\n"


def ncprefs(*bundle_ids: str) -> str:
    apps = [{"bundle-id": b, "flags": 41951246, "auth": 7} for b in bundle_ids]
    return plistlib.dumps({"apps": apps, "dnd_prefs": b"\x00"}).decode()


class FakeMac:
    """Answers every program doctor runs; ``answers[key]`` is ``(code, stdout, stderr)`` or a
    callable taking argv. Calls are recorded, and an unknown program is collected in
    ``unexpected`` (answered with 127) instead of raising inside a check."""

    def __init__(self, settings, *, server_version: str = "18.6"):
        port, web = settings.pg_port, settings.web_port
        self.loaded: set[str] = set()
        self.disabled: set[str] = set()
        self.calls: list[list[str]] = []
        self.unexpected: list[list[str]] = []
        keychain = f"/Users/{USER}/Library/Keychains/login.keychain-db"
        self.answers: dict[str, object] = {
            "fdesetup isactive": (0, "true\n", ""),
            "fdesetup status": (0, "FileVault is On.\n", ""),
            "dscl": (0, f"_mbsetupuser 248\n{USER} 501\nroot 0\nnobody -2\n", ""),
            "defaults read": (0, "0\n", ""),
            "defaults export": (0, ncprefs("com.apple.iCal", doctor.SCRIPT_EDITOR), ""),
            "netstat": (0, netstat(f"127.0.0.1.{port}", f"::1.{port}"), ""),
            "lsof": (0, f"p4242\nf7\nn127.0.0.1:{port}\nf8\nn[::1]:{port}\n", ""),
            "ps": (0, f"{settings.pg_bin('postgres')} -D {settings.pg_data_dir}\n", ""),
            "pg_isready": (0, "", ""),
            "postgres --version": (0, f"postgres (PostgreSQL) {server_version} (Homebrew)\n", ""),
            "pg_dump --version": (0, f"pg_dump (PostgreSQL) {server_version} (Homebrew)\n", ""),
            "security": (0, "", f'Keychain "{keychain}" no-timeout\n'),
            "launchctl print": self._print,
            "launchctl print-disabled": self._print_disabled,
            "git rev-parse": (0, f"{COMMIT}\n", ""),
            "git grep": (1, "", ""),
            "git check-ignore": (0, "", ""),
            "tmutil isexcluded": lambda argv: (0, f"[Excluded]  {argv[-1]}\n", ""),
            "tmutil destinationinfo": (0, plistlib.dumps({}).decode(), ""),
            "diskutil": (0, plistlib.dumps({"FileVault": True, "Encryption": True}).decode(), ""),
        }
        self.web = web

    @staticmethod
    def key(argv: list[str]) -> str:
        program = Path(argv[0]).name
        if program == "git":
            return f"git {argv[3]}"  # git -C <repo> <verb>
        if program in ("fdesetup", "defaults", "launchctl", "tmutil"):
            return f"{program} {argv[1]}"
        if program in ("postgres", "pg_dump") and "--version" in argv:
            return f"{program} --version"
        return program

    def _print(self, argv):
        return (0 if argv[-1].rsplit(".", 1)[-1] in self.loaded else 113, "", "")

    def _print_disabled(self, argv):
        lines = [f'\t"{agents.label(n)}" => disabled' for n in sorted(self.disabled)]
        lines.append('\t"com.example.other" => disabled')
        lines += [f'\t"{agents.label(n)}" => enabled' for n in sorted(self.loaded)]
        return (0, "disabled services = {\n" + "\n".join(lines) + "\n}\n", "")

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        assert kwargs.get("check") is False and kwargs.get("capture_output") is True
        self.calls.append(argv)
        answer = self.answers.get(self.key(argv))
        if answer is None:
            self.unexpected.append(argv)
            return subprocess.CompletedProcess(argv, 127, "", "not faked")
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer(argv) if callable(answer) else answer
        return subprocess.CompletedProcess(argv, code, out, err)

    def ran(self, key: str) -> list[list[str]]:
        return [argv for argv in self.calls if self.key(argv) == key]


def _folders(settings) -> None:
    """A healthy cluster layout: private folders, our pg_hba.conf and budgetapp.conf."""
    for path in (settings.data_dir, settings.pg_data_dir, settings.pg_socket_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    for path in (settings.state_dir, settings.logs_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    (settings.pg_data_dir / "pg_hba.conf").write_text(pgcluster.render_hba())
    (settings.pg_data_dir / "budgetapp.conf").write_text(pgcluster.render_conf(settings))


class Doctor:
    """One doctor run's world: settings, layout, fake Mac and database."""

    def __init__(self, settings, tmp_path, session_factory, *, repo=None, server_version=None):
        self.settings = settings
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        if repo is None:
            repo = tmp_path / "repo"
            repo.mkdir(exist_ok=True)
        self.layout = agents.Layout.for_settings(
            settings, home=self.home, repo_dir=repo, uid=501, python="/fake/python3.14"
        )
        self.mac = FakeMac(settings, server_version=server_version or "18.6")
        self.session_factory = session_factory
        self.http = 200
        self.store = store_with(*ALL_ITEMS)
        _folders(settings)

    def context(self, **overrides) -> doctor.DoctorContext:
        values = {
            "layout": self.layout,
            "runner": self.mac,
            "session_factory": self.session_factory,
            "now": NOW,
            "user": USER,
            "http_status": lambda url: self.http,
            "store": self.store,
        }
        values.update(overrides)
        return doctor.DoctorContext(settings=self.settings, **values)

    def run(self, *checks, **overrides) -> list[doctor.Check]:
        ctx = self.context(**overrides)
        report = doctor.run_checks(ctx, checks or doctor.CHECKS)
        assert self.mac.unexpected == []
        return list(report.checks)

    def one(self, check, **overrides) -> doctor.Check:
        lines = [c for c in self.run(check, **overrides) if c.section != "Doctor"]
        assert len(lines) == 1, lines
        return lines[0]

    def statuses(self, check, **overrides) -> list[str]:
        return [c.status for c in self.run(check, **overrides)]


@pytest.fixture
def server_version(db) -> str:
    raw = db.execute(text("SHOW server_version")).scalar()
    return raw.split()[0]


@pytest.fixture
def dr(tmp_path, session_factory, server_version):
    settings = load_settings(
        "test",
        data_dir=tmp_path / "App Support" / "budgetapp",
        logs_dir=tmp_path / "Logs",
        pg_socket_dir=short_socket_dir(),
    )
    return Doctor(settings, tmp_path, session_factory, server_version=server_version)


@pytest.fixture
def prod(tmp_path, session_factory, server_version):
    """Production SETTINGS only: temporary paths, fake programs, the test database."""
    settings = load_settings(
        "production",
        data_dir=tmp_path / "App Support" / "budgetapp",
        logs_dir=tmp_path / "Logs" / "budgetapp",
        pg_socket_dir=short_socket_dir(),
    )
    return Doctor(settings, tmp_path, session_factory, server_version=server_version)


def install(d: Doctor, names=agents.AGENTS, mode=0o644) -> None:
    d.layout.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = d.layout.plist_path(name)
        path.write_bytes(agents.render_plist(agents.build_plist(name, d.layout)))
        os.chmod(path, mode)


def dump(settings, hours_old: float, *, mode=0o600, name=None) -> Path:
    directory = Path(settings.backups_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    when = NOW - dt.timedelta(hours=hours_old)
    path = directory / (name or f"{settings.db_name}-{when:%Y%m%dT%H%M%SZ}.dump")
    path.write_bytes(b"PGDMP fake")
    os.chmod(path, mode)
    return path


# =========================================================================== the whole run


class TestWholeRun:
    def test_a_healthy_sandbox_mac_passes(self, dr):
        dump(dr.settings, 3)
        report = doctor.run_checks(dr.context())
        assert dr.mac.unexpected == []
        failed = [c for c in report.checks if c.status == FAIL]
        assert failed == [] and not report.failed
        assert report.env == "test" and report.database == dr.settings.db_name
        assert report.count(PASS) >= 15
        text_report = doctor.format_report(report)
        assert text_report.splitlines()[0].startswith("Budget Tracker doctor · test")
        assert text_report.splitlines()[-1].startswith("All good: ")

    def test_only_read_only_programs_run(self, dr):
        doctor.run_checks(dr.context())
        launchctl_verbs = {argv[1] for argv in dr.mac.calls if Path(argv[0]).name == "launchctl"}
        assert launchctl_verbs <= {"print", "print-disabled"}
        git_verbs = {argv[3] for argv in dr.mac.calls if Path(argv[0]).name == "git"}
        assert git_verbs <= {"rev-parse", "grep", "check-ignore"}
        tmutil_verbs = {argv[1] for argv in dr.mac.calls if Path(argv[0]).name == "tmutil"}
        assert tmutil_verbs <= {"isexcluded", "destinationinfo"}

    def test_a_crashing_check_becomes_a_warning_and_the_rest_still_run(self, dr):
        def broken(ctx):
            raise KeyError("boom")
            yield  # pragma: no cover

        checks = dr.run(broken, doctor.check_filevault)
        assert [(c.section, c.status) for c in checks] == [("Doctor", WARN), ("This Mac", PASS)]
        assert checks[0].title == "The broken check failed: KeyError"

    def test_format_shows_fixes_for_problems_only(self):
        report = doctor.Report(
            "sandbox",
            "budgetapp_sandbox",
            (
                doctor.Check("This Mac", PASS, "FileVault is on", fix="never shown"),
                doctor.Check("This Mac", FAIL, "Broken", detail="why", fix="do this"),
                doctor.Check("Backups", INFO, "Note"),
            ),
        )
        out = doctor.format_report(report)
        assert "never shown" not in out
        assert "  FAIL  Broken\n        why\n        Fix: do this" in out
        assert "\nBackups\n  INFO  Note" in out
        assert out.splitlines()[-1] == "Problems found: 1 passed, 0 warning(s), 1 failed, 1 info"

    def test_unknown_status_is_refused(self):
        with pytest.raises(ValueError):
            doctor.Check("x", "OK", "title")


class TestSafety:
    def test_tests_must_pass_a_runner(self, dr):
        with pytest.raises(RuntimeError, match="runner"):
            doctor.DoctorContext.for_settings(dr.settings, layout=dr.layout)

    def test_production_never_builds_its_own_engine_in_tests(self, prod):
        ctx = prod.context(session_factory=None)
        with pytest.raises(RuntimeError, match="session factory"), ctx.session():
            pass  # pragma: no cover

    def test_a_program_that_cannot_run_or_times_out_is_a_result(self, dr):
        dr.mac.answers["fdesetup isactive"] = FileNotFoundError(2, "No such file")
        dr.mac.answers["fdesetup status"] = subprocess.TimeoutExpired("fdesetup", 20)
        ctx = dr.context()
        assert ctx.run(["/usr/bin/fdesetup", "isactive"]).returncode == 127
        assert ctx.run(["/usr/bin/fdesetup", "status"]).returncode == 124
        assert dr.one(doctor.check_filevault).status == FAIL


# =========================================================================== This Mac


class TestFileVault:
    def test_on(self, dr):
        assert dr.one(doctor.check_filevault).status == PASS

    def test_off(self, dr):
        dr.mac.answers["fdesetup isactive"] = (1, "false\n", "")
        dr.mac.answers["fdesetup status"] = (0, "FileVault is Off.\n", "")
        check = dr.one(doctor.check_filevault)
        assert (check.status, check.title) == (FAIL, "FileVault is off (SEC-6)")
        assert "Privacy & Security → FileVault" in check.fix

    def test_encryption_in_progress_is_a_warning(self, dr):
        dr.mac.answers["fdesetup status"] = (
            0,
            "FileVault is Off.\nEncryption in progress: Percent completed = 12.3\n",
            "",
        )
        check = dr.one(doctor.check_filevault)
        assert check.status == WARN and "Encryption in progress" in check.detail


class TestOtherUsers:
    def test_only_me(self, dr):
        assert dr.one(doctor.check_other_users).status == PASS

    def test_other_accounts_warn(self, dr):
        dr.mac.answers["dscl"] = (0, f"{USER} 501\nguest 201\nkid 502\nadmin2 503\n", "")
        check = dr.one(doctor.check_other_users)
        assert (check.status, check.title) == (WARN, "Other macOS accounts exist: admin2, kid")

    def test_dscl_failure(self, dr):
        dr.mac.answers["dscl"] = (1, "", "eDSRecordNotFound")
        assert dr.one(doctor.check_other_users).status == WARN


class TestPaths:
    def test_app_data_outside_protected_folders(self, dr):
        assert dr.one(doctor.check_paths).status == PASS

    def test_a_data_folder_in_desktop_fails(self, tmp_path, session_factory):
        settings = load_settings(
            "test", data_dir=tmp_path / "home" / "Desktop" / "data", logs_dir=tmp_path / "Logs"
        )
        d = Doctor(settings, tmp_path, session_factory)
        failed = [c for c in d.run(doctor.check_paths) if c.status == FAIL]
        assert {c.title for c in failed} >= {
            "The data folder is inside ~/Desktop",
            "The database cluster is inside ~/Desktop",
            "The backup folder is inside ~/Desktop",
            "The agent scripts folder is inside ~/Desktop",
        }
        assert all("Application Support" in c.fix for c in failed)

    def test_a_symlink_into_icloud_is_resolved(self, tmp_path, session_factory):
        icloud = tmp_path / "home" / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud.mkdir(parents=True)
        link = tmp_path / "linked-data"
        link.symlink_to(icloud)
        settings = load_settings("test", data_dir=link, logs_dir=tmp_path / "Logs")
        d = Doctor(settings, tmp_path, session_factory)
        titles = [c.title for c in d.run(doctor.check_paths) if c.status == FAIL]
        assert "The data folder is inside ~/Library/Mobile Documents" in titles

    @pytest.mark.parametrize(("value", "warned"), [("1", True), ("0", False), (None, False)])
    def test_the_repository_in_a_synced_desktop_only_warns(
        self, tmp_path, session_factory, value, warned
    ):
        repo = tmp_path / "home" / "Desktop" / "budgetapp"
        repo.mkdir(parents=True)
        settings = load_settings(
            "test", data_dir=tmp_path / "App Support" / "budgetapp", logs_dir=tmp_path / "Logs"
        )
        d = Doctor(settings, tmp_path, session_factory, repo=repo)
        d.mac.answers["defaults read"] = (0, f"{value}\n", "") if value else (1, "", "not exist")
        checks = d.run(doctor.check_paths)
        assert [c.status for c in checks] == ([PASS, WARN] if warned else [PASS])
        assert d.mac.ran("defaults read")[0][-1] == "FXICloudDriveDesktop"
        if warned:
            assert checks[1].title == "The code folder is in ~/Desktop, which iCloud syncs"


class TestPermissions:
    def test_private_folders(self, dr):
        assert dr.one(doctor.check_permissions).status == PASS

    def test_a_readable_data_folder_fails_with_the_chmod(self, dr):
        os.chmod(dr.settings.pg_data_dir, 0o750)
        check = dr.one(doctor.check_permissions)
        assert check.status == FAIL and "(mode 750)" in check.title
        assert check.fix == f'chmod 700 "{dr.settings.pg_data_dir}"'


# =========================================================================== Database


class TestPostgres:
    def test_running(self, dr):
        assert dr.one(doctor.check_postgres_running).status == PASS

    def test_not_running(self, dr):
        dr.mac.answers["pg_isready"] = (2, "", "")
        check = dr.one(doctor.check_postgres_running)
        assert (check.status, check.fix) == (FAIL, "./run db start")

    def test_not_running_under_the_agent_suggests_kickstart(self, prod):
        install(prod, ["postgres"])
        prod.mac.answers["pg_isready"] = (2, "", "")
        check = prod.one(doctor.check_postgres_running)
        assert check.fix.startswith("launchctl kickstart gui/501/local.budgetapp.postgres")

    def test_pg_isready_uses_the_private_socket(self, dr):
        dr.run(doctor.check_postgres_running)
        argv = dr.mac.ran("pg_isready")[0]
        assert argv[argv.index("-h") + 1] == str(dr.settings.pg_socket_dir)


class TestDatabaseConnection:
    def test_connected(self, dr):
        check = dr.one(doctor.check_database)
        assert check.status == PASS and dr.settings.db_name in check.title

    @pytest.mark.parametrize(
        ("error", "fix"),
        [
            (app_secrets.KeychainApprovalNeeded("denied"), "click Always Allow"),
            (app_secrets.MissingSecretError("missing"), "./run setup"),
            (
                RuntimeError('FATAL:  password authentication failed for user "budgetapp_dev"'),
                "./run setup",
            ),
            (RuntimeError('FATAL:  database "budgetapp_sandbox" does not exist'), "./run setup"),
            (RuntimeError("connection refused\nIs the server running?"), "./run db start"),
        ],
    )
    def test_each_problem_has_its_own_fix(self, dr, error, fix):
        def factory():
            raise error

        check = dr.one(doctor.check_database, session_factory=factory)
        assert check.status == FAIL and fix in check.fix
        assert "\n" not in (check.detail or "")

    def test_without_a_database_the_dependent_checks_are_skipped(self, dr):
        def factory():
            raise RuntimeError("connection refused")

        checks = dr.run(
            doctor.check_postgres_versions,
            doctor.check_migrations,
            doctor.check_connection,
            session_factory=factory,
        )
        assert [c.status for c in checks] == [INFO, INFO, INFO]


class TestPostgresSettings:
    def test_the_running_server_is_loopback_and_private(self, dr):
        checks = dr.run(doctor.check_postgres_settings)
        assert [c.status for c in checks] == [PASS, PASS]
        assert "loopback only (localhost)" in checks[0].title

    def unreachable(self):
        def factory():
            raise RuntimeError("connection refused")

        return factory

    def test_without_a_connection_the_config_file_is_read(self, dr):
        checks = dr.run(doctor.check_postgres_settings, session_factory=self.unreachable())
        assert [c.status for c in checks] == [PASS, PASS]

    def test_listening_everywhere_fails(self, dr):
        conf = dr.settings.pg_data_dir / "budgetapp.conf"
        conf.write_text(
            conf.read_text()
            .replace("'localhost'", "'*'")
            .replace("unix_socket_permissions = 0700", "unix_socket_permissions = 0777")
        )
        checks = dr.run(doctor.check_postgres_settings, session_factory=self.unreachable())
        assert [(c.status, c.title) for c in checks] == [
            (FAIL, "PostgreSQL listens on * (SEC-1)"),
            (FAIL, "unix_socket_permissions is 0777"),
        ]

    def test_an_unreadable_config_warns(self, dr):
        (dr.settings.pg_data_dir / "budgetapp.conf").unlink()
        checks = dr.run(doctor.check_postgres_settings, session_factory=self.unreachable())
        assert [c.status for c in checks] == [WARN, WARN]


class TestPgHba:
    def test_peer_map_and_scram_only(self, dr):
        assert dr.one(doctor.check_pg_hba).status == PASS

    @pytest.mark.parametrize(
        ("line", "title"),
        [
            ("local all all trust", "pg_hba.conf allows passwordless 'trust' logins"),
            ("host all all 0.0.0.0/0 scram-sha-256", "pg_hba.conf has rules other than"),
            ("host all all 127.0.0.1/32 md5", "pg_hba.conf has rules other than"),
        ],
    )
    def test_other_rules_fail(self, dr, line, title):
        hba = dr.settings.pg_data_dir / "pg_hba.conf"
        hba.write_text(hba.read_text() + line + "  # added by hand\n")
        check = dr.one(doctor.check_pg_hba)
        assert check.status == FAIL and check.title.startswith(title)
        assert "./run setup" in check.fix

    def test_a_missing_peer_rule_fails(self, dr):
        hba = dr.settings.pg_data_dir / "pg_hba.conf"
        kept = [line for line in hba.read_text().splitlines(True) if "peer" not in line]
        hba.write_text("".join(kept))
        assert dr.one(doctor.check_pg_hba).title == "pg_hba.conf has no peer rule for the socket"

    def test_unreadable(self, dr):
        (dr.settings.pg_data_dir / "pg_hba.conf").unlink()
        assert dr.one(doctor.check_pg_hba).status == WARN


class TestSocketDir:
    def test_private(self, dr):
        assert dr.one(doctor.check_socket_dir).status == PASS

    def test_open_mode(self, dr):
        os.chmod(dr.settings.pg_socket_dir, 0o755)
        check = dr.one(doctor.check_socket_dir)
        assert check.status == FAIL and check.title == "The socket folder is mode 755, not 700"

    def test_missing(self, dr):
        dr.settings.pg_socket_dir.rmdir()
        assert dr.one(doctor.check_socket_dir).status == FAIL

    def test_too_long_for_macos(self, tmp_path, session_factory):
        deep = tmp_path / ("x" * 60) / ("y" * 40)
        settings = load_settings("test", data_dir=deep, logs_dir=tmp_path / "Logs")
        d = Doctor(settings, tmp_path, session_factory)
        check = d.one(doctor.check_socket_dir)
        assert check.status == FAIL and "macOS allows 103" in check.title


class TestListeners:
    def test_loopback_only(self, dr):
        check = dr.one(doctor.check_postgres_listeners)
        assert check.status == PASS and "127.0.0.1.5434, ::1.5434" in check.title

    def test_all_interfaces_fail(self, dr):
        dr.mac.answers["netstat"] = (0, netstat("*.5434", "::1.5434"), "")
        check = dr.one(doctor.check_postgres_listeners)
        assert check.status == FAIL and "*.5434" in check.title

    def test_lsof_on_the_postmaster_pid_catches_a_lan_address(self, dr):
        (dr.settings.pg_data_dir / "postmaster.pid").write_text("4242\n/data\n")
        dr.mac.answers["lsof"] = (0, "p4242\nf7\nn192.168.1.20:5434\n", "")
        check = dr.one(doctor.check_postgres_listeners)
        assert check.status == FAIL and "192.168.1.20:5434" in check.title
        assert dr.mac.ran("lsof")[0][4] == "4242"

    def test_skipped_when_not_running(self, dr):
        dr.mac.answers["pg_isready"] = (2, "", "")
        assert dr.one(doctor.check_postgres_listeners).status == INFO

    def test_netstat_failure_warns(self, dr):
        dr.mac.answers["netstat"] = (1, "", "netstat: sysctl: Operation not permitted")
        assert dr.one(doctor.check_postgres_listeners).status == WARN


class TestVersions:
    def test_matching(self, dr):
        assert dr.statuses(doctor.check_postgres_versions) == [PASS]

    def test_a_minor_upgrade_needs_a_restart(self, tmp_path, session_factory, server_version):
        major = server_version.split(".")[0]
        settings = load_settings(
            "test", data_dir=tmp_path / "App Support" / "budgetapp", logs_dir=tmp_path / "Logs"
        )
        d = Doctor(settings, tmp_path, session_factory, server_version=f"{major}.99")
        d.mac.answers["pg_dump --version"] = (0, f"pg_dump (PostgreSQL) {server_version}\n", "")
        check = d.one(doctor.check_postgres_versions)
        assert check.status == WARN and "Restart the database" in check.fix

    def test_another_major_fails_and_an_old_pg_dump_too(self, dr):
        dr.mac.answers["postgres --version"] = (0, "postgres (PostgreSQL) 19.0\n", "")
        dr.mac.answers["pg_dump --version"] = (0, "pg_dump (PostgreSQL) 14.17 (Homebrew)\n", "")
        checks = dr.run(doctor.check_postgres_versions)
        assert [c.status for c in checks] == [FAIL, FAIL]
        assert checks[1].title == "pg_dump is older than the server, so backups would fail"

    def test_a_missing_pg_dump(self, dr):
        dr.mac.answers["pg_dump --version"] = FileNotFoundError(2, "No such file")
        checks = dr.run(doctor.check_postgres_versions)
        assert [(c.status, c.title) for c in checks][1] == (
            FAIL,
            "pg_dump can't be run, so backups would fail",
        )


class TestStalePid:
    def test_nothing_stale(self, dr):
        assert dr.one(doctor.check_stale_pid).status == PASS

    @pytest.mark.parametrize("command", ["/bin/sleep 300", ""])
    def test_a_stale_lock_file_keeps_postgres_down(self, dr, command):
        dr.mac.answers["pg_isready"] = (2, "", "")
        (dr.settings.pg_data_dir / "postmaster.pid").write_text("4242\n")
        dr.mac.answers["ps"] = (0 if command else 1, f"{command}\n" if command else "", "")
        check = dr.one(doctor.check_stale_pid)
        assert check.status == FAIL and "A stale postmaster.pid" in check.title
        assert ("is now sleep" if command else "is now not running") in check.title

    def test_our_own_postgres_is_not_stale(self, dr):
        dr.mac.answers["pg_isready"] = (2, "", "")  # still starting up
        (dr.settings.pg_data_dir / "postmaster.pid").write_text("4242\n")
        assert dr.one(doctor.check_stale_pid).status == PASS

    def test_the_log_line_is_reported(self, dr):
        log = dr.settings.logs_dir / "postgres.log"
        log.write_text(f"FATAL:  {doctor.STALE_LOCK_LINE}\nHINT:  Is another postmaster…\n")
        assert dr.one(doctor.check_stale_pid).status == INFO  # running now
        dr.mac.answers["pg_isready"] = (2, "", "")
        assert dr.one(doctor.check_stale_pid).status == WARN


class TestMigrations:
    def test_at_head(self, dr):
        check = dr.one(doctor.check_migrations)
        assert check.status == PASS and code_head() in check.title

    def test_behind_in_sandbox_and_production(self, dr, prod, db):
        db.execute(text("UPDATE alembic_version SET version_num = '0002_seed_defaults'"))
        db.flush()
        check = dr.one(doctor.check_migrations)
        assert check.status == FAIL and check.fix == "./run migrate"
        assert "at schema 0002_seed_defaults" in check.title
        assert prod.one(doctor.check_migrations).fix.startswith("./run live")


# =========================================================================== Web app


class TestWeb:
    def test_not_running_is_information(self, dr):
        assert dr.one(doctor.check_web).status == INFO

    def test_running_on_loopback_and_answering(self, dr):
        dr.mac.answers["netstat"] = (0, netstat("127.0.0.1.5434", "127.0.0.1.8766"), "")
        seen = []
        check = dr.one(doctor.check_web, http_status=lambda url: seen.append(url) or 200)
        assert check.status == PASS and seen == ["http://127.0.0.1:8766/"]

    def test_bound_beyond_loopback_fails(self, dr):
        dr.mac.answers["netstat"] = (0, netstat("*.8766"), "")
        check = dr.one(doctor.check_web)
        assert check.status == FAIL and "(SEC-1)" in check.title

    @pytest.mark.parametrize(("status", "expected"), [(None, WARN), (503, WARN), (404, PASS)])
    def test_answers(self, dr, status, expected):
        dr.mac.answers["netstat"] = (0, netstat("127.0.0.1.8766"), "")
        assert dr.one(doctor.check_web, http_status=lambda url: status).status == expected

    def test_a_loaded_web_agent_must_answer(self, prod):
        install(prod, ["web"])
        prod.mac.loaded.add("web")
        prod.mac.answers["netstat"] = (0, netstat("127.0.0.1.5434"), "")
        check = prod.one(doctor.check_web)
        assert check.status == FAIL and "http://127.0.0.1:8765" in check.title
        prod.mac.answers["netstat"] = (0, netstat("127.0.0.1.8765"), "")
        assert prod.one(doctor.check_web, http_status=lambda url: None).status == FAIL


# =========================================================================== Keychain


class TestKeychainItems:
    def test_every_item_of_both_environments_is_read_and_never_shown(self, dr):
        store = RecordingStore(store_with(*ALL_ITEMS))
        report = doctor.run_checks(dr.context(store=store), [doctor.check_keychain_items])
        assert set(store.reads) >= set(ALL_ITEMS) | {app_secrets.PRODUCTION_ACCESS_TOKEN}
        assert [c.status for c in report.checks] == [PASS]
        assert "value-of" not in doctor.format_report(report)

    def test_sandbox_severities(self, dr):
        checks = dr.run(doctor.check_keychain_items, store=store_with())
        by_title = {c.title: c.status for c in checks}
        assert by_title == {
            "The Plaid client ID isn't saved": WARN,
            "The Plaid Sandbox secret isn't saved": WARN,
            "The Plaid Production secret isn't saved": INFO,
            "The Sandbox database password is missing": FAIL,
            "The production database password is missing": WARN,
        }

    def test_approval_needed_points_to_always_allow(self, dr):
        denied = app_secrets.KeychainApprovalNeeded("-25293")
        store = store_with(*ALL_ITEMS, **{"plaid.secret.production": denied})
        checks = dr.run(doctor.check_keychain_items, store=store)
        problem = next(c for c in checks if c.status == FAIL)
        assert problem.title == "macOS didn't allow reading the Plaid Production secret"
        assert "Always Allow" in problem.fix
        assert checks[-1].title.startswith("4 Keychain item(s) readable")

    def test_production_linked_needs_its_token_and_keys(self, prod, db):
        make_item(db)
        checks = prod.run(
            doctor.check_keychain_items, store=store_with("db.password.budgetapp_live")
        )
        by_title = {c.title: c.status for c in checks}
        assert by_title["The production access token is missing, but Amex is linked"] == FAIL
        assert by_title["The Plaid client ID isn't saved"] == FAIL
        assert by_title["The Plaid Production secret isn't saved"] == FAIL
        assert by_title["The Plaid Sandbox secret isn't saved"] == INFO

    def test_a_saved_token_without_a_connection_suggests_reattaching(self, prod):
        store = store_with(*ALL_ITEMS, app_secrets.PRODUCTION_ACCESS_TOKEN)
        checks = prod.run(doctor.check_keychain_items, store=store)
        assert any("Re-attach" in (c.fix or "") and c.status == WARN for c in checks)

    def test_a_missing_sandbox_token_warns(self, dr):
        store = store_with(*ALL_ITEMS, app_secrets.SANDBOX_ITEM_INDEX)
        store.values[app_secrets.SANDBOX_ITEM_INDEX] = '["item-1"]'
        checks = dr.run(doctor.check_keychain_items, store=store)
        missing = [c for c in checks if c.status == WARN]
        assert [c.title for c in missing] == ["The Sandbox access token (item-1) is missing"]


class TestKeychainLock:
    def test_the_answer_on_stderr_is_read(self, dr):
        check = dr.one(doctor.check_keychain_lock)
        assert check.status == PASS
        argv = dr.mac.ran("security")[0]
        assert argv[1:] == [
            "show-keychain-info",
            str(dr.home / "Library/Keychains/login.keychain-db"),
        ]

    def test_a_self_locking_keychain_warns(self, dr):
        dr.mac.answers["security"] = (
            0,
            "",
            'Keychain "/x/login.keychain-db" lock-on-sleep timeout=300s\n',
        )
        check = dr.one(doctor.check_keychain_lock)
        assert check.status == WARN and "lock-on-sleep, timeout=" in check.detail

    def test_unreadable(self, dr):
        dr.mac.answers["security"] = (50, "", "security: SecKeychainCopySettings: not found\n")
        assert dr.one(doctor.check_keychain_lock).status == WARN


# =========================================================================== agents, install


class TestAgents:
    def test_outside_production_it_is_information(self, dr):
        check = dr.one(doctor.check_agents)
        assert check.status == INFO and "(none installed)" in check.title

    def test_production_without_agents_warns(self, prod):
        check = prod.one(doctor.check_agents)
        assert check.status == WARN and check.fix == "./run install-agents (a go-live step)"

    def test_all_loaded(self, prod):
        install(prod, ["postgres", "sync", "backup"])
        prod.mac.loaded |= {"postgres", "sync", "backup"}
        check = prod.one(doctor.check_agents)
        assert (check.status, check.title) == (PASS, "Agents loaded: postgres, sync, backup")
        verbs = {argv[1] for argv in prod.mac.ran("launchctl print")}
        assert verbs == {"print"}

    def test_problems(self, prod):
        install(prod, ["postgres", "backup", "web"])
        os.chmod(prod.layout.plist_path("web"), 0o666)
        prod.mac.loaded |= {"postgres", "web"}
        prod.mac.disabled.add("backup")
        titles = {c.title: c.status for c in prod.run(doctor.check_agents)}
        assert titles == {
            "The sync agent is missing": FAIL,
            "local.budgetapp.web.plist can be changed by others (mode 666)": FAIL,
            "The backup agent is disabled": FAIL,
        }

    def test_installed_but_not_loaded(self, prod):
        install(prod, ["postgres", "sync", "backup"])
        prod.mac.loaded |= {"postgres", "backup"}
        check = prod.one(doctor.check_agents)
        assert check.title == "The sync agent is installed but not loaded"
        assert "Login Items" in check.fix


class TestInstall:
    def stamp(self, d, commit):
        d.layout.venv_python.parent.mkdir(parents=True)
        d.layout.venv_python.write_text("")
        agents.write_stamp(d.layout, commit, "3.14.2")

    def test_matches_the_repository(self, prod):
        self.stamp(prod, COMMIT)
        check = prod.one(doctor.check_install)
        assert check.status == PASS and COMMIT[:12] in check.title

    def test_stale_install(self, prod):
        self.stamp(prod, OLD_COMMIT)
        check = prod.one(doctor.check_install)
        assert check.status == WARN and check.fix.startswith("./run install-agents")
        assert f"at {OLD_COMMIT[:12]}; the repository is at {COMMIT[:12]}" in check.title

    def test_not_installed(self, prod):
        assert prod.one(doctor.check_install).status == WARN
        install(prod, ["sync"])
        assert prod.one(doctor.check_install).status == FAIL

    def test_outside_production(self, dr):
        assert dr.run(doctor.check_install) == []
        self.stamp(dr, COMMIT)
        assert dr.one(doctor.check_install).status == INFO


class TestNotifications:
    def test_script_editor_registered(self, dr):
        assert dr.one(doctor.check_notifications).status == PASS

    @pytest.mark.parametrize(
        "answer", [(0, ncprefs("com.apple.iCal"), ""), (1, "", "Domain not found"), (0, "{", "")]
    )
    def test_not_set_up_or_unreadable(self, dr, answer):
        dr.mac.answers["defaults export"] = answer
        check = dr.one(doctor.check_notifications)
        assert check.status == WARN and "./run notify-test" in check.fix


# =========================================================================== backups


class TestBackups:
    def test_recent(self, dr):
        dump(dr.settings, 30)
        dump(dr.settings, 5)
        check = dr.one(doctor.check_backups)
        assert check.status == PASS and check.title.startswith("Newest verified backup 5 h ago")
        assert "(2 kept in" in check.title

    def test_none_is_information_in_sandbox_and_a_failure_in_production(self, dr, prod):
        assert dr.one(doctor.check_backups).status == INFO
        check = prod.one(doctor.check_backups)
        assert check.status == FAIL and check.fix.startswith("./run backup --env production")

    def test_stale_production_backup(self, prod):
        dump(prod.settings, 27)
        check = prod.one(doctor.check_backups)
        assert check.status == FAIL and "is 27 h old" in check.title

    def test_partial_and_foreign_files_do_not_count(self, prod):
        directory = Path(prod.settings.backups_dir)
        dump(prod.settings, 1, name=".budgetapp-20261029T140000Z.dump.partial")
        dump(prod.settings, 1, name="budgetapp_sandbox-20261029T140000Z.dump")
        assert directory.is_dir()
        assert prod.one(doctor.check_backups).status == FAIL

    def test_permissions(self, prod):
        dump(prod.settings, 2, mode=0o644)
        os.chmod(prod.settings.backups_dir, 0o755)
        titles = [(c.status, c.title) for c in prod.run(doctor.check_backups)]
        assert titles[:2] == [
            (FAIL, "The backup folder is mode 755, not 700"),
            (FAIL, "1 dump(s) are readable by others"),
        ]


def destinations(*mounts: str | None) -> str:
    entries = []
    for number, mount in enumerate(mounts):
        entry = {"ID": f"ID-{number}", "Kind": "Local", "Name": f"Disk {number}"}
        if mount:
            entry["MountPoint"] = mount
        entries.append(entry)
    return plistlib.dumps({"Destinations": entries}).decode()


class TestTimeMachine:
    def test_excluded_data_and_no_destinations(self, dr):
        checks = dr.run(doctor.check_time_machine)
        assert [(c.status, c.title) for c in checks] == [
            (PASS, "Time Machine skips the live database folder"),
            (INFO, "Time Machine has no destinations: the dumps stay on this Mac"),
        ]

    def test_an_included_data_folder_fails(self, dr):
        dr.mac.answers["tmutil isexcluded"] = lambda argv: (0, f"[Included]  {argv[-1]}\n", "")
        checks = dr.run(doctor.check_time_machine)
        assert checks[0].status == FAIL and checks[0].fix.startswith("tmutil addexclusion")

    def included_dumps(self, d, *mounts):
        data = str(d.settings.pg_data_dir)
        d.mac.answers["tmutil isexcluded"] = lambda argv: (
            0,
            f"[{'Excluded' if argv[-1] == data else 'Included'}]  {argv[-1]}\n",
            "",
        )
        d.mac.answers["tmutil destinationinfo"] = (0, destinations(*mounts), "")

    def test_encrypted_destinations_pass(self, dr):
        self.included_dumps(dr, "/Volumes/Backup")
        assert dr.statuses(doctor.check_time_machine) == [PASS, PASS]
        assert dr.mac.ran("diskutil")[0][-1] == "/Volumes/Backup"

    def test_an_unencrypted_destination_fails(self, dr):
        self.included_dumps(dr, "/Volumes/Backup", "/Volumes/Old")
        dr.mac.answers["diskutil"] = lambda argv: (
            0,
            plistlib.dumps(
                {"FileVault": argv[-1] == "/Volumes/Backup", "Encryption": True}
            ).decode(),
            "",
        )
        check = dr.run(doctor.check_time_machine)[1]
        assert check.status == FAIL and check.title.endswith("Disk 1 (SEC-6)")
        assert "Encrypt backups" in check.fix and "tmutil addexclusion" in check.fix

    def test_an_unmounted_destination_is_unknown(self, dr):
        self.included_dumps(dr, None)
        check = dr.run(doctor.check_time_machine)[1]
        assert check.status == WARN and "Disk 0" in check.title

    def test_excluded_dumps_are_information(self, dr):
        dr.mac.answers["tmutil destinationinfo"] = (0, destinations("/Volumes/Backup"), "")
        assert dr.statuses(doctor.check_time_machine) == [PASS, INFO]

    def test_unreadable_destinations(self, dr):
        dr.mac.answers["tmutil destinationinfo"] = (1, "", "tmutil: requires Full Disk Access")
        assert dr.statuses(doctor.check_time_machine) == [PASS, WARN]


# =========================================================================== code


def token(kind: str = "access", env: str = "production") -> str:
    """A token-shaped string built at run time (no literal token in this file for doctor to
    find in the repository)."""
    return "-".join((kind, env, str(uuid.uuid4())))


class TestRepository:
    def test_clean(self, dr):
        checks = dr.run(doctor.check_repository)
        assert [(c.status, c.title) for c in checks] == [
            (PASS, "No Plaid tokens in files tracked by git"),
            (PASS, ".gitignore keeps CSV downloads out of git"),
        ]
        grep = dr.mac.ran("git grep")[0]
        assert grep[-1] == doctor.TOKEN_PATTERN

    def test_a_committed_token_fails_without_showing_it(self, dr):
        real = token()
        placeholder = "access-sandbox-" + "-".join(("1" * 8, "2" * 4, "3" * 4, "4" * 4, "5" * 12))
        dr.mac.answers["git grep"] = (
            0,
            f"notes.txt:3:{real}\ntests/test_x.py:9:{placeholder}\n",
            "",
        )
        report = doctor.run_checks(dr.context(), [doctor.check_repository])
        check = report.checks[0]
        assert check.status == FAIL and check.title.startswith("A Plaid token is committed in")
        assert "notes.txt:3" in check.title and "test_x.py" not in check.title
        assert real not in doctor.format_report(report)

    def test_the_pattern_matches_plaid_tokens(self):
        assert re.fullmatch(doctor.TOKEN_PATTERN, token("public", "sandbox"))
        assert not re.fullmatch(doctor.TOKEN_PATTERN, "access-sandbox-not-a-token")

    def test_csv_downloads_not_ignored(self, dr):
        dr.mac.answers["git check-ignore"] = (1, "", "")
        check = dr.run(doctor.check_repository)[1]
        assert (check.status, check.fix) == (FAIL, "Add *.csv to .gitignore.")

    def test_git_failures_warn(self, dr):
        dr.mac.answers["git grep"] = (128, "", "fatal: not a git repository")
        dr.mac.answers["git check-ignore"] = (128, "", "fatal: not a git repository")
        assert dr.statuses(doctor.check_repository) == [WARN, WARN]

    def test_running_from_the_install(self, dr):
        layout = dr.layout.replace(repo_dir=None)
        assert dr.one(doctor.check_repository, layout=layout).status == INFO


# =========================================================================== Plaid connection


class TestConnection:
    def test_nothing_linked(self, dr):
        assert dr.one(doctor.check_connection).status == INFO

    def test_healthy(self, dr, db, freeze_today):
        freeze_today(now=NOW)
        make_item(db, liabilities_status="ok")
        checks = dr.run(doctor.check_connection)
        assert [c.status for c in checks] == [PASS, PASS]
        assert checks[0].title.startswith("American Express consent is valid until")

    def test_consent_due_soon_and_expired(self, dr, db, freeze_today):
        freeze_today(now=NOW)
        make_item(db, last_consented_at=NOW - dt.timedelta(days=350), liabilities_status="ok")
        check = dr.run(doctor.check_connection)[0]
        assert check.status == WARN and "(15 days left)" in check.title
        make_item(db, last_consented_at=NOW - dt.timedelta(days=400), liabilities_status="ok")
        expired = [c for c in dr.run(doctor.check_connection) if c.status == FAIL]
        assert expired[0].title.startswith("American Express consent expired on")

    def test_reconnect_errors_liabilities_and_staleness(self, dr, db, freeze_today):
        freeze_today(now=NOW)
        make_item(
            db,
            status="needs_reconnect",
            error_code="ITEM_LOGIN_REQUIRED",
            liabilities_status="not_consented",
            last_sync_success_at=NOW - dt.timedelta(hours=50),
        )
        checks = {c.title: c.status for c in dr.run(doctor.check_connection)}
        assert checks["American Express needs you to reconnect"] == FAIL
        assert checks["Liabilities wasn't consented (no statement balance or due date)"] == WARN
        assert checks["No successful sync for 2 days"] == WARN


# =========================================================================== the command


class TestCommand:
    def run(self, monkeypatch, capsys, d, **overrides):
        ctx = d.context(**overrides)
        monkeypatch.setattr(
            doctor.DoctorContext, "for_settings", classmethod(lambda cls, settings: ctx)
        )
        code = doctor_command.run(argparse.Namespace(), d.settings)
        return code, capsys.readouterr().out

    def test_exit_status_follows_failures(self, monkeypatch, capsys, dr):
        dump(dr.settings, 1)
        code, out = self.run(monkeypatch, capsys, dr)
        assert code == 0 and "All good: " in out
        dr.mac.answers["fdesetup isactive"] = (1, "false\n", "")
        dr.mac.answers["fdesetup status"] = (0, "FileVault is Off.\n", "")
        code, out = self.run(monkeypatch, capsys, dr)
        assert code == 1 and "  FAIL  FileVault is off (SEC-6)" in out
        assert "Problems found: " in out

    def test_the_cli_registers_doctor(self):
        from budgetapp.cli import build_parser

        args = build_parser().parse_args(["doctor", "--env", "sandbox"])
        assert args.handler is doctor_command.run
