"""Amex CSV import (SYNC-8): parsing, preview, commit, duplicates, categorization, review.

The builders at the top (``amex_csv``, ``amex_row``, ``ref``) are shared with the reconciliation,
command and web tests of this package. Every fixture and every value here is synthetic.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text

from budgetapp import clock
from budgetapp.db import LEDGER_KEY, LOCK_NS_GLOBAL
from budgetapp.models import Account, CsvImport, Transaction, v_allocations
from budgetapp.services import alerts, csv_import, reconcile
from budgetapp.services.csv_import import ImportRefused
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_item,
    make_rule,
    make_transaction,
    uncategorized,
)

FIXTURES = Path(__file__).parent / "fixtures" / "amex"
HEADER = [
    "Date",
    "Description",
    "Card Member",
    "Account #",
    "Amount",
    "Extended Details",
    "Appears On Your Statement As",
    "Address",
    "City/State",
    "Zip Code",
    "Country",
    "Reference",
    "Category",
]
_SEQ = iter(range(1, 1_000_000))


def ref(posted: dt.date | str, n: int | None = None) -> str:
    """A synthetic 18-digit Amex Reference whose ``[1:8]`` is the posting date (YYYYDDD)."""
    if isinstance(posted, str):
        posted = dt.date.fromisoformat(posted)
    number = next(_SEQ) if n is None else n
    return f"3{posted.year:04d}{posted.timetuple().tm_yday:03d}{number:010d}"


def amex_row(
    date: dt.date | str,
    description: str,
    amount: str,
    *,
    reference: str | None = None,
    category: str = "Restaurant-Restaurant",
    statement_as: str | None = None,
    member: str = "ALEX RIVERA",
    lag: int = 1,
    **extra: str,
) -> dict[str, str]:
    """One extended-CSV row; ``reference`` defaults to one posted ``lag`` days after ``date``."""
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    row = {
        "Date": f"{date.month}/{date.day}/{date.year}",
        "Description": description,
        "Card Member": member,
        "Account #": "-91005",
        "Amount": amount,
        "Extended Details": "",
        "Appears On Your Statement As": statement_as if statement_as is not None else description,
        "Address": "",
        "City/State": "",
        "Zip Code": "",
        "Country": "",
        "Reference": f"'{reference or ref(date + dt.timedelta(days=lag))}'",
        "Category": category,
    }
    row.update(extra)
    return row


def amex_csv(*rows: dict[str, str], header: list[str] | None = None, bom: bool = False) -> bytes:
    """An extended Amex CSV (CRLF, UTF-8) with ``rows``."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=header or HEADER, lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    data = buffer.getvalue().encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run_import(
    db,
    data: bytes,
    account: Account | None,
    *,
    filename: str = "activity.csv",
    production: bool = False,
    **kwargs: Any,
):
    """Preview then commit; returns ``(preview, outcome)``."""
    account_id = account.id if account is not None else None
    preview = csv_import.preview(db, data, filename, account_id, production=production)
    outcome = csv_import.commit(db, preview.token, production=production, **kwargs)
    return preview, outcome


def ledger_locks_held(db) -> int:
    """Ledger locks held by this session's own backend (``db.ledger_lock``)."""
    return db.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
            "AND classid = :ns AND objid = :key AND objsubid = 2"
        ),
        {"ns": LOCK_NS_GLOBAL, "key": LEDGER_KEY},
    )


def csv_rows(db, account: Account | None = None) -> list[Transaction]:
    query = select(Transaction).where(Transaction.source == "csv").order_by(Transaction.id)
    if account is not None:
        query = query.where(Transaction.account_id == account.id)
    return list(db.scalars(query))


@pytest.fixture(autouse=True)
def _forget_previews():
    yield
    csv_import._PREVIEWS.clear()


# --------------------------------------------------------------------------- parsing


