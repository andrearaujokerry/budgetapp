"""Backups (REL-2): real ``pg_dump`` runs of the throwaway test database into a temporary folder,
plus fake-runner tests for what must never be observed for real (production connections)."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.commands import backup as backup_command
from budgetapp.config import load_settings
from budgetapp.services import agents, backup
from budgetapp.services.notify import RecordingNotifier

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 29, 2, 30, tzinfo=UTC)


@pytest.fixture
def bk(settings, test_database, tmp_path):
    """This process's (migrated) test database, with backups and state in a temporary folder."""
    return load_settings(
        "test", backups_dir=tmp_path / "backups" / "test", state_dir=tmp_path / "state"
    )


def local(*args: int) -> dt.datetime:
    """An aware datetime at this Mac's local wall-clock time (the agents' schedule is local)."""
    return dt.datetime(*args).astimezone()


def stamp(when: dt.datetime) -> str:
    return when.astimezone(UTC).strftime(backup.TIMESTAMP_FORMAT)


def fake_dump(settings, when: dt.datetime, *, pre=False, kind="", mode=0o600, db=None) -> Path:
    """A file with a final dump name (for listing/rotation tests; not a real dump)."""
    directory = backup.ensure_backup_dir(settings)
    kind = backup.PRE_RESTORE if pre else kind
    suffix = f"-{kind}" if kind else ""
    path = directory / f"{db or settings.db_name}-{stamp(when)}{suffix}.dump"
    path.write_bytes(b"PGDMP")
    os.chmod(path, mode)
    return path


def listing(dbname: str) -> str:
    return (
        ";\n; Archive created at 2026-10-29 02:30:00 UTC\n"
        f";     dbname: {dbname}\n;     TOC Entries: 12\n;     Format: CUSTOM\n"
        ";     Dumped from database version: 18.6 (Homebrew)\n"
        ";     Dumped by pg_dump version: 18.6 (Homebrew)\n;\n;\n; Selected TOC Entries:\n;\n"
        "227; 1259 197817 TABLE public accounts budgetapp_live\n"
    )


class FakePg:
    """Records calls; pg_dump writes a small file, pg_restore answers like a good dump."""

    def __init__(self, dbname: str, *, fail: str | None = None) -> None:
        self.dbname = dbname
        self.fail = fail
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        name = Path(argv[0]).name
        if name == "pg_dump":
            target = next(a for a in argv if a.startswith("--file=")).split("=", 1)[1]
            Path(target).write_bytes(b"PGDMP fake dump")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if name == "pg_restore" and "--list" in argv:
            return subprocess.CompletedProcess(argv, 0, listing(self.dbname), "")
        if name == "pg_restore" and self.fail == "full-read":
            return subprocess.CompletedProcess(
                argv, 1, "", "pg_restore: error: could not read from input file: end of file\n"
            )
        return subprocess.CompletedProcess(argv, 0, "", "")


# --------------------------------------------------------------------------- real dumps


