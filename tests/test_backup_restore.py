"""Restore (ARCHITECTURE §10): real dump → change → restore round trips on a scratch
``budgetapp_test_*`` database, and the refusals that must leave everything as it was."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import shlex
import subprocess
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from budgetapp import cli, db
from budgetapp.commands import backup as backup_command
from budgetapp.config import load_settings
from budgetapp.services import backup
from tests.conftest import create_scratch_database, drop_scratch_database
from tests.factories import make_account, make_transaction


@pytest.fixture
def scratch(settings, test_database, tmp_path):
    """A migrated scratch database of this process, with backups in a temporary folder."""
    name = f"{settings.db_name}_rs"
    create_scratch_database(settings, name)
    scratch_settings = load_settings(
        "test", db_name=name, backups_dir=tmp_path / "backups", state_dir=tmp_path / "state"
    )
    engine = db.make_engine(scratch_settings, poolclass=NullPool)
    try:
        db.run_migrations(engine)
        yield scratch_settings, engine
    finally:
        engine.dispose()
        drop_scratch_database(settings, name)


def names(engine) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT name FROM transactions ORDER BY name")).scalars())


def seed(engine, *txn_names: str, item: bool = False) -> None:
    with Session(engine) as session:
        account = make_account(session) if item else make_account(session, manual=True)
        for name in txn_names:
            make_transaction(session, account, name=name)
        session.commit()


def table_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}).scalar()
            is not None
        )


@pytest.fixture
def dumped(scratch):
    """``(settings, engine, dump)``: the dump holds Coffee; the database then moves on."""
    settings, engine = scratch
    seed(engine, "Coffee")
    dump = backup.run_backup(settings).path
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM transactions WHERE name = 'Coffee'"))
    seed(engine, "Lunch")
    with engine.begin() as conn:  # an object a newer migration might have made
        conn.execute(text("CREATE TABLE extra_from_newer_code (id int PRIMARY KEY)"))
        conn.execute(text("CREATE VIEW v_extra AS SELECT id FROM extra_from_newer_code"))
    return settings, engine, dump


class TestRestore:
    def test_a_round_trip_replaces_everything_in_one_go(self, dumped):
        settings, engine, dump = dumped
        plan = backup.inspect_backup(settings, dump)
        assert plan.revision == plan.code_head and not plan.needs_upgrade
        assert plan.info.database == settings.db_name
        said: list[str] = []
        result = backup.restore(settings, plan, say=said.append, engine=engine)
        assert names(engine) == ["Coffee"]
        assert not table_exists(engine, "extra_from_newer_code")
        assert not table_exists(engine, "v_extra")
        assert result.schema_ok and "current" in result.schema_message
        assert result.counts["transactions"] == 1 and result.counts["accounts"] == 1
        pre = result.pre_restore
        assert pre is not None and pre.path.name.endswith("-pre-restore.dump")
        assert backup.parse_backup_name(pre.path)[3] is True
        assert pre.path.stat().st_mode & 0o777 == 0o600
        assert said[0].startswith("Saved the current data first (verified): ")
        assert any(line.startswith("Restored ") for line in said)
        assert list(Path(settings.backups_dir).glob(".restore-*")) == []  # script removed
        # The pre-restore dump holds the replaced data (Lunch): restoring it undoes the restore.
        backup.restore(
            settings, backup.inspect_backup(settings, pre.path), engine=engine, say=print
        )
        assert names(engine) == ["Lunch"]

    def test_objects_stay_owned_by_the_environment_role(self, dumped):
        settings, engine, dump = dumped
        backup.restore(settings, backup.inspect_backup(settings, dump), engine=engine, say=print)
        with engine.connect() as conn:
            owners = set(
                conn.execute(
                    text(
                        "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public' "
                        "UNION SELECT DISTINCT viewowner FROM pg_views WHERE schemaname = 'public'"
                    )
                ).scalars()
            )
        assert owners == {"budgetapp_test"}

    def test_a_failure_rolls_everything_back(self, dumped, monkeypatch):
        settings, engine, dump = dumped
        monkeypatch.setattr(
            backup, "_CHECK", "DO $x$ BEGIN RAISE EXCEPTION 'check failed on purpose'; END $x$;"
        )
        with pytest.raises(backup.RestoreFailed, match="rolled back.*check failed on purpose"):
            backup.restore(
                settings, backup.inspect_backup(settings, dump), engine=engine, say=print
            )
        assert names(engine) == ["Lunch"]  # the drops and the replay were rolled back too
        assert table_exists(engine, "extra_from_newer_code")

    def test_other_connections_are_refused_or_ended(self, dumped):
        settings, engine, dump = dumped
        plan = backup.inspect_backup(settings, dump)
        other = psycopg.connect(
            host=str(settings.pg_socket_dir),
            port=settings.pg_port,
            dbname=settings.db_name,
            user=settings.db_role,
            application_name="budgetapp-test",
        )
        try:
            with pytest.raises(backup.RestoreRefused, match="1 other connection.*budgetapp-test"):
                backup.restore(settings, plan, engine=engine, say=print)
            assert names(engine) == ["Lunch"]
            assert list(Path(settings.backups_dir).glob("*-pre-restore.dump")) == []
            result = backup.restore(settings, plan, force_disconnect=True, engine=engine, say=print)
            assert [s.application for s in result.terminated] == ["budgetapp-test"]
            assert names(engine) == ["Coffee"]
            with pytest.raises(psycopg.OperationalError):
                other.execute("SELECT 1")
        finally:
            other.close()

    def test_a_running_sync_is_never_interrupted(self, scratch):
        settings, engine = scratch
        seed(engine, "Coffee", item=True)
        dump = backup.run_backup(settings).path
        with engine.connect() as conn:
            item_id = conn.execute(text("SELECT id FROM plaid_items")).scalar_one()
        other = db.make_engine(settings, poolclass=NullPool)
        try:
            with db.sync_lock(other, item_id) as held:
                assert held
                for force in (False, True):  # --force-disconnect never ends a running sync
                    with pytest.raises(backup.RestoreRefused, match="A sync is running"):
                        backup.restore(
                            settings,
                            backup.inspect_backup(settings, dump),
                            engine=engine,
                            say=print,
                            force_disconnect=force,
                        )
                assert len(backup.sync_lock_holders(engine)) == 1
            assert backup.sync_lock_holders(engine) == []
        finally:
            other.dispose()
        assert names(engine) == ["Coffee"]

    def test_the_sync_lock_is_held_during_the_restore(self, scratch, monkeypatch):
        settings, engine = scratch
        seed(engine, "Coffee", item=True)
        dump = backup.run_backup(settings).path
        seen = []
        real = backup.apply_restore_script

        def spying(settings_, script, **kwargs):
            with engine.connect() as conn:
                seen.append(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND classid = :ns AND database = "
                            "(SELECT oid FROM pg_database WHERE datname = current_database())"
                        ),
                        {"ns": db.LOCK_NS_SYNC},
                    ).scalar()
                )
            return real(settings_, script, **kwargs)

        monkeypatch.setattr(backup, "apply_restore_script", spying)
        backup.restore(settings, backup.inspect_backup(settings, dump), engine=engine, say=print)
        assert seen == [1]


class TestRefusals:
    def test_a_backup_of_another_database_is_refused(self, dumped, settings):
        scratch_settings, engine, dump = dumped
        other = load_settings("test", backups_dir=scratch_settings.backups_dir)
        with pytest.raises(backup.RestoreRefused, match="This backup is of the database"):
            backup.inspect_backup(other, dump)

    def test_damaged_files_are_refused_before_anything_changes(self, dumped, tmp_path):
        settings, engine, dump = dumped
        truncated = tmp_path / "truncated.dump"
        truncated.write_bytes(dump.read_bytes()[:-1000])
        with pytest.raises(
            backup.RestoreRefused, match="incomplete or damaged.*Nothing was changed"
        ):
            backup.inspect_backup(settings, truncated)
        junk = tmp_path / "junk.dump"
        junk.write_bytes(b"not a dump at all")
        with pytest.raises(backup.RestoreRefused, match="not a readable dump"):
            backup.inspect_backup(settings, junk)
        with pytest.raises(backup.RestoreRefused, match="There is no backup file"):
            backup.inspect_backup(settings, tmp_path / "missing.dump")
        assert names(engine) == ["Lunch"]

    def test_a_backup_from_newer_code_is_refused(self, dumped, monkeypatch):
        settings, engine, dump = dumped
        monkeypatch.setattr(backup, "known_revisions", lambda: {"0001_initial"})
        with pytest.raises(backup.RestoreRefused, match="newer version of the app"):
            backup.inspect_backup(settings, dump)

    def test_a_backup_from_older_code_needs_an_upgrade_afterwards(self, scratch):
        settings, engine = scratch
        db.downgrade_migrations(engine, "0002_seed_defaults")
        old = backup.run_backup(settings).path
        db.run_migrations(engine)
        plan = backup.inspect_backup(settings, old)
        assert plan.revision == "0002_seed_defaults" and plan.needs_upgrade
        result = backup.restore(settings, plan, engine=engine, say=print)
        assert not result.schema_ok
        assert "0002_seed_defaults" in result.schema_message
        assert "the test harness migrates" in result.schema_message
        assert db.current_revision(engine) == "0002_seed_defaults"


class FakeAgents:
    def __init__(self, stopped=("web", "backup", "sync")):
        self.events: list[str] = []
        self._stopped = list(stopped)

    def stop_app_agents(self):
        self.events.append("stop")
        return list(self._stopped)

    def start_agents(self, names):
        self.events.append(f"start:{','.join(names)}")
        return list(names)


class FakeTools:
    """pg_dump/pg_restore/psql stand-ins: production restores never run real tools in tests."""

    def __init__(self, psql_code: int = 0):
        self.psql_code = psql_code
        self.calls: list[str] = []

    def __call__(self, argv, **kwargs):
        name = Path(argv[0]).name
        self.calls.append(name)
        for arg in argv:
            if arg.startswith("--file=") and arg != "--file=/dev/null":
                Path(arg.split("=", 1)[1]).write_text(f"SELECT 1;\n{backup.SCRIPT_COMPLETE}\n")
        if name == "pg_restore" and "--list" in argv:
            out = ";\n;     dbname: budgetapp\n;     Format: CUSTOM\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if name == "psql":
            return subprocess.CompletedProcess(argv, self.psql_code, "", "ERROR:  boom")
        return subprocess.CompletedProcess(argv, 0, "", "")


class TestProductionAgents:
    """The agent stop/start order with fakes; the database side runs on the scratch database."""

    @pytest.fixture
    def prod(self, tmp_path, secrets):
        secrets.values["db.password.budgetapp_live"] = "not-a-real-password-1"
        return load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")

    def plan(self, prod, tmp_path):
        dump = tmp_path / "budgetapp-20261029T023000Z.dump"
        dump.write_bytes(b"PGDMP")
        info = backup.parse_dump_header(";     dbname: budgetapp\n")
        return backup.RestorePlan(dump, info, db.code_head(), db.code_head(), 5)

    def test_agents_stop_first_and_start_again(self, prod, scratch, tmp_path):
        _, engine = scratch
        agents = FakeAgents()
        said = []
        tools = FakeTools()
        result = backup.restore(
            prod,
            self.plan(prod, tmp_path),
            agents=agents,
            engine=engine,
            runner=tools,
            say=said.append,
        )
        assert agents.events == ["stop", "start:web,backup,sync"]
        assert result.stopped_agents == ("web", "backup", "sync")
        assert said[0] == "Stopped the web, backup, sync agent(s) for the restore."
        assert said[-1] == "Started the web, backup, sync agent(s) again."
        # The dump is read into the script first, then the current data is copied (and
        # verified twice), then the one-transaction replay.
        assert tools.calls == ["pg_restore", "pg_dump", "pg_restore", "pg_restore", "psql"]

    def test_agents_start_again_after_a_failure(self, prod, scratch, tmp_path):
        _, engine = scratch
        agents = FakeAgents()
        with pytest.raises(backup.RestoreFailed, match="boom"):
            backup.restore(
                prod,
                self.plan(prod, tmp_path),
                agents=agents,
                engine=engine,
                runner=FakeTools(psql_code=3),
                say=print,
            )
        assert agents.events == ["stop", "start:web,backup,sync"]

    def test_production_needs_agent_control(self, prod, tmp_path):
        with pytest.raises(backup.RestoreRefused, match="agent control"):
            backup.restore(prod, self.plan(prod, tmp_path), say=print)


# --------------------------------------------------------------------------- command


def restore_args(path, force=False, without_copy=False):
    return argparse.Namespace(
        file=str(path), force_disconnect=force, without_safety_copy=without_copy
    )


def parse_printed(command: str) -> argparse.Namespace:
    """What the shell and ./run make of a printed ``./run …`` command line."""
    argv = shlex.split(command)
    assert argv[0] == "./run"
    return cli.build_parser().parse_args(argv[1:])


class TestRestoreCommand:
    def test_the_typed_confirmation_is_required(self, dumped, monkeypatch, capsys):
        settings, engine, dump = dumped
        monkeypatch.setattr(backup_command, "prompt", lambda text: "yes")
        assert backup_command.run_restore(restore_args(dump), settings) == 1
        out = capsys.readouterr().out
        assert f"Restore this backup into the TEST database {settings.db_name}?" in out
        assert "Cancelled; nothing was changed." in out
        assert names(engine) == ["Lunch"]

    def test_end_of_input_cancels(self, dumped, monkeypatch, capsys):
        settings, engine, dump = dumped

        def eof(text):
            raise EOFError

        monkeypatch.setattr(backup_command, "prompt", eof)
        assert backup_command.run_restore(restore_args(dump), settings) == 1
        assert names(engine) == ["Lunch"]

    def test_confirmed_restore_reports_each_step(self, dumped, monkeypatch, capsys):
        settings, engine, dump = dumped
        engine.dispose()
        monkeypatch.setattr(backup_command, "prompt", lambda text: " test ")
        assert backup_command.run_restore(restore_args(dump), settings) == 0
        out = capsys.readouterr().out
        assert "Saved the current data first (verified):" in out
        assert f"Restored {dump.name} into {settings.db_name}." in out
        assert "The database now holds 1 transactions, 1 accounts, 0 plaid items" in out
        assert "To undo, restore the copy taken first: ./run restore " in out
        assert names(engine) == ["Coffee"]

    def test_refusals_are_printed_and_change_nothing(self, dumped, settings, monkeypatch, capsys):
        scratch_settings, engine, dump = dumped
        other = load_settings("test", backups_dir=scratch_settings.backups_dir)
        assert backup_command.run_restore(restore_args(dump), other) == 1
        assert "Not restored: This backup is of the database" in capsys.readouterr().err

    def test_production_needs_an_explicit_env(self, monkeypatch, capsys, tmp_path):
        prod = load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
        monkeypatch.setenv("BUDGETAPP_ENV", "production")
        monkeypatch.setattr(backup_command.sys, "argv", ["budgetapp", "restore", "x.dump"])
        assert backup_command.run_restore(restore_args(tmp_path / "x.dump"), prod) == 2
        assert "explicit --env production" in capsys.readouterr().err
        assert backup_command.env_was_explicit(["restore", "x.dump", "--env", "production"])
        assert backup_command.env_was_explicit(["restore", "x.dump", "--env=production"])
        monkeypatch.setenv("BUDGETAPP_ENV", "test")
        assert backup_command.env_was_explicit([])

    def test_production_confirmation_is_the_word_production(self, monkeypatch, capsys, tmp_path):
        prod = load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
        dump = tmp_path / "budgetapp-20261029T023000Z.dump"
        plan = backup.RestorePlan(
            dump, backup.parse_dump_header(";     dbname: budgetapp\n"), "0003", "0003", 5
        )
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: True)
        monkeypatch.setattr(backup_command.backups, "inspect_backup", lambda s, p: plan)
        called = []
        monkeypatch.setattr(backup_command.backups, "restore", lambda *a, **k: called.append(1))
        monkeypatch.setattr(backup_command, "prompt", lambda text: "test")
        assert backup_command.run_restore(restore_args(dump), prod) == 1
        out = capsys.readouterr().out
        assert "the PRODUCTION database budgetapp" in out
        assert "The web, sync and backup agents stop during the restore" in out
        assert called == []

    def test_the_database_must_be_up(self, dumped, monkeypatch, capsys):
        settings, engine, dump = dumped
        monkeypatch.setattr(backup_command, "wait_for_db", lambda settings, timeout: False)
        assert backup_command.run_restore(restore_args(dump), settings) == 1
        assert "./run db start" in capsys.readouterr().err


def test_restore_scripts_drop_functions_types_and_sequences_too(scratch):
    """The prelude empties ``public`` whatever a newer migration added (not just tables)."""
    settings, engine = scratch
    seed(engine, "Coffee")
    dump = backup.run_backup(settings).path
    with engine.begin() as conn:
        conn.execute(text("CREATE TYPE mood AS ENUM ('ok', 'meh')"))
        conn.execute(text("CREATE DOMAIN positive AS int CHECK (VALUE > 0)"))
        conn.execute(text("CREATE SEQUENCE loose_seq"))
        conn.execute(
            text("CREATE FUNCTION add_one(i int) RETURNS int LANGUAGE sql AS 'SELECT i + 1'")
        )
    backup.restore(settings, backup.inspect_backup(settings, dump), engine=engine, say=print)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regtype('public.mood')")).scalar() is None
        assert conn.execute(text("SELECT to_regtype('public.positive')")).scalar() is None
        assert conn.execute(text("SELECT to_regclass('public.loose_seq')")).scalar() is None
        assert conn.execute(text("SELECT to_regprocedure('public.add_one(int)')")).scalar() is None
    assert names(engine) == ["Coffee"]


def test_no_backup_names_collide_with_the_live_clock():
    when = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert when.strftime(backup.TIMESTAMP_FORMAT) == "20260102T030405Z"


# --------------------------------------------------------------------------- review regressions


def fill_with_newer_dumps(settings, count: int, start: dt.datetime) -> list[Path]:
    """``count`` final-named files, a day apart after ``start`` (only names matter to rotation)."""
    made = []
    for day in range(1, count + 1):
        when = start + dt.timedelta(days=day)
        path = Path(settings.backups_dir) / f"{settings.db_name}-{when:%Y%m%dT%H%M%SZ}.dump"
        path.write_bytes(b"PGDMP")
        os.chmod(path, 0o600)
        made.append(path)
    return made


class TestRestoreKeepsTheBackups:
    """Review blocker: after 30 nights the folder holds exactly 30 dumps, and restoring the
    OLDEST (the likely pick when the recent ones already hold the damage) deleted it: the copy
    of the current data made 31 and the rotation removed the file about to be restored."""

    @pytest.fixture
    def thirty(self, scratch):
        settings, engine = scratch
        seed(engine, "Coffee")
        start = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)
        oldest = backup.run_backup(settings, now=start).path
        newer = fill_with_newer_dumps(settings, 29, start)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions"))
        seed(engine, "Lunch")
        assert len(backup.list_backups(settings)) == 30
        return settings, engine, oldest, newer

    def test_restoring_the_oldest_of_30_keeps_it_and_every_other_backup(self, thirty):
        settings, engine, oldest, newer = thirty
        plan = backup.inspect_backup(settings, oldest)
        result = backup.restore(settings, plan, engine=engine, say=print)
        assert names(engine) == ["Coffee"]
        assert oldest.exists() and all(path.exists() for path in newer)
        kept = [b.path for b in backup.list_backups(settings, pre_restore=False)]
        assert len(kept) == 30 and kept[-1] == oldest
        copies = [b.path for b in backup.list_backups(settings, pre_restore=True)]
        assert copies == [result.pre_restore.path] and result.pruned == ()

    def test_the_command_restores_the_oldest_of_30(self, thirty, monkeypatch, capsys):
        settings, engine, oldest, newer = thirty
        engine.dispose()
        monkeypatch.setattr(backup_command, "prompt", lambda text: "test")
        assert backup_command.run_restore(restore_args(oldest), settings) == 0
        assert "Not restored" not in capsys.readouterr().err
        assert oldest.exists() and names(engine) == ["Coffee"]

    def test_failed_restores_delete_no_backup(self, thirty, monkeypatch):
        settings, engine, oldest, newer = thirty
        monkeypatch.setattr(backup, "_CHECK", "DO $x$ BEGIN RAISE EXCEPTION 'no'; END $x$;")
        for _ in range(2):  # each failed attempt used to delete one more old dump
            with pytest.raises(backup.RestoreFailed, match="database is unchanged"):
                backup.restore(
                    settings, backup.inspect_backup(settings, oldest), engine=engine, say=print
                )
        assert oldest.exists() and all(path.exists() for path in newer)
        assert len(backup.list_backups(settings, pre_restore=True)) == 2  # both copies stay
        assert names(engine) == ["Lunch"]

    def test_the_dump_is_read_before_anything_else_happens(self, dumped, monkeypatch):
        """Whatever happens to the file later, the restore already holds all of it."""
        settings, engine, dump = dumped
        plan = backup.inspect_backup(settings, dump)
        real = backup.run_backup

        def copy_then_lose_the_file(*args, **kwargs):
            dump.unlink()  # e.g. a manual backup's rotation in another Terminal
            return real(*args, **kwargs)

        monkeypatch.setattr(backup, "run_backup", copy_then_lose_the_file)
        backup.restore(settings, plan, engine=engine, say=print)
        assert names(engine) == ["Coffee"]

    def test_copies_are_pruned_after_success_but_never_the_restored_file(self, dumped):
        settings, engine, dump = dumped
        start = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
        copies = [
            backup.run_backup(settings, pre_restore=True, now=start + dt.timedelta(days=d)).path
            for d in range(5)
        ]
        # An undo with the OLDEST copy: the pruning after it must not delete that copy.
        first = backup.restore(
            settings, backup.inspect_backup(settings, copies[0]), engine=engine, say=print
        )
        assert first.pruned == () and copies[0].exists()
        assert len(backup.list_backups(settings, pre_restore=True)) == 6
        said: list[str] = []
        second = backup.restore(
            settings, backup.inspect_backup(settings, dump), engine=engine, say=said.append
        )
        assert set(second.pruned) == {copies[0], copies[1]}
        kept = {b.path for b in backup.list_backups(settings, pre_restore=True)}
        assert kept == {second.pre_restore.path, first.pre_restore.path, *copies[2:]}
        assert "Removed 2 older pre-restore copy(ies); the newest 5 are kept." in said
        assert dump.exists() and names(engine) == ["Coffee"]


class KillingAgents:
    """``launchctl bootout`` ends the agents' processes, which releases a sync lock they held."""

    def __init__(self, release):
        self.release = release
        self.events: list[str] = []

    def stop_app_agents(self):
        self.events.append("stop")
        self.release()
        return ["web", "backup", "sync"]

    def start_agents(self, names):
        self.events.append("start")
        return list(names)