class TestParsing:
    def test_the_extended_download_parses_every_row(self):
        parsed = csv_import.parse_file(fixture("activity_2025-08.csv"))
        assert parsed.encoding == "UTF-8"  # the BOM is not part of the first header
        assert parsed.errors == ()
        assert parsed.data_rows == len(parsed.rows) == 15
        first = parsed.rows[0]
        assert first.row_number == 2
        assert first.date == dt.date(2025, 8, 1)
        assert first.amount == Decimal("6.50")  # "6.5": one decimal
        assert first.reference == "320252140000000001"  # apostrophes stripped, kept as text
        assert first.posted_date == dt.date(2025, 8, 2)  # day 214 of 2025
        assert first.name == "BLUE BOTTLE COFFEE SAN FRANCISCO CA"  # padding collapsed
        assert first.original_description == "BLUE BOTTLE COFFEE"
        assert first.external_category == "Restaurant-Bar & Café"
        assert first.account_owner == "ALEX RIVERA"
        assert first.location == {
            "address": "1 FERRY BUILDING\nSTE 7",  # a quoted multi-line cell
            "city": "SAN FRANCISCO\nCA",
            "postal_code": "94111",
            "country": "UNITED STATES",
            "extended_details": (
                "000000123456 94111\nBLUE BOTTLE COFFEE\n1 FERRY BUILDING\nSAN FRANCISCO\nCA"
            ),
        }
        by_row = {r.row_number: r for r in parsed.rows}
        assert by_row[4].amount == Decimal("-1245.18")  # "-1,245.18", Amex's sign = Plaid's
        assert by_row[4].external_category is None  # payments have no category
        assert by_row[4].location is None
        assert by_row[5].amount == Decimal("42.00")  # "42": no decimals
        assert by_row[5].account_owner == "SAM RIVERA"  # an additional card member
        assert by_row[12].amount == Decimal("-42.00")  # the refund

    def test_five_identical_same_day_charges_stay_five_rows(self):
        parsed = csv_import.parse_file(fixture("activity_2025-08.csv"))
        hotel = [r for r in parsed.rows if r.name.startswith("HILTON")]
        assert len(hotel) == 5
        assert {(r.date, r.amount) for r in hotel} == {(dt.date(2025, 8, 8), Decimal("189.00"))}
        assert len({r.reference for r in hotel}) == 5

    def test_windows_1252_files_decode(self):
        parsed = csv_import.parse_file(fixture("activity_cp1252.csv"))
        assert parsed.encoding == "Windows-1252"
        assert [r.name for r in parsed.rows] == [
            "CAFÉ REVEILLE SAN FRANCISCO CA",
            "CROISSANTERIE DU MARCHÉ SAN FRANCISCO CA",
        ]
        assert parsed.rows[0].external_category == "Restaurant-Bar & Café"
        assert parsed.rows[0].date == dt.date(2025, 7, 14)  # "07/14/25"
        assert parsed.rows[1].amount == Decimal("12.40")
        assert parsed.rows[0].reference == "320251960000000019"  # no apostrophes in this file

    def test_columns_are_found_by_name_in_any_order_with_extra_columns(self):
        header = ["Amount", "Foreign Spend", "Reference", "Description", "Date", "Category"]
        data = amex_csv(
            {
                "Date": "10/6/2025",
                "Description": "PHILZ COFFEE",
                "Amount": "5.25",
                "Foreign Spend": "ignored",
                "Reference": "'320252800000000001'",
                "Category": "Restaurant-Bar & Café",
            },
            header=header,
        )
        (row,) = csv_import.parse_file(data).rows
        assert (row.date, row.name, row.amount) == (
            dt.date(2025, 10, 6),
            "PHILZ COFFEE",
            Decimal("5.25"),
        )
        assert row.original_description is None and row.account_owner is None

    def test_title_rows_above_the_header_and_blank_rows_are_skipped(self):
        body = amex_csv(amex_row("2025-10-06", "PHILZ COFFEE", "5.25")).decode()
        data = ("Activity for ALEX RIVERA\r\n\r\n" + body + ",,,,,,,,,,,,\r\n").encode()
        parsed = csv_import.parse_file(data)
        assert len(parsed.rows) == 1 and parsed.rows[0].row_number == 4
        assert parsed.errors == () and parsed.data_rows == 1

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("10/6/2025", dt.date(2025, 10, 6)),
            ("10/06/2025", dt.date(2025, 10, 6)),
            ("10/06/25", dt.date(2025, 10, 6)),
            ("2/8/20", dt.date(2020, 2, 8)),
            ("2025-10-06", dt.date(2025, 10, 6)),
            ("06/01/2019  Sat", dt.date(2019, 6, 1)),
        ],
    )
    def test_dates(self, text, expected):
        assert csv_import.parse_date(text) == expected

    @pytest.mark.parametrize("text", ["13/45/2025", "2025/10/06", "yesterday", "10-06-2025"])
    def test_bad_dates(self, text):
        with pytest.raises(ValueError):
            csv_import.parse_date(text)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12.34", "12.34"),
            ("6.5", "6.50"),
            ("42", "42.00"),
            ("-42.00", "-42.00"),
            ("$1,234.50", "1234.50"),
            ("-$1,245.18", "-1245.18"),
            (" 1 234.50 ", "1234.50"),
            ("(4.20)", "-4.20"),
            ("12.340", "12.34"),
            ("0", "0.00"),
        ],
    )
    def test_amounts_are_exact(self, text, expected):
        assert csv_import.parse_amount(text) == Decimal(expected)

    @pytest.mark.parametrize(
        ("text", "kind"),
        [("12.345", "decimals"), ("abc", "number"), ("1e3", "number"), ("1" * 20, "range")],
    )
    def test_bad_amounts(self, text, kind):
        with pytest.raises(ValueError, match=kind):
            csv_import.parse_amount(text)

    @pytest.mark.parametrize(
        "text", ["'320252140000000001'", "320252140000000001", " '320252140000000001' "]
    )
    def test_references_keep_every_digit(self, text):
        assert csv_import.clean_reference(text) == "320252140000000001"

    def test_excel_formula_references_are_unwrapped(self):
        assert csv_import.clean_reference('="320252140000000001"') == "320252140000000001"