class TestRealBackup:
    def test_a_verified_private_dump_named_for_the_database(self, bk):
        result = backup.run_backup(bk)
        assert result.created and result.path is not None
        parsed = backup.parse_backup_name(result.path)
        assert parsed is not None and parsed[0] == bk.db_name and parsed[3] == ""
        assert result.path.stat().st_mode & 0o777 == 0o600
        assert result.path.parent.stat().st_mode & 0o777 == 0o700
        assert result.info.database == bk.db_name
        assert result.info.format == "CUSTOM" and result.info.server_version.startswith("18")
        assert [b.path for b in backup.list_backups(bk)] == [result.path]
        assert [p.name for p in result.path.parent.iterdir()] == [result.path.name]  # no temp
        toc = subprocess.run(
            [bk.pg_bin("pg_restore"), "--list", str(result.path)], capture_output=True, text=True
        )
        assert "TABLE public transactions" in toc.stdout
        assert backup.dump_revision(bk, result.path) == backup.db.code_head()

    def test_a_loose_folder_is_made_private(self, bk):
        bk.backups_dir.mkdir(parents=True, mode=0o755)
        os.chmod(bk.backups_dir, 0o755)
        backup.run_backup(bk)
        assert bk.backups_dir.stat().st_mode & 0o777 == 0o700

    def test_rotation_keeps_the_newest(self, bk):
        made = []
        for hour in range(5):
            made.append(backup.run_backup(bk, keep=3, now=NOW + dt.timedelta(hours=hour)).path)
        kept = [b.path for b in backup.list_backups(bk)]
        assert kept == list(reversed(made[2:]))
        assert not made[0].exists() and not made[1].exists()

    def test_two_backups_in_the_same_second_both_survive(self, bk):
        first = backup.run_backup(bk, now=NOW).path
        second = backup.run_backup(bk, now=NOW).path
        assert first != second and first.exists() and second.exists()
        assert [b.path for b in backup.list_backups(bk)] == [second, first]

    @pytest.mark.parametrize(
        ("cut", "message"),
        [
            (1000, "incomplete or damaged"),  # pg_restore --list still passes (RESEARCH T5.5)
            (40000, "not a readable dump"),
        ],
    )
    def test_a_truncated_dump_fails_verification_and_leaves_nothing(self, bk, cut, message):
        def truncating(argv, **kwargs):
            result = subprocess.run(argv, **kwargs)
            if Path(argv[0]).name == "pg_dump":
                target = Path(next(a for a in argv if a.startswith("--file=")).split("=", 1)[1])
                data = target.read_bytes()
                target.write_bytes(data[: len(data) - cut])
            return result

        with pytest.raises(backup.BackupError, match=message):
            backup.run_backup(bk, runner=truncating)
        assert list(bk.backups_dir.iterdir()) == []

    def test_a_failed_pg_dump_leaves_nothing(self, bk, tmp_path):
        missing = load_settings(
            "test", db_name="budgetapp_test_no_such_db", backups_dir=bk.backups_dir
        )
        with pytest.raises(backup.BackupError, match="pg_dump failed"):
            backup.run_backup(missing)
        assert list(bk.backups_dir.iterdir()) == []

    def test_pre_upgrade_copies_never_rotate_out_nightly_backups(self, bk):
        """Final review ops-1: each upgrade (and each retry of a failing one) took an ordinary
        backup, so the 30 kept were "the 30 newest dumps of any kind" and every upgrade attempt
        deleted one nightly restore point."""
        nightly = [fake_dump(bk, NOW - dt.timedelta(days=d)) for d in range(30)]
        made = []
        for minute in (0, 5, 10):  # three ./run live attempts, two of them failing later on
            with clock.frozen(now=NOW + dt.timedelta(hours=8, minutes=minute)):
                made.append(agents._default_backup(bk))
        assert [b.path for b in backup.list_backups(bk, kind="")] == nightly
        copies = backup.list_backups(bk, kind=backup.PRE_UPGRADE)
        assert [b.path for b in copies] == made[::-1]
        assert all(p.name.endswith("-pre-upgrade.dump") for p in made)
        assert backup.newest_backup(bk).path == nightly[0]

    def test_pre_upgrade_copies_keep_their_newest_5(self, bk):
        old = [fake_dump(bk, NOW - dt.timedelta(days=d), kind=backup.PRE_UPGRADE) for d in range(6)]
        nightly = fake_dump(bk, NOW - dt.timedelta(days=40))
        result = backup.run_backup(bk, kind=backup.PRE_UPGRADE, now=NOW + dt.timedelta(hours=1))
        kept = [b.path for b in backup.list_backups(bk, kind=backup.PRE_UPGRADE)]
        assert kept == [result.path, *old[:4]] and result.kept == backup.PRE_UPGRADE_KEEP
        assert set(result.removed) == set(old[4:]) and nightly.exists()

    def test_if_due_follows_the_nightly_schedule(self, bk):
        first = backup.run_backup(bk, now=local(2026, 11, 10, 2, 30))
        skipped = backup.run_backup(bk, if_due=True, now=local(2026, 11, 10, 21, 0))  # a login
        assert skipped.status == "skipped" and skipped.newest.path == first.path
        due = backup.run_backup(bk, if_due=True, now=local(2026, 11, 11, 2, 30))
        assert due.created and due.path != first.path
        assert not backup.is_due(bk, now=local(2026, 11, 12, 2, 29))
        assert backup.is_due(bk, now=local(2026, 11, 12, 2, 30))
        # --min-age-hours still overrides the schedule
        assert backup.is_due(bk, min_age=dt.timedelta(hours=1), now=local(2026, 11, 11, 4, 0))
        assert not backup.is_due(bk, min_age=dt.timedelta(hours=3), now=local(2026, 11, 11, 4, 0))


# --------------------------------------------------------------------------- unit


