"""``import-csv FILE [--account ID] [--dry-run]`` — import an extended Amex CSV download (SYNC-8).

Shows the same preview as the Import page (new rows, rows held for review, rows that match a Plaid
transaction, rows already imported, changed rows and errors), then imports unless ``--dry-run``.
Without ``--account`` the file goes to the only linked credit-card account, else to the manual
account "American Express (CSV)" (created when needed). The file is read into memory only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from budgetapp.commands import add_command
from budgetapp.config import Settings
from budgetapp.money import format_money
from budgetapp.services import csv_import
from budgetapp.services.notify import Notifier

Printer = Callable[[str], None]

GROUP_TITLES = {
    "new": "Will be added",
    "suspect": "Will be added but held for review (inside Plaid's history, no matching Plaid charge)",
    "matched": "Already synced from Plaid (merged, not added again)",
    "duplicate": "Already imported",
    "changed": "Imported before with a different date or amount (skipped)",
}
MAX_ERRORS_SHOWN = 20


def register(subparsers: Any) -> None:
    parser = add_command(
        subparsers,
        "import-csv",
        handler=run,
        help="import an Amex CSV download (extended format) into an account",
        description=__doc__,
    )
    parser.add_argument(
        "file", metavar="FILE", help="the CSV file downloaded from americanexpress.com"
    )
    parser.add_argument("--account", type=int, metavar="ID", help="target account (database id)")
    parser.add_argument("--dry-run", action="store_true", help="preview without importing")


def run(args: argparse.Namespace, settings: Settings) -> int:
    path = Path(args.file).expanduser()
    try:
        data = _read(path)
    except OSError as exc:
        print(f"error: can't read {path}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except csv_import.ImportRefused as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    from budgetapp import db
    from budgetapp.services.notify import get_notifier

    if not db.wait_for_db(settings, timeout=30):
        print("The database is not running. Start it with: ./run db start", file=sys.stderr)
        return 1
    engine = db.make_engine(settings)
    try:
        db.schema_guard(engine)
        with db.make_session_factory(engine)() as session:
            return import_file(
                session,
                data,
                path.name,
                account_id=args.account,
                dry_run=args.dry_run,
                production=settings.is_production,
                notifier=get_notifier(settings),
            )
    finally:
        engine.dispose()


def _read(path: Path) -> bytes:
    size = path.stat().st_size
    if size > csv_import.MAX_FILE_BYTES:
        raise csv_import.ImportRefused(
            "too_large", f"{path.name} is larger than {csv_import.MAX_FILE_BYTES // 2**20} MB."
        )
    return path.read_bytes()


def choose_account(session: Session, account_id: int | None, *, production: bool) -> int | None:
    """``--account`` or the page's default; ``None`` means a new manual account."""
    if account_id is not None:
        return account_id
    targets = csv_import.import_targets(session, production=production)
    if targets.default is None:
        choices = "\n".join(
            f"  --account {o.value}  {o.label}" for o in targets.options if o.value.isdigit()
        )
        raise csv_import.ImportRefused(
            "account", f"Choose the account to import into with --account:\n{choices}"
        )
    return csv_import.parse_target(targets.default)


def import_file(
    session: Session,
    data: bytes,
    filename: str,
    *,
    account_id: int | None = None,
    dry_run: bool = False,
    production: bool = False,
    notifier: Notifier | None = None,
    out: Printer = print,
) -> int:
    """Preview (and unless ``dry_run``, import) ``data``; commits ``session``. Returns the exit code."""
    try:
        target = choose_account(session, account_id, production=production)
        preview = csv_import.preview(session, data, filename, target, production=production)
    except csv_import.ImportRefused as exc:
        out(f"error: {exc.message}")
        return 1
    print_preview(preview, out)
    if dry_run:
        csv_import.discard_preview(preview.token)
        out("Dry run: nothing was imported.")
        return 0
    if not preview.can_import:
        csv_import.discard_preview(preview.token)
        out("Nothing new to import.")
        return 0
    try:
        outcome = csv_import.commit(
            session, preview.token, notifier=notifier, production=production
        )
    except csv_import.ImportRefused as exc:
        session.rollback()
        out(f"error: {exc.message}")
        return 1
    session.commit()
    record = csv_import.import_record(session, outcome.csv_import_id or 0)
    held = record.suspects if record is not None else 0
    out(
        f"Imported {outcome.filename}: {outcome.inserted_count:,} added"
        f"{f' ({held:,} held for review on the Import page)' if held else ''}, "
        f"{outcome.matched_plaid_count:,} merged with Plaid transactions, "
        f"{outcome.duplicate_count:,} already imported, {outcome.error_count:,} errors."
    )
    return 0


def _row_line(row: csv_import.PreviewRow) -> str:
    text = f"    row {row.row_number:>5}  {row.date.isoformat()}  {format_money(row.amount):>12}  {row.name}"
    if row.plaid_name or row.plaid_date:
        text += f"  ↔ Plaid: {row.plaid_name or '?'} ({row.plaid_date})"
    if row.other_account:
        text += f"  (in {row.other_account})"
    if row.earlier_amount is not None:
        text += f"  (before: {format_money(row.earlier_amount)} on {row.earlier_date})"
    return text


def print_preview(preview: csv_import.ImportPreview, out: Printer = print) -> None:
    """The preview as text (the same groups as the Import page)."""
    dates = (
        f"{preview.date_min.isoformat()} to {preview.date_max.isoformat()}"
        if preview.date_min and preview.date_max
        else "no dates"
    )
    account = f"{preview.account_label}{' (new account)' if preview.new_account else ''}"
    out(f"{preview.filename} → {account}")
    out(f"  {preview.row_count:,} rows ({preview.encoding}), {dates}")
    if preview.same_file_imported_at is not None:
        out(f"  This exact file was imported before ({preview.same_file_imported_at:%Y-%m-%d}).")
    counts = [
        ("New", preview.new),
        ("Held for review", preview.suspects),
        ("Match a Plaid transaction", preview.matched),
        ("Already imported", preview.duplicates),
        ("Changed since an earlier import", preview.changed),
        ("Errors", len(preview.errors)),
    ]
    for label, count in counts:
        out(f"  {label + ':':<34}{count:>7,}")
    for group in preview.groups:
        if not group.count:
            continue
        shown = len(group.sample)
        more = f", first {shown}" if group.count > shown else ""
        out(f"  {GROUP_TITLES[group.key]} ({group.count:,}{more}):")
        for row in group.sample:
            out(_row_line(row))
    if preview.errors:
        out(f"  Errors ({len(preview.errors):,}):")
        for error in preview.errors[:MAX_ERRORS_SHOWN]:
            out(f"    row {error.row_number:>5}  {error.reason}")
        if len(preview.errors) > MAX_ERRORS_SHOWN:
            out(f"    … and {len(preview.errors) - MAX_ERRORS_SHOWN:,} more")