class TestReferenceDates:
    @pytest.mark.parametrize(
        ("reference", "csv_date", "expected"),
        [
            ("320150170634561830", "2015-01-17", "2015-01-17"),  # RESEARCH T4.1b's example
            ("320150170634561830", "2015-01-14", "2015-01-17"),  # three days later
            ("320150170634561830", "2014-12-18", "2015-01-17"),  # 30 days later: still plausible
            ("320150170634561830", "2014-12-17", None),  # 31 days: not plausible
            ("320150170634561830", "2015-01-18", None),  # before the transaction date
            ("320243660000000001", "2024-12-31", "2024-12-31"),  # day 366 of a leap year
            ("320253660000000001", "2025-12-31", None),  # day 366 of 2025 doesn't exist
            ("320250000000000001", "2025-01-01", None),  # day 0
            ("3ABC", "2025-01-01", None),
            ("", "2025-01-01", None),
        ],
    )
    def test_reference_posting_date(self, reference, csv_date, expected):
        result = reconcile.reference_posting_date(reference, dt.date.fromisoformat(csv_date))
        assert result == (dt.date.fromisoformat(expected) if expected else None)

    def test_posted_date_falls_back_to_the_date(self):
        data = amex_csv(
            amex_row("2025-10-06", "PHILZ", "5.25", reference="320240010000000001"),
            amex_row("2025-10-06", "PHILZ", "5.25", reference="X-1"),
        )
        rows = csv_import.parse_file(data).rows
        assert [r.posted_date for r in rows] == [dt.date(2025, 10, 6)] * 2


class TestRefusedFiles:
    def test_the_basic_download_is_refused_with_instructions(self):
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(fixture("activity_basic.csv"))
        assert info.value.kind == "basic"
        assert "Include all additional transaction details" in info.value.message

    def test_a_file_without_a_reference_column_is_refused(self):
        header = [h for h in HEADER if h != "Reference"]
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(amex_csv(amex_row("2025-10-06", "PHILZ", "5.25"), header=header))
        assert info.value.kind == "no_reference"

    def test_a_file_without_a_description_is_refused(self):
        header = ["Date", "Amount", "Reference"]
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(amex_csv(amex_row("2025-10-06", "PHILZ", "5.25"), header=header))
        assert info.value.kind == "no_description"

    @pytest.mark.parametrize(
        "data", [b"name,email\r\nAlex,alex@example.com\r\n", b"%PDF-1.7\n\x00\x01\x02binary"]
    )
    def test_other_files_are_not_amex(self, data):
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(data)
        assert info.value.kind == "not_amex"

    @pytest.mark.parametrize("data", [b"", b"  \r\n"])
    def test_empty_files(self, data):
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(data)
        assert info.value.kind == "empty"

    def test_an_unclosed_quote_is_refused(self):
        with pytest.raises(ImportRefused) as info:
            csv_import.parse_file(fixture("activity_unclosed_quote.csv"))
        assert info.value.kind == "damaged"
        assert "Row 2" in info.value.message


class TestRowErrors:
    def test_malformed_rows_are_reported_with_their_row_numbers(self):
        parsed = csv_import.parse_file(fixture("activity_malformed.csv"))
        assert [r.row_number for r in parsed.rows] == [2, 11]
        errors = {e.row_number: e.reason for e in parsed.errors}
        assert errors == {
            3: "“13/45/2025” isn’t a date (expected M/D/YYYY).",
            4: "The amount “twelve” isn’t a number.",
            5: "The Reference is missing, so duplicates can’t be detected.",
            6: "The amount “12.345” has more than two decimals.",
            7: (
                "The Reference “3.20250000000001E+17” was changed by a spreadsheet app. "
                "Download the file again and import it without opening it."
            ),
            8: "The same Reference as row 2.",
            9: "The date is missing.",
            10: "The description is missing.",
            12: "This row has more values than the header has columns.",
        }
        assert parsed.data_rows == 11

    def test_a_future_date_is_an_error(self, freeze_today):
        freeze_today("2026-10-15")
        parsed = csv_import.parse_file(
            amex_csv(
                amex_row("2026-10-22", "SOON", "1.00"),
                amex_row("2026-10-23", "TOO SOON", "1.00"),
            )
        )
        assert [r.name for r in parsed.rows] == ["SOON"]
        assert parsed.errors[0].reason == "The date 2026-10-23 is in the future."

    def test_the_description_falls_back_to_the_statement_text(self):
        (row,) = csv_import.parse_file(
            amex_csv(amex_row("2025-10-06", "", "5.25", statement_as="PHILZ COFFEE"))
        ).rows
        assert row.name == "PHILZ COFFEE"