class TestListing:
    def test_only_final_dumps_of_this_database_count(self, bk):
        good = fake_dump(bk, NOW)
        pre = fake_dump(bk, NOW + dt.timedelta(hours=1), pre=True)
        upgrade = fake_dump(bk, NOW + dt.timedelta(hours=3), kind=backup.PRE_UPGRADE)
        fake_dump(bk, NOW + dt.timedelta(hours=2), db="budgetapp_sandbox")
        (bk.backups_dir / f".{bk.db_name}-{stamp(NOW)}.partial").write_bytes(b"x")
        (bk.backups_dir / "notes.txt").write_text("hi")
        (bk.backups_dir / f"{bk.db_name}-garbage.dump").write_bytes(b"x")
        found = backup.list_backups(bk)
        assert [b.path for b in found] == [upgrade, pre, good]
        assert [b.kind for b in found] == [backup.PRE_UPGRADE, backup.PRE_RESTORE, ""]
        assert found[1].pre_restore and not found[0].pre_restore and not found[2].pre_restore
        assert found[2].created_at == NOW
        assert backup.parse_backup_name(upgrade)[3] == backup.PRE_UPGRADE

    def test_a_daytime_backup_does_not_skip_the_next_nightly_run(self, bk):
        """Final review ops-2: a backup at 09:00 (a login after the Mac was off, or a manual one)
        was under 20 h old at 02:30, so that night was skipped and the next backup came ~42 h
        after the last, with doctor failing "26 h old" in between."""
        fake_dump(bk, local(2026, 11, 10, 9, 0))
        assert not backup.is_due(bk, now=local(2026, 11, 10, 14, 0))  # a later login that day
        assert backup.is_due(bk, now=local(2026, 11, 11, 2, 30))
        assert backup.due_after(local(2026, 11, 11, 2, 30)) == local(2026, 11, 11, 2, 30)
        assert backup.due_after(local(2026, 11, 11, 1, 0)) == local(2026, 11, 10, 2, 30)

    def test_a_backup_since_the_last_nightly_time_makes_a_login_run_skip(self, bk):
        fake_dump(bk, local(2026, 11, 10, 8, 0))  # the Mac was off at 02:30: caught up at login
        assert not backup.is_due(bk, now=local(2026, 11, 10, 10, 0))
        fake_dump(bk, local(2026, 11, 11, 3, 0), kind=backup.PRE_UPGRADE)  # copies don't count
        assert backup.is_due(bk, now=local(2026, 11, 11, 9, 0))

    def test_an_absent_folder_has_no_backups(self, bk):
        assert backup.list_backups(bk) == [] and backup.newest_backup(bk) is None
        assert backup.is_due(bk)

    def test_rotation_never_counts_or_touches_pre_restore_copies(self, bk):
        backups = [fake_dump(bk, NOW + dt.timedelta(hours=h)) for h in range(4)]
        copies = [fake_dump(bk, NOW + dt.timedelta(hours=h, minutes=30), pre=True) for h in (0, 3)]
        removed = backup.rotate(bk, keep=2)
        assert removed == [backups[1], backups[0]]  # 2 backups stay; the copies don't count
        assert all(path.exists() for path in [*backups[2:], *copies])
        assert [b.path for b in backup.list_backups(bk, kind="")] == backups[:1:-1]
        assert [b.path for b in backup.list_backups(bk, kind=backup.PRE_RESTORE)] == copies[::-1]
        with pytest.raises(ValueError):
            backup.rotate(bk, keep=0)

    def test_pre_restore_copies_keep_their_newest_and_never_a_protected_one(self, bk):
        copies = [fake_dump(bk, NOW + dt.timedelta(hours=h), pre=True) for h in range(4)]
        nightly = fake_dump(bk, NOW - dt.timedelta(days=1))
        removed = backup.prune_pre_restore(bk, keep=2, protect=(copies[0],))
        assert removed == [copies[1]]  # copies[0] is being restored (an undo): never deleted
        assert copies[0].exists() and nightly.exists()
        assert backup.prune_pre_restore(bk, keep=2) == [copies[0]]

    def test_a_copy_taken_before_a_restore_is_not_a_backup(self, bk):
        """Due/stale/newest describe the nightly backups; a restore's copy says nothing about
        them (and holds the data the restore replaced)."""
        nightly = fake_dump(bk, NOW - dt.timedelta(hours=30))
        fake_dump(bk, NOW - dt.timedelta(hours=1), pre=True)
        fake_dump(bk, NOW - dt.timedelta(hours=2), kind=backup.PRE_UPGRADE)
        assert backup.newest_backup(bk).path == nightly
        assert backup.is_due(bk, now=NOW)
        assert backup.backup_health(bk, now=NOW) == (True, backup.newest_backup(bk))

    def test_stale_temporary_files_are_cleaned_up(self, bk):
        directory = backup.ensure_backup_dir(bk)
        old = directory / ".old.partial"
        new = directory / ".new.partial"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        past = (NOW - dt.timedelta(hours=7)).timestamp()
        os.utime(old, (past, past))
        os.utime(new, (NOW.timestamp(), NOW.timestamp()))
        assert backup._remove_stale_partials(directory, NOW) == [old]
        assert new.exists()

    def test_fix_permissions(self, bk):
        path = fake_dump(bk, NOW, mode=0o644)
        copy = fake_dump(bk, NOW, kind=backup.PRE_UPGRADE, mode=0o644)
        os.chmod(bk.backups_dir, 0o755)
        changed = backup.fix_permissions(bk)
        assert set(changed) == {bk.backups_dir, path, copy}
        assert path.stat().st_mode & 0o777 == 0o600 and copy.stat().st_mode & 0o777 == 0o600

    def test_the_dump_header(self):
        info = backup.parse_dump_header(listing("budgetapp"))
        assert info.database == "budgetapp" and info.format == "CUSTOM"
        assert info.toc_entries == 12 and info.created == "2026-10-29 02:30:00 UTC"
        assert info.server_version == "18.6 (Homebrew)" and info.dumped_by == "18.6 (Homebrew)"
        assert backup.parse_dump_header("").database is None


