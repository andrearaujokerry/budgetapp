"""``./run recategorize [--since] [--only-uncategorized] [--dry-run]`` (CAT-4)."""

import datetime as dt

import pytest

from budgetapp import cli
from budgetapp.commands import recategorize as command
from budgetapp.db import DatabaseUnavailable
from tests.factories import get_category, make_account, make_rule, make_transaction, uncategorized


@pytest.fixture
def world(db):
    account = make_account(db)
    travel = get_category(db, "Travel")
    make_rule(db, travel, merchant_pattern="DELTA")
    rows = [
        make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 3, 1)),
        make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 10, 1)),
        make_transaction(
            db,
            account,
            name="DELTA AIR",
            date=dt.date(2026, 10, 2),
            category=get_category(db, "Shopping"),
            category_source="user",
        ),
    ]
    return travel, rows


@pytest.fixture
def use_test_database(monkeypatch, session_factory):
    """The command's database is the test's rolled-back connection."""
    closed = []
    monkeypatch.setattr(
        command, "open_database", lambda settings: (session_factory, lambda: closed.append(1))
    )
    return closed


def test_dry_run_prints_counts_and_saves_nothing(db, world, session_factory):
    travel, rows = world
    lines = []
    assert command.recategorize(session_factory, dry_run=True, out=lines.append) == 0
    assert lines == [
        "Dry run for all past transactions: nothing was saved.",
        "  2 checked",
        "  2 would change category",
        "  1 kept: you set their category yourself",
    ]
    db.expire_all()
    assert {r.category_id for r in rows[:2]} == {uncategorized(db).id}


def test_applies_and_commits(db, world, session_factory):
    travel, rows = world
    lines = []
    command.recategorize(session_factory, since=dt.date(2026, 9, 1), out=lines.append)
    assert lines[0] == "Re-applied rules and categories to transactions since Sep 1, 2026."
    assert lines[2] == "  1 changed category"
    db.expire_all()
    assert rows[1].category_id == travel.id and rows[1].category_source == "rule"
    assert rows[0].category_id == uncategorized(db).id
    assert rows[2].category_source == "user"


def test_cli_wiring(db, world, use_test_database, capsys):
    travel, rows = world
    assert cli.main(["recategorize", "--only-uncategorized", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Dry run for Uncategorized transactions: nothing was saved.")
    assert "2 would change category" in out
    assert use_test_database == [1]  # the engine is closed afterwards
    assert cli.main(["recategorize"]) == 0
    db.expire_all()
    assert rows[0].category_id == travel.id


def test_bad_date_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["recategorize", "--since", "31/01/2026"])
    assert exit_info.value.code == 2
    assert "not a date" in capsys.readouterr().err


def test_a_stopped_database_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(command, "wait_for_db", lambda settings, timeout: False)
    assert cli.main(["recategorize", "--dry-run"]) == 1
    assert command.NOT_RUNNING in capsys.readouterr().err
    with pytest.raises(DatabaseUnavailable):
        command.open_database(object())


def test_wide_numbers_line_up():
    from budgetapp.services.rules import ReapplyResult, ReapplyScope

    result = ReapplyResult(ReapplyScope(), True, 12345, 7, 120, ())
    assert command.report_lines(result)[1:] == [
        "  12,345 checked",
        "       7 would change category",
        "     120 kept: you set their category yourself",
    ]