# --------------------------------------------------------------------------- targets


class TestTargets:
    def test_nothing_yet_offers_a_new_manual_account(self, db):
        targets = csv_import.import_targets(db)
        assert [(o.value, o.label) for o in targets.options] == [
            ("new", "New account: American Express (CSV)")
        ]
        assert targets.default == "new" and not targets.linked_only

    def test_the_only_linked_credit_card_is_the_default(self, db):
        manual = make_account(db, manual=True)
        linked = make_account(db, name="Gold Card", mask="1001")
        targets = csv_import.import_targets(db)
        assert targets.default == str(linked.id)
        assert [o.value for o in targets.options] == [str(linked.id), str(manual.id)]
        assert targets.options[0].label == "Gold Card ··1001" and targets.options[0].linked
        assert targets.options[1].label == "American Express (CSV)"

    def test_a_manual_account_is_labelled_csv_only_once(self, db):
        assert csv_import.account_label(make_account(db, manual=True)) == "American Express (CSV)"
        renamed = make_account(db, manual=True, name="Old Gold Card")
        assert csv_import.account_label(renamed) == "Old Gold Card (CSV only)"

    def test_the_only_manual_account_is_the_default_before_linking(self, db):
        manual = make_account(db, manual=True)
        targets = csv_import.import_targets(db)
        assert targets.default == str(manual.id)
        assert "new" not in [o.value for o in targets.options]  # one manual account is enough

    def test_two_linked_cards_have_no_default(self, db):
        item = make_item(db)
        make_account(db, item)
        make_account(db, item)
        assert csv_import.import_targets(db).default is None

    def test_hidden_accounts_are_not_offered(self, db):
        make_account(db, is_hidden=True)
        visible = make_account(db)
        assert [o.value for o in csv_import.import_targets(db).options] == [str(visible.id), "new"]

    def test_a_production_item_allows_only_linked_targets(self, db):
        manual = make_account(db, manual=True)
        linked = make_account(db)
        targets = csv_import.import_targets(db, production=True)
        assert targets.linked_only
        assert [o.value for o in targets.options] == [str(linked.id)]
        with pytest.raises(ImportRefused) as info:
            csv_import.preview(
                db, fixture("activity_2025-08.csv"), "a.csv", manual.id, production=True
            )
        assert info.value.kind == "account"
        with pytest.raises(ImportRefused):
            csv_import.preview(db, fixture("activity_2025-08.csv"), "a.csv", None, production=True)
        # Sandbox (or production before linking) still allows CSV-only accounts.
        assert csv_import.preview(db, fixture("activity_2025-08.csv"), "a.csv", manual.id).new == 15

    def test_an_unknown_account_is_refused(self, db):
        with pytest.raises(ImportRefused) as info:
            csv_import.preview(db, fixture("activity_2025-08.csv"), "a.csv", 999_999)
        assert info.value.kind == "account"

    @pytest.mark.parametrize(("value", "expected"), [("new", None), ("12", 12), (" 7 ", 7)])
    def test_parse_target(self, value, expected):
        assert csv_import.parse_target(value) == expected

    @pytest.mark.parametrize("value", ["", None, "abc", "-1"])
    def test_parse_target_refuses_junk(self, value):
        with pytest.raises(ImportRefused):
            csv_import.parse_target(value)


# --------------------------------------------------------------------------- preview and commit


