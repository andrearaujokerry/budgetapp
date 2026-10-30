"""Amex CSV import (SYNC-8) — ARCHITECTURE §5 (``transactions``, ``csv_imports``) and §6.

Only the **extended** download is accepted (americanexpress.com → Statements & Activity →
Download → CSV with "Include all additional transaction details" ticked): its ``Reference`` column
is the only safe duplicate key (identical same-day charges are common, RESEARCH T4.1b). The basic
download (``Date,Description,Amount``) is refused with instructions.

Parsing (:func:`parse_file`, no database): UTF-8 (BOM or not), else Windows-1252; ``csv`` with
``newline=''`` (CRLF and multi-line quoted cells); columns found by header name, in any order,
extra columns ignored; dates ``M/D/YYYY``, ``MM/DD/YY`` and ISO; amounts exact to the cent
(``$``, ``,`` and spaces stripped, 0–2 decimals; Amex's sign is Plaid's); References stripped of
their apostrophes and kept as text. Rows that can't be used are reported with their spreadsheet
row number and a reason.

Mapping: ``Description`` → ``name``, ``Appears On Your Statement As`` → ``original_description``,
``Category`` → ``external_category`` (blank for payments), ``Card Member`` → ``account_owner``,
the address fields and ``Extended Details`` → ``location``; ``source='csv'``, ``pending=false``,
``posted_date`` from the Reference when plausible (``reconcile.reference_posting_date``), else
the ``Date``. ``Account #`` is never used: it differs per additional card member, and the whole
file goes to one account the user chooses.

Flow: :func:`preview` parses and classifies every row (new, held for review, matches a Plaid
transaction, already imported, changed since an earlier import, error) without writing anything
and keeps the parsed rows in process memory under a random token for 30 minutes (uploaded files are
never written to disk). :func:`commit` inserts the new rows (``ON CONFLICT (csv_reference) DO
NOTHING``, so a re-import or an overlapping file adds nothing twice), categorizes them (rules,
then the Amex category mapping), records ``csv_imports``, runs ``reconcile.reconcile_item`` (rows
that pair with a Plaid transaction are merged into it: never stored twice) and calls
``alerts.evaluate_after_import``; a preview is imported once (committing it again answers with
that import). ``csv_imports.duplicate_count`` counts rows imported before and unchanged;
``error_count`` counts rows that couldn't be read plus rows changed since an earlier import (the
returned :class:`CsvImportOutcome` has both numbers apart). Every writing function takes
``db.ledger_lock`` first and never commits; the caller commits. Raw file contents never go to
the logs.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.db import ledger_lock
from budgetapp.models import Account, CsvImport, PlaidItem, Transaction
from budgetapp.money import CENT, to_money
from budgetapp.services import alerts, reconcile
from budgetapp.services.categorize import Categorizer
from budgetapp.services.events import ImportOutcome
from budgetapp.services.notify import Notifier, NullNotifier
from budgetapp.services.reconcile import CsvSide, ReconcileResult, reference_posting_date

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
PREVIEW_TTL = dt.timedelta(minutes=30)
MAX_PREVIEWS = 20
SAMPLE_SIZE = 5
HEADER_SCAN_ROWS = 20
FUTURE_TOLERANCE = dt.timedelta(days=7)
MANUAL_ACCOUNT_NAME = "American Express (CSV)"
NEW_ACCOUNT = "new"
#: Row ids are BIGINT; a larger number never names a row.
MAX_ROW_ID = 2**63 - 1
_CHUNK = 1000

DOWNLOAD_STEPS = (
    "On americanexpress.com, open Statements & Activity.",
    "Choose Custom Date Range (Amex keeps about two years online) and search.",
    "Click Download, choose CSV, then tick “Include all additional transaction details”.",
    "Upload the file here as downloaded: opening and saving it in Excel or Numbers changes the "
    "Reference numbers and dates.",
)
BASIC_FILE = (
    "This is Amex’s basic download (Date, Description, Amount only). It has no Reference "
    "numbers, so duplicates can’t be detected safely. Download it again: choose CSV, then tick "
    "“Include all additional transaction details”."
)
NO_REFERENCE = (
    "This file has no Reference column, so duplicates can’t be detected safely. Download it again "
    "from americanexpress.com with “Include all additional transaction details” ticked."
)

#: Recognised columns: field → header names (compared case-insensitively, spaces collapsed).
COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date",),
    "description": ("description",),
    "amount": ("amount",),
    "reference": ("reference",),
    "category": ("category",),
    "card_member": ("card member", "cardmember"),
    "statement_as": ("appears on your statement as", "doing business as"),
    "extended_details": ("extended details",),
    "address": ("address", "street address"),
    "city_state": ("city/state", "city / state"),
    "zip": ("zip code", "zip", "postal code"),
    "country": ("country",),
    "account_number": ("account #", "account", "account number"),
    "receipt": ("receipt",),
}
_ALIASES = {alias: name for name, aliases in COLUMNS.items() for alias in aliases}
_BASIC_FIELDS = frozenset({"date", "description", "amount", "receipt"})

_WEEKDAY = re.compile(r"\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?$", re.IGNORECASE)
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
_NUMBER = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)$")
_SCIENTIFIC = re.compile(r"^[-+]?\d+(\.\d+)?e\+?\d+$", re.IGNORECASE)
#: A cell that swallowed the following records (an unclosed quote): a line that starts a new row.
_SWALLOWED_ROW = re.compile(r"\n\s*(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})(\s+\w+)?\s*,")
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f]")


class ImportRefused(ValueError):
    """The file or the request can't be imported; ``message`` is written for the user.

    ``kind``: ``empty``, ``too_large``, ``not_amex``, ``basic``, ``no_reference``,
    ``no_description``, ``no_rows``, ``damaged``, ``account`` or ``expired``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One usable row of the file. ``row_number`` is the spreadsheet row (the header counts)."""

    row_number: int
    reference: str
    date: dt.date
    posted_date: dt.date
    amount: Decimal
    name: str
    original_description: str | None
    external_category: str | None
    account_owner: str | None
    location: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class RowError:
    """A row that can't be imported, with a reason for the user."""

    row_number: int
    reason: str


