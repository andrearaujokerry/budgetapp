"""``./run import-csv FILE [--account ID] [--dry-run]`` (SYNC-8)."""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import func, select

from budgetapp import cli
from budgetapp.commands import import_csv
from budgetapp.models import Account, Transaction
from budgetapp.services import csv_import
from tests.factories import make_account, make_item, make_transaction
from tests.test_csv_import import FIXTURES, amex_csv, amex_row, fixture


@pytest.fixture(autouse=True)
def _forget_previews():
    yield
    csv_import._PREVIEWS.clear()


def run(db, data: bytes, **kwargs) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = import_csv.import_file(db, data, "activity.csv", out=lines.append, **kwargs)
    return code, lines


def count(db) -> int:
    return db.scalar(select(func.count()).select_from(Transaction))


def test_the_command_is_registered():
    args, _ = cli.build_parser().parse_known_args(
        ["import-csv", "activity.csv", "--account", "3", "--dry-run"]
    )
    assert (args.file, args.account, args.dry_run) == ("activity.csv", 3, True)
    assert args.handler is import_csv.run


def test_a_dry_run_prints_the_preview_and_imports_nothing(db):
    code, lines = run(db, fixture("activity_malformed.csv"), dry_run=True)
    assert code == 0
    text = "\n".join(lines)
    assert lines[0] == "activity.csv → American Express (CSV) (new account)"
    assert "11 rows (UTF-8), 2025-06-02 to 2025-06-04" in text
    assert "New:                                    2" in text
    assert "Errors:                                 9" in text
    assert "row     3  “13/45/2025” isn’t a date (expected M/D/YYYY)." in text
    assert "Will be added (2):" in text
    assert lines[-1] == "Dry run: nothing was imported."
    assert count(db) == 0 and db.scalar(select(func.count()).select_from(Account)) == 0
    assert csv_import._PREVIEWS == {}


def test_an_import(db):
    code, lines = run(db, fixture("activity_2025-08.csv"))
    assert code == 0
    assert lines[-1] == (
        "Imported activity.csv: 15 added, 0 merged with Plaid transactions, 0 already imported, "
        "0 errors."
    )
    assert count(db) == 15
    code, lines = run(db, fixture("activity_2025-08.csv"))  # the only manual account now
    assert code == 0 and lines[-1] == "Nothing new to import."
    assert "Already imported:                      15" in "\n".join(lines)
    assert count(db) == 15


def test_rows_held_for_review_are_mentioned(db):
    from budgetapp import clock

    item = make_item(db, history_complete_at=clock.now())
    account = make_account(db, item)
    make_transaction(db, account, amount="40.00", date="2025-10-01")
    code, lines = run(db, amex_csv(amex_row("2025-10-02", "HOTEL", "312.00")))
    assert code == 0
    assert "1 added (1 held for review on the Import page)" in lines[-1]


def test_changed_rows_are_reported_apart(db):
    """Review finding: they were counted as "already imported"."""
    reference = "320252140000004242"
    run(db, amex_csv(amex_row("2025-08-01", "PHILZ", "5.25", reference=reference)))
    code, lines = run(
        db,
        amex_csv(
            amex_row("2025-08-01", "PHILZ", "6.25", reference=reference),
            amex_row("2025-08-03", "BLUE BOTTLE", "4.00"),
        ),
    )
    assert code == 0
    assert lines[-1] == (
        "Imported activity.csv: 1 added, 0 merged with Plaid transactions, 0 already imported, "
        "1 changed since an earlier import (skipped), 0 errors."
    )


def test_rows_pending_in_plaid_are_explained(db):
    from budgetapp import clock

    item = make_item(db, history_complete_at=clock.now())
    account = make_account(db, item)
    make_transaction(db, account, amount="40.00", date="2025-10-01")
    make_transaction(db, account, amount="64.10", date="2025-10-20", pending=True, name="SAFEWAY")
    data = amex_csv(amex_row("2025-10-19", "SAFEWAY #1490", "64.10"))
    code, lines = run(db, data, dry_run=True)
    assert code == 0
    text = "\n".join(lines)
    assert "1 of the rows held for review match charges Plaid still shows as pending" in text
    assert "SAFEWAY #1490  (pending in Plaid: SAFEWAY (2025-10-20); merges when it posts)" in text


def test_an_explicit_account(db):
    target = make_account(db, manual=True, name="Old card")
    make_account(db, manual=True)
    code, _ = run(db, fixture("activity_cp1252.csv"), account_id=target.id)
    assert code == 0
    assert db.scalar(select(func.count()).where(Transaction.account_id == target.id)) == 2


def test_an_ambiguous_target_lists_the_choices(db):
    item = make_item(db)
    first = make_account(db, item, name="Gold Card", mask="1001")
    make_account(db, item, name="Blue Card", mask="2002")
    code, lines = run(db, fixture("activity_cp1252.csv"))
    assert code == 1
    assert lines[0].startswith("error: Choose the account to import into with --account:")
    assert f"  --account {first.id}  Gold Card ··1001" in lines[0]


def test_the_basic_download_is_refused(db):
    code, lines = run(db, fixture("activity_basic.csv"))
    assert code == 1 and "basic download" in lines[0]


def test_run_reads_the_file_and_refuses_a_basic_download(settings, capsys):
    args = argparse.Namespace(file=str(FIXTURES / "activity_basic.csv"), account=None, dry_run=True)
    assert import_csv.run(args, settings) == 1  # read-only: the file is refused before writing
    assert "basic download" in capsys.readouterr().out


def test_run_reports_a_missing_file(settings, capsys, tmp_path):
    args = argparse.Namespace(file=str(tmp_path / "missing.csv"), account=None, dry_run=True)
    assert import_csv.run(args, settings) == 2
    assert "can't read" in capsys.readouterr().err


def test_run_refuses_a_huge_file(settings, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(csv_import, "MAX_FILE_BYTES", 10)
    path = tmp_path / "big.csv"
    path.write_bytes(b"x" * 100)
    args = argparse.Namespace(file=str(path), account=None, dry_run=True)
    assert import_csv.run(args, settings) == 1
    assert "larger than" in capsys.readouterr().err