class TestPreviewAndCommit:
    def test_preview_writes_nothing(self, db):
        before = db.scalar(select(func.count()).select_from(Transaction))
        preview = csv_import.preview(db, fixture("activity_2025-08.csv"), "aug.csv", None)
        assert preview.new == 15 and preview.new_account and preview.account_id is None
        assert db.scalar(select(func.count()).select_from(Transaction)) == before
        assert db.scalar(select(func.count()).select_from(CsvImport)) == 0
        assert db.scalar(select(func.count()).select_from(Account)) == 0
        assert csv_import.get_preview(preview.token) == preview

    def test_preview_counts_and_samples(self, db):
        preview = csv_import.preview(db, fixture("activity_malformed.csv"), "x.csv", None)
        assert (preview.row_count, preview.parsed, preview.new, len(preview.errors)) == (
            11,
            2,
            2,
            9,
        )
        assert (preview.date_min, preview.date_max) == (dt.date(2025, 6, 2), dt.date(2025, 6, 4))
        new = preview.group("new")
        assert new.count == 2 and [r.row_number for r in new.sample] == [2, 11]
        assert preview.reference_dates == 2
        assert preview.can_import and preview.to_add == 2

    def test_import_into_a_new_manual_account(self, db):
        preview, outcome = run_import(db, fixture("activity_2025-08.csv"), None, filename="aug.csv")
        account = db.get(Account, outcome.account_id)
        assert account.is_manual and account.name == "American Express (CSV)"
        assert (account.type, account.subtype) == ("credit", "credit card")
        rows = csv_rows(db, account)
        assert len(rows) == 15 == outcome.inserted_count
        assert outcome.duplicate_count == outcome.matched_plaid_count == outcome.error_count == 0
        assert set(outcome.inserted_transaction_ids) == {t.id for t in rows}
        record = db.get(CsvImport, outcome.csv_import_id)
        assert (record.filename, record.file_sha256, record.account_id) == (
            "aug.csv",
            preview.file_sha256,
            account.id,
        )
        assert (record.row_count, record.inserted_count, record.date_min, record.date_max) == (
            15,
            15,
            dt.date(2025, 8, 1),
            dt.date(2025, 8, 29),
        )
        first = rows[0]
        assert first.source == "csv" and not first.pending
        assert first.csv_import_id == record.id and first.duplicate_status == "none"
        assert first.authorized_date is None and first.iso_currency_code == "USD"
        assert first.posted_date == dt.date(2025, 8, 2)
        assert first.location["postal_code"] == "94111"
        payment = next(t for t in rows if t.name.startswith("AUTOPAY"))
        assert payment.location is None  # SQL NULL, not a JSON null
        assert db.scalar(select(func.count()).where(Transaction.location.is_(None))) >= 1
        assert csv_import.get_preview(preview.token) is None  # used once

    def test_rows_are_categorized_by_the_amex_mapping(self, db):
        _, outcome = run_import(db, fixture("activity_2025-08.csv"), None)
        rows = {t.name: t for t in csv_rows(db)}

        def category(prefix: str):
            txn = next(t for name, t in rows.items() if name.startswith(prefix))
            return txn.category.name, txn.category_source

        assert category("BLUE BOTTLE") == ("Dining & Drinks", "amex")
        assert category("WHOLEFDS") == ("Groceries", "amex")
        assert category("AUTOPAY") == ("Card Payments", "system")
        assert category("HILTON") == ("Travel", "amex")
        assert category("UBER") == ("Transportation", "amex")
        assert category("YOUR CASH BACK") == ("Credits & Rewards", "amex")  # a |credit mapping
        assert category("ANNUAL MEMBERSHIP FEE") == ("Fees & Interest", "amex")
        assert category("COMCAST") == ("Utilities & Bills", "amex")
        refund = next(t for t in csv_rows(db) if t.amount == Decimal("-42.00"))
        assert (refund.category.name, refund.category_source) == ("Shopping", "amex")

    def test_rules_come_before_the_amex_mapping(self, db):
        rides = get_category(db, "Travel")
        rule = make_rule(db, rides, description_pattern="uber")
        run_import(db, fixture("activity_2025-08.csv"), None)
        uber = next(t for t in csv_rows(db) if t.name.startswith("UBER"))
        assert (uber.category_id, uber.category_source, uber.rule_id) == (rides.id, "rule", rule.id)
        payment = next(t for t in csv_rows(db) if t.name.startswith("AUTOPAY"))
        assert payment.category_id == card_payments(db).id  # rules never override payments

    def test_an_unmapped_category_is_uncategorized(self, db):
        run_import(
            db, amex_csv(amex_row("2025-10-06", "MYSTERY", "9.00", category="Zzz-Unknown")), None
        )
        (txn,) = csv_rows(db)
        assert (txn.category_id, txn.category_source) == (uncategorized(db).id, "default")

    def test_the_same_file_twice_adds_nothing(self, db):
        _, first = run_import(db, fixture("activity_2025-08.csv"), None)
        account = db.get(Account, first.account_id)
        preview = csv_import.preview(db, fixture("activity_2025-08.csv"), "again.csv", account.id)
        assert (preview.new, preview.duplicates, preview.changed) == (0, 15, 0)
        assert preview.same_file_imported_at is not None
        assert not preview.can_import
        outcome = csv_import.commit(db, preview.token)
        assert (outcome.inserted_count, outcome.duplicate_count) == (0, 15)
        assert len(csv_rows(db)) == 15

    def test_overlapping_files_add_only_the_new_rows(self, db):
        _, first = run_import(db, fixture("activity_2025-08.csv"), None)
        account = db.get(Account, first.account_id)
        preview, outcome = run_import(db, fixture("activity_2025-09.csv"), account)
        assert (preview.new, preview.duplicates) == (3, 2)
        assert (outcome.inserted_count, outcome.duplicate_count) == (3, 2)
        assert len(csv_rows(db)) == 18

    def test_identical_charges_are_all_kept(self, db):
        run_import(db, fixture("activity_2025-08.csv"), None)
        hotel = [t for t in csv_rows(db) if t.name.startswith("HILTON")]
        assert len(hotel) == 5
        spent = db.scalar(
            select(func.sum(v_allocations.c.amount)).where(
                v_allocations.c.transaction_id.in_([t.id for t in hotel])
            )
        )
        assert spent == Decimal("945.00")

    def test_rows_with_errors_are_skipped_and_counted(self, db):
        _, outcome = run_import(db, fixture("activity_malformed.csv"), None)
        assert (outcome.row_count, outcome.inserted_count, outcome.error_count) == (11, 2, 9)

    def test_a_changed_row_is_flagged_and_skipped(self, db):
        reference = ref("2025-10-07")
        _, first = run_import(
            db, amex_csv(amex_row("2025-10-06", "PHILZ", "5.25", reference=reference)), None
        )
        account = db.get(Account, first.account_id)
        changed = amex_csv(amex_row("2025-10-06", "PHILZ", "6.25", reference=reference))
        preview = csv_import.preview(db, changed, "b.csv", account.id)
        assert (preview.changed, preview.duplicates, preview.new) == (1, 0, 0)
        (row,) = preview.group("changed").sample
        assert (row.earlier_amount, row.earlier_date) == (Decimal("5.25"), dt.date(2025, 10, 6))
        outcome = csv_import.commit(db, preview.token)
        assert (outcome.inserted_count, outcome.duplicate_count) == (0, 1)
        assert [t.amount for t in csv_rows(db)] == [Decimal("5.25")]

    def test_a_row_already_in_another_account_is_a_duplicate(self, db):
        _, first = run_import(db, fixture("activity_cp1252.csv"), None)
        other = make_account(db, name="Gold Card", mask="2002")
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", other.id)
        assert (preview.new, preview.duplicates) == (0, 2)
        assert {r.other_account for r in preview.group("duplicate").sample} == {
            "American Express (CSV)"
        }

    def test_an_existing_manual_account_is_reused_for_a_new_account_preview(self, db):
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", None)
        manual = make_account(db, manual=True)  # created meanwhile (another import)
        outcome = csv_import.commit(db, preview.token)
        assert outcome.account_id == manual.id
        assert db.scalar(select(func.count()).select_from(Account)) == 1

    def test_a_preview_expires_after_30_minutes(self, db, freeze_today):
        start = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
        freeze_today(now=start)
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", None)
        freeze_today(now=start + dt.timedelta(minutes=29))
        assert csv_import.get_preview(preview.token) is not None
        freeze_today(now=start + dt.timedelta(minutes=30))
        assert csv_import.get_preview(preview.token) is None
        with pytest.raises(ImportRefused) as info:
            csv_import.commit(db, preview.token)
        assert info.value.kind == "expired"

    def test_a_preview_is_imported_once(self, db, monkeypatch):
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", None)
        first = csv_import.commit(db, preview.token)
        assert csv_import.get_preview(preview.token) is None  # the page shows it as gone
        evaluated = []
        monkeypatch.setattr(
            alerts, "evaluate_after_import", lambda *args: evaluated.append(args) or []
        )
        # a double-clicked Import button: the second request answers with the first import
        again = csv_import.commit(db, preview.token)
        assert again.csv_import_id == first.csv_import_id
        assert (again.inserted_count, again.duplicate_count) == (2, 0)
        assert db.scalar(select(func.count()).select_from(CsvImport)) == 1
        assert len(csv_rows(db)) == 2
        assert evaluated == []

    def test_a_preview_whose_import_was_rolled_back_imports_again(self, db):
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", None)
        savepoint = db.begin_nested()
        csv_import.commit(db, preview.token)
        savepoint.rollback()  # e.g. the request's commit failed
        assert csv_rows(db) == []
        outcome = csv_import.commit(db, preview.token)
        assert outcome.inserted_count == 2
        assert db.scalar(select(func.count()).select_from(CsvImport)) == 1

    def test_only_a_few_previews_are_kept(self, db):
        tokens = [
            csv_import.preview(db, fixture("activity_cp1252.csv"), f"{i}.csv", None).token
            for i in range(csv_import.MAX_PREVIEWS + 3)
        ]
        assert len(csv_import._PREVIEWS) <= csv_import.MAX_PREVIEWS
        assert csv_import.get_preview(tokens[-1]) is not None
        assert csv_import.get_preview(tokens[0]) is None

    def test_an_account_deleted_since_the_preview_is_refused(self, db):
        manual = make_account(db, manual=True)
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", manual.id)
        db.delete(manual)
        db.flush()
        with pytest.raises(ImportRefused) as info:
            csv_import.commit(db, preview.token)
        assert info.value.kind == "account"

    def test_too_large_files_are_refused(self, db, monkeypatch):
        monkeypatch.setattr(csv_import, "MAX_FILE_BYTES", 100)
        with pytest.raises(ImportRefused) as info:
            csv_import.preview(db, fixture("activity_2025-08.csv"), "big.csv", None)
        assert info.value.kind == "too_large"

    def test_a_header_without_rows_is_refused(self, db):
        with pytest.raises(ImportRefused) as info:
            csv_import.preview(db, amex_csv(), "empty.csv", None)
        assert info.value.kind == "no_rows"

    def test_filenames_are_cleaned(self):
        assert csv_import.clean_filename("C:\\Users\\me\\activity.csv") == "activity.csv"
        assert csv_import.clean_filename("../../etc/x.csv") == "x.csv"
        assert csv_import.clean_filename("a\x00b\nc.csv") == "a b c.csv"
        assert csv_import.clean_filename(None) == "upload.csv"
        assert len(csv_import.clean_filename("a" * 500)) == 200

    def test_alerts_are_evaluated_after_an_import(self, db, notifier, monkeypatch):
        seen = []
        monkeypatch.setattr(
            alerts, "evaluate_after_import", lambda s, n, outcome: seen.append((n, outcome)) or []
        )
        _, outcome = run_import(db, fixture("activity_cp1252.csv"), None, notifier=notifier)
        assert seen == [(notifier, outcome)]

    def test_the_import_takes_the_ledger_lock(self, db):
        preview = csv_import.preview(db, fixture("activity_cp1252.csv"), "c.csv", None)
        assert ledger_locks_held(db) == 0  # a preview writes nothing and locks nothing
        csv_import.commit(db, preview.token)
        assert ledger_locks_held(db) >= 1