class TestProductionConnection:
    """Production is exercised with a fake runner only: nothing connects to real data."""

    @pytest.fixture
    def prod(self, tmp_path, secrets):
        secrets.values["db.password.budgetapp_live"] = "live-password-123456"
        return load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")

    def test_the_role_password_travels_only_in_the_environment(self, prod):
        fake = FakePg("budgetapp")
        result = backup.run_backup(prod, runner=fake, now=NOW)
        argv, kwargs = fake.calls[0]
        assert Path(argv[0]).name == "pg_dump" and "--format=custom" in argv
        joined = " ".join(" ".join(a) for a, _ in fake.calls)
        assert "live-password-123456" not in joined
        assert argv[-8:] == [
            "-h",
            "127.0.0.1",
            "-p",
            "5434",
            "-U",
            "budgetapp_live",
            "-d",
            "budgetapp",
        ]
        env = kwargs["env"]
        assert env["PGPASSWORD"] == "live-password-123456"
        assert env["PGREQUIREAUTH"] == "scram-sha-256" and env["PGSSLMODE"] == "disable"
        assert "PGHOST" not in env and "PGSERVICE" not in env
        assert all(kw["umask"] == 0o077 for _, kw in fake.calls)
        assert all(
            "PGPASSWORD" not in kw["env"] for a, kw in fake.calls[1:]
        )  # pg_restore reads files
        assert result.path.parent == prod.backups_dir
        assert result.path.name == f"budgetapp-{stamp(NOW)}.dump"
        assert [Path(a[0]).name for a, _ in fake.calls] == ["pg_dump", "pg_restore", "pg_restore"]
        assert "--file=/dev/null" in fake.calls[2][0]

    def test_a_dump_of_another_database_is_not_kept(self, prod):
        with pytest.raises(backup.BackupError, match="names database 'budgetapp_sandbox'"):
            backup.run_backup(prod, runner=FakePg("budgetapp_sandbox"), now=NOW)
        assert list(prod.backups_dir.iterdir()) == []

    def test_a_failed_full_read_keeps_nothing(self, prod):
        with pytest.raises(backup.BackupError, match="end of file"):
            backup.run_backup(prod, runner=FakePg("budgetapp", fail="full-read"), now=NOW)
        assert list(prod.backups_dir.iterdir()) == []

    def test_a_missing_password_says_how_to_fix_it(self, prod, secrets):
        secrets.values.clear()
        with pytest.raises(app_secrets.MissingSecretError, match="./run setup"):
            backup.run_backup(prod, runner=FakePg("budgetapp"), now=NOW)

    def test_programs_that_cannot_run_are_errors(self, prod):
        def broken(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        with pytest.raises(backup.BackupError, match="Could not run"):
            backup.run_backup(prod, runner=broken, now=NOW)


class TestAlerts:
    def test_problems_notify_once_a_day(self, bk):
        notifier = RecordingNotifier()
        day = dt.date(2026, 10, 29)
        assert backup.notify_backup_problem(bk, notifier, "failed", today=day) is True
        assert backup.notify_backup_problem(bk, notifier, "failed again", today=day) is False
        assert backup.notify_backup_problem(bk, notifier, "next day", today=day.replace(day=30))
        assert [n.message for n in notifier.sent] == ["failed", "next day"]

    def test_the_sync_agent_announces_stale_backups_once_a_day(self, bk):
        """Final review ops-2: only the backup agent ever checked freshness, so backups that
        stopped because that agent no longer runs were never announced."""
        notifier = RecordingNotifier()
        fake_dump(bk, NOW - dt.timedelta(hours=30))
        assert backup.alert_if_backups_stopped(bk, notifier, now=NOW) is True
        assert backup.alert_if_backups_stopped(bk, notifier, now=NOW) is False  # once a day
        assert len(notifier.sent) == 1 and "30 hours old" in notifier.sent[0].message

    def test_the_sync_agent_leaves_a_running_backup_agent_to_report(self, bk):
        """The backup agent reports its own failures; right after a wake both agents start
        together, and the backup it is making must not be announced as missing."""
        notifier = RecordingNotifier()
        fake_dump(bk, NOW - dt.timedelta(hours=50))
        backup.note_agent_run(bk, now=NOW - dt.timedelta(minutes=1))
        assert backup.alert_if_backups_stopped(bk, notifier, now=NOW) is False
        backup.note_agent_run(bk, now=NOW - dt.timedelta(hours=27))
        assert backup.alert_if_backups_stopped(bk, notifier, now=NOW) is True
        fresh = load_settings(
            "test", backups_dir=bk.backups_dir.parent / "fresh", state_dir=bk.state_dir / "x"
        )
        fake_dump(fresh, NOW - dt.timedelta(hours=2))
        assert backup.alert_if_backups_stopped(fresh, notifier, now=NOW) is False
        assert len(notifier.sent) == 1

    def test_stale_means_none_or_over_26_hours(self, bk):
        assert backup.backup_health(bk, now=NOW) == (True, None)
        path = fake_dump(bk, NOW - dt.timedelta(hours=25))
        stale, newest = backup.backup_health(bk, now=NOW)
        assert not stale and newest.path == path
        assert backup.backup_health(bk, now=NOW + dt.timedelta(hours=2))[0]
        assert "27 hours old" in backup.stale_message(bk, newest, NOW + dt.timedelta(hours=2))
        assert "no verified test backup" in backup.stale_message(bk, None)


# --------------------------------------------------------------------------- command


def args(**overrides):
    values = {"if_due": False, "min_age_hours": None, "keep": 30, "unattended": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class TestBackupCommand:
    @pytest.fixture
    def notifier(self, monkeypatch):
        recording = RecordingNotifier()
        monkeypatch.setattr(backup_command, "get_notifier", lambda settings: recording)
        monkeypatch.setattr(backup_command.secrets, "enable_unattended_mode", lambda: True)
        return recording

    def test_backup_now_prints_what_happened(self, bk, capsys):
        assert backup_command.run_backup(args(keep=5), bk) == 0
        out = capsys.readouterr().out
        assert f"Backed up {bk.db_name} to {bk.backups_dir}" in out
        assert "verified with a full read" in out and "1 backup(s) kept (newest 5)" in out

    def test_if_due_skips_quietly(self, bk, capsys):
        backup.run_backup(bk)
        assert backup_command.run_backup(args(if_due=True), bk) == 0
        assert "Skipped: the newest backup" in capsys.readouterr().out

    def test_unattended_failure_notifies_once_a_day(self, bk, notifier, monkeypatch, capsys):
        def fail(*a, **k):
            raise backup.BackupError("pg_dump failed: boom")

        monkeypatch.setattr(backup_command.backups, "run_backup", fail)
        assert backup_command.run_backup(args(unattended=True), bk) == 1
        assert backup_command.run_backup(args(unattended=True), bk) == 1
        assert (
            len(notifier.sent) == 1
            and "backup failed: pg_dump failed: boom" in notifier.sent[0].message
        )
        err = capsys.readouterr().err
        assert "error: the test backup failed" in err and err[:4].isdigit()  # timestamped

    def test_unattended_missing_postgres_programs_notify(self, bk, notifier, monkeypatch):
        missing = bk.replace(pg_bin_dir=bk.backups_dir / "no-such-bin")
        assert backup_command.run_backup(args(unattended=True), missing) == 1
        assert len(notifier.sent) == 1
        assert "PostgreSQL 18 not found" in notifier.sent[0].message
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: True)
        assert backup_command.run_backup(args(unattended=True), missing) == 1  # pg_dump too
        assert len(notifier.sent) == 1  # once a day
        assert backup_command.run_backup(args(), missing) == 1  # in Terminal: printed only
        assert len(notifier.sent) == 1

    def test_unattended_keychain_approval_exits_75_and_notifies_once(
        self, bk, notifier, monkeypatch
    ):
        def locked(*a, **k):
            raise app_secrets.KeychainApprovalNeeded("db.password.budgetapp_live", -25308)

        monkeypatch.setattr(backup_command.backups, "run_backup", locked)
        assert backup_command.run_backup(args(unattended=True), bk) == 75
        assert backup_command.run_backup(args(unattended=True), bk) == 75
        assert len(notifier.sent) == 1 and "Keychain" in notifier.sent[0].title

    def test_a_database_that_never_comes_up_is_not_an_alert_unless_backups_are_stale(
        self, bk, notifier, monkeypatch
    ):
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: False)
        fake_dump(bk, backup_command.clock.now() - dt.timedelta(hours=2))
        assert backup_command.run_backup(args(unattended=True), bk) == 75
        assert notifier.sent == []
        for path in bk.backups_dir.iterdir():
            path.unlink()
        assert backup_command.run_backup(args(unattended=True), bk) == 75
        assert len(notifier.sent) == 1 and "no verified test backup" in notifier.sent[0].message

    def test_interactive_runs_say_the_database_is_down(self, bk, monkeypatch, capsys):
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: False)
        assert backup_command.run_backup(args(), bk) == 1
        assert "./run db start" in capsys.readouterr().err

    def test_keep_must_be_positive(self, bk):
        assert backup_command.run_backup(args(keep=0), bk) == 2

    def test_an_unattended_run_notes_that_the_agent_ran(self, bk, notifier, monkeypatch):
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: False)
        assert backup.agent_last_run(bk) is None
        backup_command.run_backup(args(), bk)
        assert backup.agent_last_run(bk) is None  # by hand: not the agent
        with clock.frozen(now=NOW):
            backup_command.run_backup(args(unattended=True), bk)
        assert backup.agent_last_run(bk) == NOW

    def test_an_unattended_run_trims_oversize_logs(self, bk, notifier, monkeypatch):
        """Final review ops-7: nothing capped the logs launchd and PostgreSQL write; a crash loop
        appends a traceback every 30 s. The backup agent trims them, even with the database down
        (a postgres crash loop is exactly that)."""
        trimmed = []
        monkeypatch.setattr(
            backup_command, "trim_logs", lambda folder: trimmed.append(folder) or []
        )
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: False)
        backup_command.run_backup(args(), bk)
        assert trimmed == []  # by hand: not the agent's job
        backup_command.run_backup(args(unattended=True), bk)
        assert trimmed == [bk.logs_dir]

    def test_the_skip_message_names_the_schedule(self, bk, capsys):
        backup.run_backup(bk)
        assert backup_command.run_backup(args(if_due=True, min_age_hours=None), bk) == 0
        assert "since the backup due" in capsys.readouterr().out


