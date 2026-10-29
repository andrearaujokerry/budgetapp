"""Restore (ARCHITECTURE §10): real dump → change → restore round trips on a scratch
``budgetapp_test_*`` database, and the refusals that must leave everything as it was."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from budgetapp import db
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
        assert tools.calls[0] == "pg_dump" and tools.calls[-1] == "psql"

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


def restore_args(path, force=False):
    return argparse.Namespace(file=str(path), force_disconnect=force)


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