class TestRestoreAndAgentSyncs:
    """Review minor: the agents were booted out BEFORE the sync locks were checked, so a sync
    the sync or web agent was running got killed instead of the restore being refused."""

    @pytest.fixture
    def linked(self, scratch):
        settings, engine = scratch
        seed(engine, "Coffee", item=True)
        plan = backup.inspect_backup(settings, backup.run_backup(settings).path)
        with engine.connect() as conn:
            item_id = conn.execute(text("SELECT id FROM plaid_items")).scalar_one()
        other = db.make_engine(settings, poolclass=NullPool)
        yield settings, engine, plan, item_id, other
        other.dispose()

    def test_an_agent_sync_refuses_the_restore_before_any_agent_stops(self, linked):
        settings, engine, plan, item_id, other = linked
        with contextlib.ExitStack() as running_sync:
            assert running_sync.enter_context(db.sync_lock(other, item_id))
            control = KillingAgents(running_sync.close)
            with pytest.raises(backup.RestoreRefused, match="A sync is running"):
                backup.restore(settings, plan, agents=control, engine=engine, say=print)
        assert control.events == []  # nothing stopped, so nothing was killed
        assert list(Path(settings.backups_dir).glob("*-pre-restore.dump")) == []
        assert names(engine) == ["Coffee"]

    def test_a_sync_an_agent_starts_during_the_stop_is_skipped(self, linked):
        settings, engine, plan, item_id, other = linked
        seen: list[bool] = []

        class StartingAgents(KillingAgents):
            def stop_app_agents(self):  # the sync agent fires while launchd stops the others
                with db.sync_lock(other, item_id) as acquired:
                    seen.append(acquired)
                return super().stop_app_agents()

        control = StartingAgents(lambda: None)
        said: list[str] = []
        result = backup.restore(settings, plan, agents=control, engine=engine, say=said.append)
        assert seen == [False]  # the lock was already the restore's: the run records "skipped"
        assert control.events == ["stop", "start"] and result.terminated == ()
        assert said[0] == "Holding the sync lock of 1 Item(s) until the restore ends."
        assert said[1] == "Stopped the web, backup, sync agent(s) for the restore."