class TestSyncAgentChecksBackups:
    """The sync agent (four times a day) checks that backups still happen (final review ops-2)."""

    @pytest.fixture
    def wired(self, monkeypatch):
        from budgetapp.commands import sync as sync_command

        calls = []

        class FakeRuntime:
            def __init__(self, settings):
                self.notifier = RecordingNotifier()

            def close(self):
                calls.append("closed")

        monkeypatch.setattr(sync_command, "Runtime", FakeRuntime)
        monkeypatch.setattr(sync_command, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(sync_command, "sync_command", lambda a, rt: calls.append("sync") or 0)
        monkeypatch.setattr(
            sync_command.backups,
            "alert_if_backups_stopped",
            lambda settings, notifier: calls.append(("check", settings.env)),
        )
        return sync_command, calls

    def test_after_an_unattended_production_sync(self, wired, tmp_path):
        sync_command, calls = wired
        prod = load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "l")
        assert sync_command.run(argparse.Namespace(unattended=True), prod) == 0
        assert calls == ["sync", ("check", "production"), "closed"]

    def test_never_by_hand_or_outside_production(self, wired, bk, tmp_path):
        sync_command, calls = wired
        prod = load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "l")
        sync_command.run(argparse.Namespace(unattended=False), prod)
        sync_command.run(argparse.Namespace(unattended=True), bk)
        assert calls == ["sync", "closed", "sync", "closed"]