# --------------------------------------------------------------------------- linked accounts


class TestLinkedAccounts:
    def test_rows_that_plaid_already_has_are_merged_not_inserted(self, db):
        account = make_account(db)
        plaid = make_transaction(
            db,
            account,
            amount="5.25",
            date="2025-10-08",
            authorized_date="2025-10-06",
            name="Philz Coffee",
        )
        data = amex_csv(
            amex_row("2025-10-06", "PHILZ COFFEE", "5.25", reference="320252810000000001"),
            amex_row("2025-10-06", "BLUE BOTTLE", "6.50"),
        )
        preview = csv_import.preview(db, data, "a.csv", account.id)
        assert (preview.new, preview.matched) == (1, 1)
        (row,) = preview.group("matched").sample
        assert (row.plaid_name, row.plaid_date) == ("Philz Coffee", dt.date(2025, 10, 6))
        outcome = csv_import.commit(db, preview.token)
        assert (outcome.inserted_count, outcome.matched_plaid_count) == (1, 1)
        db.refresh(plaid)
        assert plaid.csv_reference == "320252810000000001"
        assert plaid.csv_import_id == outcome.csv_import_id
        assert [t.name for t in csv_rows(db)] == ["BLUE BOTTLE"]
        record = db.get(CsvImport, outcome.csv_import_id)
        assert (record.inserted_count, record.matched_plaid_count) == (1, 1)
        # A second import of the same file now finds the Plaid row by its Reference.
        again = csv_import.preview(db, data, "a.csv", account.id)
        assert (again.new, again.matched, again.duplicates) == (0, 0, 2)

    def test_rows_inside_plaids_history_without_a_match_are_held_for_review(self, db):
        item = make_item(db, history_complete_at=clock.now())
        account = make_account(db, item)
        make_transaction(db, account, amount="40.00", date="2025-10-01")  # history starts here
        data = amex_csv(
            amex_row("2025-09-30", "BEFORE", "11.00"),
            amex_row("2025-10-02", "INSIDE", "12.00"),
        )
        preview = csv_import.preview(db, data, "a.csv", account.id)
        assert (preview.new, preview.suspects) == (1, 1)
        assert [r.name for r in preview.group("suspect").sample] == ["INSIDE"]
        outcome = csv_import.commit(db, preview.token)
        assert outcome.inserted_count == 2  # both stored; one held out of the totals
        statuses = {t.name: t.duplicate_status for t in csv_rows(db)}
        assert statuses == {"BEFORE": "none", "INSIDE": "suspect"}
        assert db.get(Account, account.id).plaid_history_start == dt.date(2025, 10, 1)
        record = csv_import.import_record(db, outcome.csv_import_id)
        assert record.suspects == 1

    def test_without_complete_history_nothing_is_held(self, db):
        account = make_account(db)
        make_transaction(db, account, amount="40.00", date="2025-10-01")
        preview = csv_import.preview(
            db, amex_csv(amex_row("2025-10-02", "INSIDE", "12.00")), "a.csv", account.id
        )
        assert (preview.new, preview.suspects) == (1, 0)