class TestPrintedCommands:
    """Review minor: the undo command printed an unquoted path, which the shell splits at the
    space in ~/Library/Application Support."""

    def test_the_undo_command_survives_the_shell_in_a_folder_with_a_space(
        self, scratch, tmp_path, monkeypatch, capsys
    ):
        settings, engine = scratch
        spaced = settings.replace(backups_dir=tmp_path / "Application Support" / "backups")
        seed(engine, "Coffee")
        dump = backup.run_backup(spaced).path
        engine.dispose()
        monkeypatch.setattr(backup_command, "prompt", lambda text: "test")
        assert backup_command.run_restore(restore_args(dump), spaced) == 0
        out = capsys.readouterr().out
        line = next(x for x in out.splitlines() if x.startswith("To undo"))
        args = parse_printed(line.split(": ", 1)[1])
        assert args.command == "restore" and " " in args.file
        assert Path(args.file).name.endswith("-pre-restore.dump") and Path(args.file).exists()

    def test_production_commands_name_the_environment(self, tmp_path):
        prod = load_settings(
            "production", data_dir=tmp_path / "App Support" / "budgetapp", logs_dir=tmp_path
        )
        path = prod.backups_dir / "budgetapp-20261029T023000Z.dump"
        command = backup_command.restore_command(
            prod, path, force_disconnect=True, without_safety_copy=True
        )
        args = parse_printed(command)
        assert Path(args.file) == path and args.env == "production"
        assert args.force_disconnect and args.without_safety_copy
        assert backup_command.restore_command(load_settings("test"), path).endswith(
            shlex.quote(str(path))
        )