@dataclass(frozen=True, slots=True)
class ParsedFile:
    encoding: str
    columns: tuple[str, ...]
    rows: tuple[ParsedRow, ...]
    errors: tuple[RowError, ...]
    #: Data rows after the header (usable rows + errors; blank rows are not counted).
    data_rows: int


def decode(data: bytes) -> tuple[str, str]:
    """``(text, encoding label)``: UTF-8 (with or without a BOM), else Windows-1252."""
    try:
        return data.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "Windows-1252"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "Windows-1252"


def _header_key(cell: str) -> str:
    return " ".join(cell.replace("﻿", "").strip().strip('"').casefold().split())


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def clean_text(value: str | None) -> str | None:
    """Unicode NFC, whitespace runs collapsed (Amex pads descriptions); ``None`` when blank."""
    if value is None:
        return None
    text = " ".join(_nfc(value).split())
    return text or None


def clean_lines(value: str | None) -> str | None:
    """Like :func:`clean_text` but keeps one line per line of a multi-line cell."""
    if value is None:
        return None
    lines = [" ".join(line.split()) for line in _nfc(value).splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def parse_date(text: str) -> dt.date:
    """``M/D/YYYY`` (also zero-padded), ``MM/DD/YY`` or ISO; a trailing weekday is ignored."""
    cleaned = _WEEKDAY.sub("", text.strip())
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


def parse_amount(text: str) -> Decimal:
    """An exact amount: ``$``, ``,`` and spaces stripped, ``(x)`` = ``-x``, 0–2 decimals.

    Raises ``ValueError("decimals")`` for sub-cent digits (never rounded away) and
    ``ValueError("number")`` for anything that isn't a number.
    """
    cleaned = (
        text.strip()
        .replace(" ", "")
        .replace(" ", "")
        .replace("$", "")
        .replace(",", "")
        .replace("−", "-")
    )
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative, cleaned = True, cleaned[1:-1]
    if not _NUMBER.match(cleaned):
        raise ValueError("number")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - the pattern already checked it
        raise ValueError("number") from exc
    if negative:
        value = -value
    if value.quantize(CENT) != value:
        raise ValueError("decimals")
    try:
        return to_money(value)
    except ValueError as exc:
        raise ValueError("range") from exc


def clean_reference(text: str) -> str:
    """The Reference as text: surrounding spaces and apostrophes (and Excel's ``="…"``) removed."""
    ref = text.strip()
    if ref.startswith('="') and ref.endswith('"'):
        ref = ref[2:-1]
    return ref.strip().strip("'").strip()


def parse_file(data: bytes) -> ParsedFile:
    """Parse an extended Amex CSV download; raises :class:`ImportRefused` for unusable files."""
    if not data or not data.strip():
        raise ImportRefused("empty", "The file is empty.")
    text, encoding = decode(data)
    records: list[list[str]] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        for record in reader:
            records.append(record)
    except csv.Error as exc:
        raise ImportRefused(
            "damaged",
            f"The file can’t be read as CSV near row {len(records) + 1}. Download it again and "
            "import it without opening it in a spreadsheet app.",
        ) from exc
    header_index, columns = _find_header(records)
    found = set(columns)
    if "reference" not in found:
        if found <= _BASIC_FIELDS:
            raise ImportRefused("basic", BASIC_FILE)
        raise ImportRefused("no_reference", NO_REFERENCE)
    if "description" not in found and "statement_as" not in found:
        raise ImportRefused(
            "no_description", "This file has no Description column, so it can’t be imported."
        )
    width = len(records[header_index])
    rows: list[ParsedRow] = []
    errors: list[RowError] = []
    seen: dict[str, int] = {}
    data_rows = 0
    for index in range(header_index + 1, len(records)):
        record = records[index]
        number = index + 1
        if not any(cell.strip() for cell in record):
            continue
        data_rows += 1
        if any(_SWALLOWED_ROW.search(cell) for cell in record):
            raise ImportRefused(
                "damaged",
                f"Row {number} has a quote that is never closed, so the rows after it can’t be "
                "read reliably. Download the file again and import it without editing it.",
            )
        result = _parse_row(record, number, columns, width, seen)
        if isinstance(result, RowError):
            errors.append(result)
        else:
            seen[result.reference] = number
            rows.append(result)
    present = tuple(name for name in COLUMNS if name in found)
    return ParsedFile(encoding, present, tuple(rows), tuple(errors), data_rows)


def _find_header(records: list[list[str]]) -> tuple[int, dict[str, int]]:
    for index, record in enumerate(records[:HEADER_SCAN_ROWS]):
        keys = [_header_key(cell) for cell in record]
        if "date" in keys and "amount" in keys:
            columns: dict[str, int] = {}
            for position, key in enumerate(keys):
                name = _ALIASES.get(key)
                if name is not None and name not in columns:
                    columns[name] = position
            return index, columns
    raise ImportRefused(
        "not_amex",
        "This doesn’t look like an Amex CSV download: there is no header row with Date and "
        "Amount columns.",
    )


def _parse_row(
    record: list[str], number: int, columns: dict[str, int], width: int, seen: dict[str, int]
) -> ParsedRow | RowError:
    def cell(name: str) -> str:
        position = columns.get(name)
        if position is None or position >= len(record):
            return ""
        return record[position]

    if any(c.strip() for c in record[width:]):
        return RowError(number, "This row has more values than the header has columns.")
    raw_date = cell("date").strip()
    if not raw_date:
        return RowError(number, "The date is missing.")
    try:
        date = parse_date(raw_date)
    except ValueError:
        return RowError(number, f"“{raw_date}” isn’t a date (expected M/D/YYYY).")
    if date > clock.today() + FUTURE_TOLERANCE:
        return RowError(number, f"The date {date.isoformat()} is in the future.")
    raw_amount = cell("amount").strip()
    if not raw_amount:
        return RowError(number, "The amount is missing.")
    try:
        amount = parse_amount(raw_amount)
    except ValueError as exc:
        reason = {
            "decimals": f"The amount “{raw_amount}” has more than two decimals.",
            "range": f"The amount “{raw_amount}” is too large.",
        }.get(str(exc), f"The amount “{raw_amount}” isn’t a number.")
        return RowError(number, reason)
    reference = clean_reference(cell("reference"))
    if not reference:
        return RowError(number, "The Reference is missing, so duplicates can’t be detected.")
    if _SCIENTIFIC.match(reference):
        return RowError(
            number,
            f"The Reference “{reference}” was changed by a spreadsheet app. Download the file "
            "again and import it without opening it.",
        )
    if len(reference) > 64:
        return RowError(number, "The Reference is too long to be an Amex Reference.")
    if reference in seen:
        return RowError(number, f"The same Reference as row {seen[reference]}.")
    statement_as = clean_text(cell("statement_as"))
    name = clean_text(cell("description")) or statement_as
    if not name:
        return RowError(number, "The description is missing.")
    location = {
        key: value
        for key, value in (
            ("address", clean_lines(cell("address"))),
            ("city", clean_lines(cell("city_state"))),
            ("postal_code", clean_text(cell("zip"))),
            ("country", clean_text(cell("country"))),
            ("extended_details", clean_lines(cell("extended_details"))),
        )
        if value
    }
    return ParsedRow(
        row_number=number,
        reference=reference,
        date=date,
        posted_date=reference_posting_date(reference, date) or date,
        amount=amount,
        name=name,
        original_description=statement_as,
        external_category=clean_text(cell("category")),
        account_owner=clean_text(cell("card_member")),
        location=location or None,
    )


# --------------------------------------------------------------------------- import targets


@dataclass(frozen=True, slots=True)
class TargetOption:
    """One choice in the "Import into" picker: an account id as text, or ``"new"``."""

    value: str
    label: str
    linked: bool


@dataclass(frozen=True, slots=True)
class ImportTargets:
    options: tuple[TargetOption, ...]
    default: str | None
    #: True once a production Item exists: only linked accounts may be targeted.
    linked_only: bool


def linked_only(session: Session, *, production: bool) -> bool:
    """Once a production Item exists, CSV import may only target linked accounts (§6)."""
    return production and session.scalar(select(func.count()).select_from(PlaidItem)) > 0


def account_label(account: Account) -> str:
    """The account's name; a manual account's says it holds CSV rows only (once)."""
    if account.is_manual:
        name = account.name or MANUAL_ACCOUNT_NAME
        return name if "csv" in name.casefold() else f"{name} (CSV only)"
    return account.display_name


def import_targets(session: Session, *, production: bool = False) -> ImportTargets:
    """Accounts a file can go to. Default: the only linked credit-card account; else the only
    manual account; else a new manual account (created when the import is committed)."""
    only_linked = linked_only(session, production=production)
    accounts = session.scalars(
        select(Account).where(Account.is_hidden.is_(False)).order_by(Account.id)
    ).all()
    linked = [a for a in accounts if not a.is_manual]
    manual = [] if only_linked else [a for a in accounts if a.is_manual]
    options = [TargetOption(str(a.id), account_label(a), True) for a in linked]
    options += [TargetOption(str(a.id), account_label(a), False) for a in manual]
    if not only_linked and not manual:
        options.append(TargetOption(NEW_ACCOUNT, f"New account: {MANUAL_ACCOUNT_NAME}", False))
    credit = [a for a in linked if (a.type or "credit") == "credit"]
    default: str | None = None
    if len(credit) == 1:
        default = str(credit[0].id)
    elif not linked:
        default = str(manual[0].id) if len(manual) == 1 else (NEW_ACCOUNT if not manual else None)
    return ImportTargets(tuple(options), default, only_linked)


def parse_target(value: str | None) -> int | None:
    """The picker's value → an account id, or ``None`` for a new manual account."""
    text = (value or "").strip()
    if text == NEW_ACCOUNT:
        return None
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_ROW_ID:
        raise ImportRefused("account", "Choose the account to import into.")
    return int(text)


def _target(session: Session, account_id: int | None, *, production: bool) -> Account | None:
    """The account to import into (``None``: a new manual account); refuses what §6 forbids."""
    only_linked = linked_only(session, production=production)
    if account_id is None:
        if only_linked:
            raise ImportRefused(
                "account",
                "Your Amex card is linked, so import into the linked account (CSV history in a "
                "separate account would count twice).",
            )
        return None
    account = session.get(Account, account_id) if 1 <= account_id <= MAX_ROW_ID else None
    if account is None:
        raise ImportRefused(
            "account",
            "That account no longer exists (it may have been attached to your linked card). "
            "Choose the account again.",
        )
    if account.is_manual and only_linked:
        raise ImportRefused(
            "account",
            "Your Amex card is linked, so import into the linked account, not the CSV-only one.",
        )
    return account


# --------------------------------------------------------------------------- preview


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """A row shown in the preview, with what it was compared with."""

    row_number: int
    date: dt.date
    name: str
    original_description: str | None
    external_category: str | None
    account_owner: str | None
    amount: Decimal
    #: duplicate: the account it was imported into, when not the chosen one
    other_account: str | None = None
    #: changed: what is stored under this Reference
    earlier_date: dt.date | None = None
    earlier_amount: Decimal | None = None
    #: matched: the Plaid transaction it pairs with; held: the charge Plaid still shows as
    #: pending (``plaid_pending``), which it pairs with once that posts
    plaid_name: str | None = None
    plaid_date: dt.date | None = None
    plaid_pending: bool = False


@dataclass(frozen=True, slots=True)
class PreviewGroup:
    """``key``: ``new``, ``suspect``, ``matched``, ``duplicate`` or ``changed``."""

    key: str
    count: int
    sample: tuple[PreviewRow, ...]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """What importing a file would do. Nothing is written until :func:`commit`.

    Counts (disjoint): ``new`` rows will be added and counted; ``suspects`` will be added but held
    for review (inside Plaid's history without a matching Plaid charge, excluded from totals until
    the user keeps them), ``suspects_pending`` of them because Plaid still shows the charge as
    pending (they merge on their own once it posts); ``matched`` pair with a Plaid transaction
    already stored (merged into it, never stored twice); ``duplicates`` were imported before;
    ``changed`` have a Reference imported before with a different date or amount (skipped, shown
    as a warning); ``errors`` can't be read.
    """

    token: str
    filename: str
    file_sha256: str
    account_id: int | None
    account_label: str
    new_account: bool
    linked: bool
    encoding: str
    created_at: dt.datetime
    expires_at: dt.datetime
    row_count: int
    parsed: int
    new: int
    suspects: int
    suspects_pending: int
    matched: int
    duplicates: int
    changed: int
    errors: tuple[RowError, ...]
    date_min: dt.date | None
    date_max: dt.date | None
    groups: tuple[PreviewGroup, ...]
    same_file_imported_at: dt.datetime | None
    reference_dates: int

    @property
    def to_add(self) -> int:
        return self.new + self.suspects

    @property
    def can_import(self) -> bool:
        return self.to_add + self.matched > 0

    def group(self, key: str) -> PreviewGroup | None:
        return next((g for g in self.groups if g.key == key), None)


@dataclass
class _Stored:
    preview: ImportPreview
    rows: tuple[ParsedRow, ...]
    #: the ``csv_imports`` row this preview was imported as (kept so a repeated commit, e.g. a
    #: double-clicked Import button, answers with that import instead of importing again), and
    #: how many of its rows were skipped as changed since an earlier import
    import_id: int | None = None
    changed_count: int = 0


_PREVIEWS: dict[str, _Stored] = {}
_LOCK = threading.Lock()


def _purge(now: dt.datetime) -> None:
    for token in [t for t, s in _PREVIEWS.items() if s.preview.expires_at <= now]:
        del _PREVIEWS[token]
    while len(_PREVIEWS) >= MAX_PREVIEWS:
        oldest = min(_PREVIEWS, key=lambda t: _PREVIEWS[t].preview.created_at)
        del _PREVIEWS[oldest]


def _remember(preview: ImportPreview, rows: tuple[ParsedRow, ...]) -> None:
    with _LOCK:
        _purge(clock.now())
        _PREVIEWS[preview.token] = _Stored(preview, rows)


def _stored(token: str | None) -> _Stored | None:
    if not token:
        return None
    with _LOCK:
        stored = _PREVIEWS.get(token)
        if stored is None:
            return None
        if stored.preview.expires_at <= clock.now():
            del _PREVIEWS[token]
            return None
        return stored


def get_preview(token: str | None) -> ImportPreview | None:
    """The preview stored under ``token``, or ``None`` when unknown, expired (30 minutes) or
    already imported."""
    stored = _stored(token)
    return stored.preview if stored is not None and stored.import_id is None else None


def discard_preview(token: str | None) -> bool:
    """Forget a preview (after importing, or when the user cancels)."""
    with _LOCK:
        return _PREVIEWS.pop(token or "", None) is not None


def clean_filename(filename: str | None) -> str:
    """The file's base name, without control characters, at most 200 characters."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = " ".join(_UNSAFE_NAME.sub(" ", name).split())
    if len(name) > 200:
        name = name[:199] + "…"
    return name or "upload.csv"


def _chunks[T](items: Sequence[T], size: int = _CHUNK) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _preview_row(row: ParsedRow, **extra: Any) -> PreviewRow:
    return PreviewRow(
        row_number=row.row_number,
        date=row.date,
        name=row.name,
        original_description=row.original_description,
        external_category=row.external_category,
        account_owner=row.account_owner,
        amount=row.amount,
        **extra,
    )


def _same_as_stored(row: ParsedRow, stored: Any) -> bool:
    """Is the stored row with this Reference the same charge (same amount and date)?

    A CSV row keeps the file's date. A Plaid row that paired with it only had to accept the CSV
    row's dates (``reconcile.PlaidSide.accepts``).
    """
    if stored.amount != row.amount:
        return False
    if stored.source == "csv":
        return stored.date == row.date
    plaid = reconcile.PlaidSide(
        id=0,
        account_id=0,
        date=stored.date,
        authorized_date=stored.authorized_date,
        amount=stored.amount,
        texts=(),
    )
    return plaid.accepts(row.date, reference_posting_date(row.reference, row.date))


def preview(
    session: Session,
    file_bytes: bytes,
    filename: str | None,
    account_id: int | None,
    *,
    production: bool = False,
) -> ImportPreview:
    """Parse ``file_bytes`` and classify every row against the database; writes nothing.

    ``account_id`` is the target account, or ``None`` for a new manual account
    ("American Express (CSV)", created when the import is committed). ``production``: the app's
    environment, for the rule that a production Item allows only linked targets. Raises
    :class:`ImportRefused`. The preview is kept in memory for 30 minutes under ``.token``.
    """
    name = clean_filename(filename)
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ImportRefused(
            "too_large",
            f"The file is larger than {MAX_FILE_BYTES // 2**20} MB; an Amex download of two years "
            "is usually under 2 MB. Check that you chose the CSV file.",
        )
    account = _target(session, account_id, production=production)
    parsed = parse_file(file_bytes)
    if not parsed.rows and not parsed.errors:
        raise ImportRefused("no_rows", "The file has a header row but no transactions.")
    digest = hashlib.sha256(file_bytes).hexdigest()

    stored = _stored_by_reference(session, [r.reference for r in parsed.rows])
    labels: dict[int, str] = {}

    def label_of(stored_account_id: int) -> str:
        if stored_account_id not in labels:
            other = session.get(Account, stored_account_id)
            labels[stored_account_id] = account_label(other) if other else "another account"
        return labels[stored_account_id]

    fresh: list[ParsedRow] = []
    duplicates: list[PreviewRow] = []
    changed: list[PreviewRow] = []
    for row in parsed.rows:
        existing = stored.get(row.reference)
        if existing is None:
            fresh.append(row)
        elif _same_as_stored(row, existing):
            other = None
            if account is None or existing.account_id != account.id:
                other = label_of(existing.account_id)
            duplicates.append(_preview_row(row, other_account=other))
        else:
            changed.append(
                _preview_row(row, earlier_date=existing.date, earlier_amount=existing.amount)
            )

    matched: dict[int, reconcile.PlaidSide] = {}
    suspect_indexes: set[int] = set()
    pending: dict[int, reconcile.PlaidSide] = {}
    if account is not None and account.item_id is not None:
        matched, suspect_indexes, pending = _simulate(session, account, fresh)
    new_rows = [
        _preview_row(row)
        for i, row in enumerate(fresh)
        if i not in matched and i not in suspect_indexes
    ]
    suspect_rows = [
        _preview_row(
            row,
            plaid_name=(pending[i].label or None) if i in pending else None,
            plaid_date=pending[i].spend_date if i in pending else None,
            plaid_pending=i in pending,
        )
        for i, row in enumerate(fresh)
        if i in suspect_indexes
    ]
    matched_rows = [
        _preview_row(fresh[i], plaid_name=plaid.label or None, plaid_date=plaid.spend_date)
        for i, plaid in sorted(matched.items())
    ]
    groups = tuple(
        PreviewGroup(key, len(rows), tuple(rows[:SAMPLE_SIZE]))
        for key, rows in (
            ("new", new_rows),
            ("suspect", suspect_rows),
            ("matched", matched_rows),
            ("duplicate", duplicates),
            ("changed", changed),
        )
    )
    dates = [r.date for r in parsed.rows]
    same_file = session.scalar(
        select(func.max(CsvImport.imported_at)).where(CsvImport.file_sha256 == digest)
    )
    now = clock.now()
    result = ImportPreview(
        token=token_urlsafe(24),
        filename=name,
        file_sha256=digest,
        account_id=account.id if account is not None else None,
        account_label=account_label(account) if account is not None else MANUAL_ACCOUNT_NAME,
        new_account=account is None,
        linked=account is not None and account.item_id is not None,
        encoding=parsed.encoding,
        created_at=now,
        expires_at=now + PREVIEW_TTL,
        row_count=parsed.data_rows,
        parsed=len(parsed.rows),
        new=len(new_rows),
        suspects=len(suspect_rows),
        suspects_pending=sum(1 for row in suspect_rows if row.plaid_pending),
        matched=len(matched_rows),
        duplicates=len(duplicates),
        changed=len(changed),
        errors=parsed.errors,
        date_min=min(dates) if dates else None,
        date_max=max(dates) if dates else None,
        groups=groups,
        same_file_imported_at=same_file,
        reference_dates=sum(
            1 for r in parsed.rows if reference_posting_date(r.reference, r.date) is not None
        ),
    )
    _remember(result, parsed.rows)
    log.info(
        "CSV preview: %d rows (%d new, %d held, %d matched, %d duplicates, %d changed, %d errors)",
        result.row_count,
        result.new,
        result.suspects,
        result.matched,
        result.duplicates,
        result.changed,
        len(result.errors),
    )
    return result


def _stored_by_reference(session: Session, references: Sequence[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for chunk in _chunks(list(references)):
        for stored in session.execute(
            select(
                Transaction.csv_reference,
                Transaction.account_id,
                Transaction.source,
                Transaction.date,
                Transaction.authorized_date,
                Transaction.amount,
            ).where(Transaction.csv_reference.in_(chunk))
        ):
            found[stored.csv_reference] = stored
    return found


def _simulate(
    session: Session, account: Account, fresh: Sequence[ParsedRow]
) -> tuple[dict[int, reconcile.PlaidSide], set[int], dict[int, reconcile.PlaidSide]]:
    """What :func:`reconcile.reconcile_item` would do with these rows added to ``account``.

    Returns ``{index in fresh: the Plaid row it pairs with}``, the indexes the cutover would hold
    for review, and ``{index: pending Plaid row}`` for held rows whose charge Plaid still shows
    as pending (``reconcile.pending_twins``: they pair once it posts).
    """
    assert account.item_id is not None
    account_ids = reconcile.item_account_ids(session, account.item_id)
    plaid_rows = reconcile.plaid_candidates(session, account_ids)
    pending_rows = reconcile.pending_candidates(session, account_ids)
    new_sides = [
        CsvSide(
            key=(1, i),
            account_id=account.id,
            date=row.date,
            amount=row.amount,
            texts=(row.original_description or "", row.name),
            posted=reference_posting_date(row.reference, row.date),
        )
        for i, row in enumerate(fresh)
    ]
    since = reconcile.earliest_window_start([*plaid_rows, *pending_rows])
    stored_sides = (
        reconcile.csv_candidates(session, account_ids, since=since) if since is not None else []
    )
    matched: dict[int, reconcile.PlaidSide] = {}
    paired: set[tuple[int, int]] = set()
    for csv_side, plaid in reconcile.match([*stored_sides, *new_sides], plaid_rows):
        paired.add(csv_side.key)
        if csv_side.key[0] == 1:
            matched[csv_side.key[1]] = plaid
    suspects: set[int] = set()
    item = session.get(PlaidItem, account.item_id)
    if item is not None and item.history_complete_at is not None:
        start = account.plaid_history_start or reconcile.history_start(session, account.id)
        if start is not None:
            suspects = {i for i, row in enumerate(fresh) if i not in matched and row.date >= start}
    pending: dict[int, reconcile.PlaidSide] = {}
    if suspects and pending_rows:
        unpaired = [s for s in (*stored_sides, *new_sides) if s.key not in paired]
        twins = {row.key: plaid for row, plaid in reconcile.match(unpaired, pending_rows)}
        pending = {i: twins[(1, i)] for i in sorted(suspects) if (1, i) in twins}
    return matched, suspects, pending


# --------------------------------------------------------------------------- commit


@dataclass(frozen=True, slots=True)
class _RowLike:
    """The fields ``Categorizer.decide`` reads, for a row not inserted yet."""

    account_id: int
    amount: Decimal
    name: str
    original_description: str | None
    external_category: str | None
    id: int | None = None
    source: str = "csv"
    merchant_name: str | None = None
    transaction_code: str | None = None
    pfc_primary: str | None = None
    pfc_detailed: str | None = None


def _row_values(
    row: ParsedRow, account_id: int, import_id: int, categorizer: Categorizer
) -> dict[str, Any]:
    decision = categorizer.decide(
        _RowLike(
            account_id=account_id,
            amount=row.amount,
            name=row.name,
            original_description=row.original_description,
            external_category=row.external_category,
        )
    )
    values: dict[str, Any] = {
        "account_id": account_id,
        "source": "csv",
        "pending": False,
        "date": row.date,
        "authorized_date": None,
        "posted_date": row.posted_date,
        "name": row.name,
        "merchant_name": None,
        "original_description": row.original_description,
        "amount": row.amount,
        "iso_currency_code": "USD",
        "external_category": row.external_category,
        "account_owner": row.account_owner,
        "csv_reference": row.reference,
        "csv_import_id": import_id,
        "duplicate_status": "none",
        "category_id": decision.category_id,
        "category_source": decision.source,
        "rule_id": decision.rule_id if decision.source == "rule" else None,
        "tags": [],
        "needs_review": False,
    }
    if row.location is not None:  # left out otherwise: SQL NULL, not a JSON null
        values["location"] = row.location
    return values


def _insert(session: Session, values: list[dict[str, Any]]) -> dict[str, int]:
    """Insert rows, skipping References that exist; returns ``{reference: new id}``."""
    table = Transaction.__table__
    stmt = (
        pg_insert(table)
        .on_conflict_do_nothing(index_elements=[table.c.csv_reference])
        .returning(table.c.id, table.c.csv_reference)
    )
    inserted: dict[str, int] = {}
    for with_location in (True, False):
        group = [v for v in values if ("location" in v) is with_location]
        for chunk in _chunks(group, 500):
            for new_id, reference in session.execute(stmt, list(chunk)):
                inserted[reference] = new_id
    return inserted


def _manual_account(session: Session) -> Account:
    """The manual account a new import goes to: the existing one, else a new one."""
    existing = session.scalars(
        select(Account).where(Account.item_id.is_(None)).order_by(Account.id).limit(1)
    ).first()
    if existing is not None:
        return existing
    account = Account(
        name=MANUAL_ACCOUNT_NAME,
        type="credit",
        subtype="credit card",
        iso_currency_code="USD",
    )
    session.add(account)
    session.flush()
    return account


@dataclass(frozen=True, slots=True)
class CsvImportOutcome(ImportOutcome):
    """``events.ImportOutcome`` plus ``changed_count``: rows skipped because their Reference was
    imported before with a different date or amount. They count in ``error_count`` (rows skipped
    that need checking; ``csv_imports`` stores no separate column), never in ``duplicate_count``
    (rows imported before, unchanged)."""

    changed_count: int = 0

    @property
    def unreadable_count(self) -> int:
        """Rows skipped because they couldn't be read (``error_count`` without the changed)."""
        return self.error_count - self.changed_count


def _outcome(
    row: CsvImport, *, inserted_ids: tuple[int, ...] = (), changed_count: int = 0
) -> CsvImportOutcome:
    return CsvImportOutcome(
        account_id=row.account_id,
        filename=row.filename,
        csv_import_id=row.id,
        row_count=row.row_count,
        inserted_count=row.inserted_count,
        duplicate_count=row.duplicate_count,
        matched_plaid_count=row.matched_plaid_count,
        error_count=row.error_count,
        inserted_transaction_ids=inserted_ids,
        date_min=row.date_min,
        date_max=row.date_max,
        changed_count=changed_count,
    )


def _count_changed(session: Session, rows: Sequence[ParsedRow]) -> int:
    """How many of ``rows`` (each Reference is stored already) differ from the stored charge."""
    stored = _stored_by_reference(session, [row.reference for row in rows])
    return sum(
        1
        for row in rows
        if (existing := stored.get(row.reference)) is not None
        and not _same_as_stored(row, existing)
    )


def commit(
    session: Session,
    preview_token: str,
    *,
    notifier: Notifier | None = None,
    production: bool = False,
) -> CsvImportOutcome:
    """Import the rows of a preview; returns what happened (the caller commits).

    Takes the ledger lock, creates the manual account when the preview asked for a new one,
    inserts with ``ON CONFLICT (csv_reference) DO NOTHING``, categorizes (rules, then the Amex
    mapping), records ``csv_imports``, reconciles with Plaid (rows that pair are merged into the
    Plaid transaction and count as ``matched_plaid_count``, not as inserted) and calls
    ``alerts.evaluate_after_import``. Raises :class:`ImportRefused` (``expired``, ``account``).
    A row whose Reference is stored already counts in ``duplicate_count`` when it is the same
    charge, else (changed since an earlier import) in ``error_count`` and ``changed_count``.

    A preview is imported once. Committing it again (a double-clicked Import button sends two
    requests) waits for the first import on the ledger lock and then returns that import's
    outcome without importing anything or evaluating alerts again; only when the first attempt
    was rolled back does it import.
    """
    expired = ImportRefused(
        "expired",
        "That preview has expired (previews last 30 minutes) or was already imported. Upload "
        "the file again.",
    )
    if _stored(preview_token) is None:
        raise expired
    ledger_lock(session)  # first, and it serializes two commits of the same preview
    stored = _stored(preview_token)
    if stored is None:
        raise expired
    if stored.import_id is not None:
        earlier = session.get(CsvImport, stored.import_id)
        if earlier is not None:
            return _outcome(earlier, changed_count=stored.changed_count)
    preview_ = stored.preview
    account = _target(session, preview_.account_id, production=production)
    if account is None:
        account = _manual_account(session)
    csv_import = CsvImport(
        filename=preview_.filename,
        file_sha256=preview_.file_sha256,
        account_id=account.id,
        imported_at=clock.now(),
        row_count=preview_.row_count,
        error_count=len(preview_.errors),
        date_min=preview_.date_min,
        date_max=preview_.date_max,
    )
    session.add(csv_import)
    session.flush()

    categorizer = Categorizer.load(session)
    values = [_row_values(row, account.id, csv_import.id, categorizer) for row in stored.rows]
    inserted = _insert(session, values)
    # classified before reconciling, while each skipped Reference is still on its stored row
    changed = _count_changed(session, [r for r in stored.rows if r.reference not in inserted])

    result = ReconcileResult()
    if account.item_id is not None:
        result = reconcile.reconcile_item(session, account.item_id)
    new_ids = set(inserted.values())
    paired = {csv_id for csv_id, _ in result.pairs if csv_id in new_ids}
    kept = sorted(new_ids - paired)
    csv_import.inserted_count = len(kept)
    csv_import.matched_plaid_count = len(paired)
    csv_import.duplicate_count = len(stored.rows) - len(inserted) - changed
    csv_import.error_count = len(preview_.errors) + changed
    session.flush()

    outcome = _outcome(csv_import, inserted_ids=tuple(kept), changed_count=changed)
    alerts.evaluate_after_import(session, notifier or NullNotifier(), outcome)
    with _LOCK:
        stored.import_id = csv_import.id
        stored.changed_count = changed
    log.info(
        "CSV import %d: %d added, %d matched Plaid, %d duplicates, %d changed, %d errors",
        csv_import.id,
        outcome.inserted_count,
        outcome.matched_plaid_count,
        outcome.duplicate_count,
        changed,
        outcome.unreadable_count,
    )
    return outcome


# --------------------------------------------------------------------------- history and review


@dataclass(frozen=True, slots=True)
class ReferenceDates:
    """How often a Reference's embedded posting date equals Plaid's posting date (paired rows)."""

    compared: int = 0
    matched: int = 0

    @property
    def percent(self) -> Decimal | None:
        if not self.compared:
            return None
        return Decimal(self.matched * 100) / Decimal(self.compared)


def reference_dates(session: Session, *, csv_import_id: int | None = None) -> ReferenceDates:
    """Check the Reference posting dates against the Plaid rows they paired with.

    The CSV date is gone once a row has paired, so plausibility is judged from the Plaid row's
    spend date (the CSV date was at most 3 days before it, or 30 without an authorized date).
    """
    query = select(
        Transaction.csv_reference,
        Transaction.authorized_date,
        Transaction.date,
        Transaction.posted_date,
    ).where(Transaction.source == "plaid", Transaction.csv_reference.is_not(None))
    if csv_import_id is not None:
        query = query.where(Transaction.csv_import_id == csv_import_id)
    compared = matched = 0
    for reference, authorized, date, posted in session.execute(query):
        lower = (
            authorized - reconcile.WINDOW_BEFORE_AUTHORIZED
            if authorized
            else date - reconcile.WINDOW_BEFORE_POSTED
        )
        derived = reference_posting_date(reference, lower)
        if derived is None:
            continue
        compared += 1
        matched += derived == (posted or date)
    return ReferenceDates(compared, matched)


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One past import, for the history table and the summary after an import.

    ``error_count`` counts every row skipped as needing a check: rows that couldn't be read and
    rows whose Reference was imported before with a different date or amount.
    """

    id: int
    filename: str
    account_label: str
    imported_at: dt.datetime
    row_count: int
    inserted_count: int
    duplicate_count: int
    matched_plaid_count: int
    error_count: int
    date_min: dt.date | None
    date_max: dt.date | None
    #: rows of this import currently held for review
    suspects: int


def _record(session: Session, row: CsvImport, suspects: int) -> ImportRecord:
    account = session.get(Account, row.account_id)
    return ImportRecord(
        id=row.id,
        filename=row.filename,
        account_label=account_label(account) if account else "—",
        imported_at=row.imported_at,
        row_count=row.row_count,
        inserted_count=row.inserted_count,
        duplicate_count=row.duplicate_count,
        matched_plaid_count=row.matched_plaid_count,
        error_count=row.error_count,
        date_min=row.date_min,
        date_max=row.date_max,
        suspects=suspects,
    )


def _suspect_counts(session: Session, import_ids: Iterable[int]) -> dict[int, int]:
    ids = list(import_ids)
    if not ids:
        return {}
    return dict(
        session.execute(
            select(Transaction.csv_import_id, func.count())
            .where(
                Transaction.csv_import_id.in_(ids),
                Transaction.duplicate_status == "suspect",
            )
            .group_by(Transaction.csv_import_id)
        ).all()
    )


def import_history(session: Session, *, limit: int = 50) -> list[ImportRecord]:
    """Past imports, newest first."""
    rows = session.scalars(
        select(CsvImport).order_by(CsvImport.imported_at.desc(), CsvImport.id.desc()).limit(limit)
    ).all()
    counts = _suspect_counts(session, (r.id for r in rows))
    return [_record(session, r, counts.get(r.id, 0)) for r in rows]


def import_record(session: Session, csv_import_id: int) -> ImportRecord | None:
    row = session.get(CsvImport, csv_import_id)
    if row is None:
        return None
    return _record(session, row, _suspect_counts(session, [row.id]).get(row.id, 0))


@dataclass(frozen=True, slots=True)
class SuspectRow:
    """An unpaired CSV row inside Plaid's history: out of every total until the user decides.

    ``pending_name``/``pending_date``: Plaid still shows this charge as pending; the row merges
    with it on its own once it posts (keeping the row now would count the charge twice).
    """

    id: int
    account_label: str
    date: dt.date
    posted_date: dt.date | None
    name: str
    original_description: str | None
    external_category: str | None
    account_owner: str | None
    amount: Decimal
    filename: str | None
    pending_name: str | None = None
    pending_date: dt.date | None = None

    @property
    def pending(self) -> bool:
        return self.pending_date is not None


def list_suspects(session: Session) -> list[SuspectRow]:
    rows = session.execute(
        select(Transaction, CsvImport.filename)
        .outerjoin(CsvImport, CsvImport.id == Transaction.csv_import_id)
        .where(Transaction.source == "csv", Transaction.duplicate_status == "suspect")
        .order_by(Transaction.date, Transaction.id)
    ).all()
    accounts: dict[int, Account | None] = {}
    for txn, _ in rows:
        if txn.account_id not in accounts:
            accounts[txn.account_id] = session.get(Account, txn.account_id)
    items = {a.item_id for a in accounts.values() if a is not None and a.item_id is not None}
    pending: dict[int, reconcile.PlaidSide] = {}
    for item_id in sorted(items):
        pending.update(
            reconcile.pending_twins(session, reconcile.item_account_ids(session, item_id))
        )
    out = []
    for txn, filename in rows:
        account = accounts[txn.account_id]
        name = clean_text(txn.name) or txn.name
        statement_as = clean_text(txn.original_description)
        twin = pending.get(txn.id)
        out.append(
            SuspectRow(
                id=txn.id,
                account_label=account_label(account) if account else "—",
                date=txn.date,
                posted_date=txn.posted_date,
                name=name,
                original_description=statement_as if statement_as != name else None,
                external_category=txn.external_category,
                account_owner=txn.account_owner,
                amount=txn.amount,
                filename=filename,
                pending_name=(twin.label or None) if twin else None,
                pending_date=twin.spend_date if twin else None,
            )
        )
    return out


def _suspect(session: Session, transaction_id: int) -> Transaction | None:
    txn = session.get(Transaction, transaction_id)
    if txn is None or txn.source != "csv" or txn.duplicate_status != "suspect":
        return None
    return txn


def keep_suspect(session: Session, transaction_id: int) -> bool:
    """The user says the charge is real: ``kept`` (counted, never flagged again). False if the
    row is not a suspect (any more)."""
    ledger_lock(session)
    txn = _suspect(session, transaction_id)
    if txn is None:
        return False
    txn.duplicate_status = "kept"
    session.flush()
    return True


def delete_suspect(session: Session, transaction_id: int) -> bool:
    """The user says the charge is a duplicate of one Plaid has: delete the CSV row. False if the
    row is not a suspect (any more).

    Its Reference goes with it: a later import of a file holding the row adds it again, held for
    review again (out of every total), so a mistaken Delete can be undone by importing the file.
    """
    ledger_lock(session)
    txn = _suspect(session, transaction_id)
    if txn is None:
        return False
    session.delete(txn)
    session.flush()
    return True


@dataclass(frozen=True, slots=True)
class AttachOffer:
    """A manual (CSV-only) account that can be attached to a linked account."""

    manual_id: int
    manual_label: str
    transaction_count: int
    targets: tuple[tuple[int, str], ...]
    default_target: int | None


def attach_offers(session: Session) -> list[AttachOffer]:
    """Manual accounts and the linked accounts they can be attached to (none without both)."""
    accounts = session.scalars(select(Account).order_by(Account.id)).all()
    linked = [a for a in accounts if not a.is_manual]
    manual = [a for a in accounts if a.is_manual]
    if not linked or not manual:
        return []
    visible = [a for a in linked if not a.is_hidden] or linked
    credit = [a for a in visible if (a.type or "credit") == "credit"]
    default = credit[0].id if len(credit) == 1 else (visible[0].id if len(visible) == 1 else None)
    counts = dict(
        session.execute(
            select(Transaction.account_id, func.count())
            .where(Transaction.account_id.in_([a.id for a in manual]))
            .group_by(Transaction.account_id)
        ).all()
    )
    targets = tuple((a.id, account_label(a)) for a in visible)
    return [
        AttachOffer(m.id, account_label(m), counts.get(m.id, 0), targets, default) for m in manual
    ]