# --------------------------------------------------------------------------- review, history


class TestReview:
    def suspect(self, db, **kwargs):
        account = kwargs.pop("account", None) or make_account(db)
        return make_transaction(
            db, account, source="csv", duplicate_status="suspect", amount="12.00", **kwargs
        )

    def test_list_keep_and_delete(self, db):
        a = self.suspect(db, name="HOTEL", date="2025-10-02")
        b = self.suspect(db, name="CAR", date="2025-10-01")
        make_transaction(db, source="csv", name="NORMAL")
        assert [s.name for s in csv_import.list_suspects(db)] == ["CAR", "HOTEL"]
        assert csv_import.keep_suspect(db, a.id)
        assert db.get(Transaction, a.id).duplicate_status == "kept"
        assert csv_import.delete_suspect(db, b.id)
        assert db.get(Transaction, b.id) is None
        assert csv_import.list_suspects(db) == []
        assert not csv_import.keep_suspect(db, a.id)  # kept now: not a suspect any more
        assert not csv_import.delete_suspect(db, a.id)
        assert not csv_import.keep_suspect(db, 999_999)

    def test_kept_rows_count_again(self, db):
        txn = self.suspect(db, date="2025-10-02")
        counted = select(func.count()).where(v_allocations.c.transaction_id == txn.id)
        assert db.scalar(counted) == 0
        csv_import.keep_suspect(db, txn.id)
        db.flush()
        assert db.scalar(counted) == 1

    def test_plaid_rows_are_never_suspects_here(self, db):
        plaid = make_transaction(db, duplicate_status="suspect")
        assert not csv_import.keep_suspect(db, plaid.id)
        assert not csv_import.delete_suspect(db, plaid.id)

    def test_history_is_newest_first_with_held_counts(self, db):
        _, first = run_import(db, fixture("activity_cp1252.csv"), None, filename="july.csv")
        _, second = run_import(db, fixture("activity_2025-08.csv"), None, filename="aug.csv")
        txn = csv_rows(db)[-1]
        txn.duplicate_status = "suspect"
        db.flush()
        history = csv_import.import_history(db)
        assert [h.filename for h in history] == ["aug.csv", "july.csv"]
        assert history[0].suspects == 1 and history[1].suspects == 0
        assert history[0].account_label == "American Express (CSV)"

    def test_attach_offers_need_a_manual_and_a_linked_account(self, db):
        manual = make_account(db, manual=True)
        make_transaction(db, manual, source="csv")
        assert csv_import.attach_offers(db) == []
        linked = make_account(db, name="Gold Card", mask="1001")
        (offer,) = csv_import.attach_offers(db)
        assert (offer.manual_id, offer.transaction_count) == (manual.id, 1)
        assert offer.targets == ((linked.id, "Gold Card ··1001"),)
        assert offer.default_target == linked.id

    def test_reference_dates_compare_paired_rows(self, db):
        account = make_account(db)
        make_transaction(
            db,
            account,
            date="2025-10-08",
            authorized_date="2025-10-06",
            csv_reference="320252810000000001",  # 2025-10-08: equal
        )
        make_transaction(
            db,
            account,
            date="2025-10-09",
            authorized_date="2025-10-06",
            csv_reference="320252810000000002",  # 2025-10-08: one day early
        )
        make_transaction(db, account, date="2025-10-09", csv_reference="X-not-a-date")
        stats = csv_import.reference_dates(db)
        assert (stats.compared, stats.matched, stats.percent) == (2, 1, Decimal(50))
        assert csv_import.reference_dates(db, csv_import_id=123).compared == 0
        assert csv_import.ReferenceDates().percent is None