class TestWithoutSafetyCopy:
    """Review minor: when pg_dump can't read the current data (a damaged table), the copy taken
    first always failed and the restore was refused with no way forward."""

    @pytest.fixture
    def unreadable(self, scratch):
        settings, engine = scratch
        seed(engine, "Coffee")
        good = backup.run_backup(settings).path
        seed(engine, "Lunch")
        with engine.begin() as conn:  # pg_dump now fails on this table, as on a damaged one
            conn.execute(text("REVOKE SELECT ON transactions FROM CURRENT_USER"))
        engine.dispose()
        return settings, engine, good

    def test_the_refusal_offers_the_explicit_override(self, unreadable, monkeypatch, capsys):
        settings, engine, good = unreadable
        monkeypatch.setattr(backup_command, "prompt", lambda text: "test")
        assert backup_command.run_restore(restore_args(good, force=True), settings) == 1
        err = capsys.readouterr().err
        assert "Not restored: The copy of the current data failed (pg_dump failed:" in err
        assert "permission denied for table transactions" in err
        assert "the database was not changed" in err and "WITHOUT saving it first" in err
        args = parse_printed(err.strip().splitlines()[-1])
        assert Path(args.file) == good and args.without_safety_copy and args.force_disconnect
        assert list(Path(settings.backups_dir).glob("*-pre-restore.dump")) == []

    def test_the_override_restores_and_says_nothing_was_saved(
        self, unreadable, monkeypatch, capsys
    ):
        settings, engine, good = unreadable
        monkeypatch.setattr(backup_command, "prompt", lambda text: "test")
        assert backup_command.run_restore(restore_args(good, without_copy=True), settings) == 0
        out = capsys.readouterr().out
        assert "the current data is NOT backed up first (--without-safety-copy)" in out
        assert "Not saving the current data first (--without-safety-copy)." in out
        assert "The data it replaced was not saved (--without-safety-copy)." in out
        assert "To undo" not in out
        assert names(engine) == ["Coffee"]
        assert list(Path(settings.backups_dir).glob("*-pre-restore.dump")) == []

    def test_the_service_skips_only_the_copy(self, unreadable):
        settings, engine, good = unreadable
        plan = backup.inspect_backup(settings, good)
        with pytest.raises(backup.SafetyCopyFailed, match="permission denied"):
            backup.restore(settings, plan, engine=engine, say=print)
        result = backup.restore(settings, plan, engine=engine, say=print, safety_copy=False)
        assert result.pre_restore is None and names(engine) == ["Coffee"]
