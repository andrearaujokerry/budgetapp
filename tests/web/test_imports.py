"""The Import page (SYNC-8): upload → preview → import, review held rows, attach, errors."""

from __future__ import annotations

import re
from tempfile import SpooledTemporaryFile
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select
from starlette import formparsers

from budgetapp import clock
from budgetapp.models import Account, CsvImport, Transaction
from budgetapp.services import csv_import
from budgetapp.web.routes import imports as imports_routes
from tests.factories import make_account, make_item, make_transaction
from tests.test_csv_import import amex_csv, amex_row, fixture
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems


@pytest.fixture(autouse=True)
def _forget_previews():
    yield
    csv_import._PREVIEWS.clear()


def upload(client, data: bytes, *, account: str = "new", filename: str = "activity.csv", **kw):
    return client.post(
        "/import/preview",
        files={"file": (filename, data, "text/csv")},
        data={"account": account},
        follow_redirects=False,
        **kw,
    )


def preview_token(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/import?preview=")
    return parse_qs(urlparse(location).query)["preview"][0]


def toasts(html: str) -> list[str]:
    return re.findall(r'<div class="toast__text">([^<]*)</div>', html)


def assert_clean(response) -> None:
    assert csp_problems(response.text, rendered=True) == []


# --------------------------------------------------------------------------- the page


def test_the_page_renders_on_an_empty_database(client):
    response = client.get("/import")
    assert response.status_code == 200
    assert response.template.name == "imports/index.html"
    html = response.text
    assert (
        '<form class="form" method="post" action="/import/preview" enctype="multipart/form-data">'
        in html
    )
    assert 'type="file"' in html and 'accept=".csv,text/csv"' in html and 'name="file"' in html
    assert '<option value="new" selected>New account: American Express (CSV)</option>' in html
    assert "Include all additional transaction details" in html
    assert "No imports yet" in html
    assert 'id="suspects" hidden' in html
    assert '<link rel="stylesheet" href="/static/css/pages/imports.css">' in html
    assert_clean(response)


def test_the_page_css_exists(client):
    response = client.get("/static/css/pages/imports.css")
    assert response.status_code == 200 and ".import-steps" in response.text


def test_upload_preview_import(client, db):
    response = upload(client, fixture("activity_2025-08.csv"), filename="aug.csv")
    token = preview_token(response)
    page = client.get(f"/import?preview={token}")
    assert page.status_code == 200
    preview = page.context["preview"]
    assert (preview.new, preview.duplicates, len(preview.errors)) == (15, 0, 0)
    html = page.text
    assert "Preview: aug.csv" in html
    assert (
        "Into American Express (CSV) (new account) · 15 rows · Aug 1, 2025 – Aug 29, 2025" in html
    )
    assert "Will be added" in html and "First 5 of 15" in html
    assert "A CSV-only account “American Express (CSV)” will be created." in html
    assert f'<input type="hidden" name="token" value="{token}">' in html
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0  # nothing yet
    assert_clean(page)

    done = client.post("/import/commit", data={"token": token}, follow_redirects=False)
    assert done.status_code == 303
    record = db.scalars(select(CsvImport)).one()
    assert done.headers["location"] == f"/import?done={record.id}"
    page = client.get(done.headers["location"])
    assert toasts(page.text) == ["Imported aug.csv: 15 added."]
    assert "Imported aug.csv into American Express (CSV)." in page.text
    assert page.context["history"][0].filename == "aug.csv"
    assert page.context["preview"] is None
    assert db.scalar(select(func.count()).select_from(Transaction)) == 15
    assert_clean(page)


def test_a_double_clicked_import_imports_once(client, db):
    token = preview_token(upload(client, fixture("activity_2025-08.csv"), filename="aug.csv"))
    first = client.post("/import/commit", data={"token": token}, follow_redirects=False)
    second = client.post("/import/commit", data={"token": token}, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    record = db.scalars(select(CsvImport)).one()
    assert second.headers["location"] == first.headers["location"] == f"/import?done={record.id}"
    page = client.get(second.headers["location"])
    assert toasts(page.text) == ["Imported aug.csv: 15 added."]  # not "0 added, 15 already…"
    assert db.scalar(select(func.count()).select_from(Transaction)) == 15


def test_changed_rows_are_reported_apart_after_an_import(client, db):
    """Review finding: the flash, banner and history said "already imported" for them."""
    reference = "320252140000004242"
    first = amex_csv(amex_row("2025-08-01", "PHILZ", "5.25", reference=reference))
    client.post("/import/commit", data={"token": preview_token(upload(client, first))})
    manual = db.scalars(select(Account)).one()
    data = (
        amex_csv(
            amex_row("2025-08-01", "PHILZ", "6.25", reference=reference),
            amex_row("2025-08-03", "BLUE BOTTLE", "4.00"),
        )
        + b"13/45/2025,BAD,ALEX RIVERA,-91005,1.00,,BAD,,,,,'320252130000000999',Other\r\n"
    )
    token = preview_token(upload(client, data, account=str(manual.id), filename="b.csv"))
    page = client.post("/import/commit", data={"token": token})
    assert toasts(page.text) == [
        "Imported b.csv: 1 added, 1 changed since an earlier import (skipped), "
        "1 with errors skipped."
    ]
    assert (
        "1 added, 0 merged with Plaid transactions, 0 already imported, 2 skipped (unreadable, "
        "or changed since an earlier import)." in page.text
    )
    assert '<th scope="col" class="num col-optional-md">Skipped</th>' in page.text
    assert_clean(page)


def test_a_preview_with_errors_and_duplicates(client, db):
    token = preview_token(upload(client, fixture("activity_malformed.csv")))
    client.post("/import/commit", data={"token": token})
    manual = db.scalars(select(Account)).one()
    token = preview_token(upload(client, fixture("activity_malformed.csv"), account=str(manual.id)))
    page = client.get(f"/import?preview={token}")
    html = page.text
    assert "Nothing new to import." in html
    assert "Already imported" in html and "Can’t be read" in html
    assert "“13/45/2025” isn’t a date (expected M/D/YYYY)." in html
    assert 'action="/import/commit"' not in html  # nothing to import: no Import button
    assert "Choose another file" in html
    assert "You imported this exact file on" in html
    assert_clean(page)


def test_a_linked_card_shows_the_matching_plaid_transactions(client, db):
    account = make_account(db, name="Gold Card", mask="1001")
    make_transaction(
        db,
        account,
        amount="5.25",
        date="2025-10-08",
        authorized_date="2025-10-06",
        name="Philz Coffee",
    )
    data = amex_csv(amex_row("2025-10-06", "PHILZ COFFEE", "5.25", lag=2))  # posted 10-08
    page = client.get(
        f"/import?preview={preview_token(upload(client, data, account=str(account.id)))}"
    )
    html = page.text
    assert "Already synced from Plaid" in html and "Philz Coffee" in html
    assert "merges <strong>1</strong> with transactions Plaid already has" in html
    assert_clean(page)


def test_an_import_with_rows_held_for_review(client, db):
    item = make_item(db, history_complete_at=clock.now())
    account = make_account(db, item, name="Gold Card", mask="1001")
    make_transaction(db, account, amount="40.00", date="2025-10-01")
    data = amex_csv(amex_row("2025-10-02", "HOTEL CHECK-IN", "312.00", category="Travel-Lodging"))
    token = preview_token(upload(client, data, account=str(account.id)))
    page = client.get(f"/import?preview={token}")
    assert "1 row will be held for review." in page.text
    done = client.post("/import/commit", data={"token": token})
    assert toasts(done.text) == ["Imported activity.csv: 1 added (1 held for review)."]
    assert "HOTEL CHECK-IN" in done.text and "Held for review" in done.text
    assert "1 held" in done.text  # the history badge
    assert_clean(done)


# --------------------------------------------------------------------------- refused uploads


def test_the_basic_download_is_refused_with_instructions(client):
    response = upload(client, fixture("activity_basic.csv"))
    assert response.status_code == 422
    assert response.template.name == "imports/index.html"
    assert "basic download" in response.text and 'aria-invalid="true"' in response.text
    assert "Include all additional transaction details" in response.text
    assert_clean(response)


def test_no_file_chosen(client):
    response = client.post("/import/preview", data={"account": "new"}, files={"x": ("", b"")})
    assert response.status_code == 422
    assert "Choose the CSV file you downloaded." in response.text


def test_a_plain_form_without_a_file(client):
    response = client.post("/import/preview", data={"account": "new"})
    assert response.status_code == 422
    assert "Choose the CSV file you downloaded." in response.text


def test_no_account_chosen(client, db):
    item = make_item(db)
    make_account(db, item)
    make_account(db, item)
    response = upload(client, fixture("activity_cp1252.csv"), account="")
    assert response.status_code == 422
    assert "Choose the account to import into." in response.text
    assert response.context["errors"] == {"account": "Choose the account to import into."}


def test_a_file_too_large_is_refused_before_it_is_read(client, monkeypatch):
    monkeypatch.setattr(imports_routes, "MAX_BODY_BYTES", 1000)
    response = upload(client, fixture("activity_2025-08.csv"))
    assert response.status_code == 422
    assert "larger than 10 MB" in response.text


def test_a_chunked_upload_over_the_limit_is_refused_while_it_streams(client, monkeypatch):
    """No Content-Length to check first: the stream itself is cut at the limit."""
    monkeypatch.setattr(imports_routes, "MAX_BODY_BYTES", 2000)
    data = amex_csv(*[amex_row("2025-06-01", f"COFFEE {i}", "4.50") for i in range(100)])
    head = (
        '--XyZ\r\nContent-Disposition: form-data; name="account"\r\n\r\nnew\r\n'
        '--XyZ\r\nContent-Disposition: form-data; name="file"; filename="a.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    )
    body = head.encode() + data + b"\r\n--XyZ--\r\n"

    def chunks():
        for start in range(0, len(body), 512):
            yield body[start : start + 512]

    response = client.post(
        "/import/preview",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=XyZ"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "larger than 10 MB" in response.text


def test_uploads_stay_in_memory(client, monkeypatch):
    """Starlette spools uploads over 1 MB to a temporary file; this page never does."""
    rolled = []

    class Spy(SpooledTemporaryFile):
        def rollover(self):
            rolled.append(self._max_size)
            super().rollover()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Spy)
    details = "SYNTHETIC EXTENDED DETAILS " * 4
    rows = [
        amex_row("2025-06-01", f"COFFEE SHOP {i:05d}", "4.50", **{"Extended Details": details})
        for i in range(6000)
    ]
    data = amex_csv(*rows)
    assert len(data) > 1024 * 1024
    response = upload(client, data)
    assert response.status_code == 303
    assert rolled == []
    assert imports_routes.InMemoryMultiPartParser.spool_max_size > imports_routes.MAX_BODY_BYTES


def test_an_expired_preview_link(client):
    page = client.get("/import?preview=gone")
    assert page.status_code == 200
    assert "That preview is gone." in page.text


def test_importing_an_expired_preview(client):
    response = client.post("/import/commit", data={"token": "gone"})
    assert response.status_code == 200 and response.url.path == "/import"
    assert toasts(response.text) == [
        "That preview has expired (previews last 30 minutes) or was already imported. Upload the "
        "file again."
    ]


def test_cancel_forgets_the_preview(client):
    token = preview_token(upload(client, fixture("activity_cp1252.csv")))
    response = client.post("/import/discard", data={"token": token}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/import"
    assert csv_import.get_preview(token) is None


def test_posts_from_other_sites_are_refused(raw_client):
    response = raw_client.post(
        "/import/preview",
        files={"file": ("a.csv", fixture("activity_cp1252.csv"), "text/csv")},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- held for review


def make_suspect(db, name="ACE HOTEL", **kwargs):
    return make_transaction(
        db, source="csv", duplicate_status="suspect", amount="312.00", name=name, **kwargs
    )


def test_held_rows_are_listed(client, db):
    make_suspect(db, date="2025-10-02")
    page = client.get("/import")
    html = page.text
    assert '<section class="card card--flush" id="suspects"' in html
    assert "ACE HOTEL" in html
    assert (
        'hx-post="/import/suspects/' in html
        and 'data-confirm="Delete this CSV row (ACE HOTEL, ' in html
    )
    assert_clean(page)


def test_keep_and_delete_with_htmx(client, db):
    keep = make_suspect(db, name="KEEP ME")
    drop_id = make_suspect(db, name="DROP ME").id
    response = client.post(f"/import/suspects/{keep.id}/keep", headers=HTMX)
    assert response.status_code == 200
    assert response.template.name == "imports/_suspects.html"
    assert "KEEP ME" not in response.text and "DROP ME" in response.text
    assert toasts(response.text) == ["Kept. It now counts in your totals."]
    assert 'hx-swap-oob="beforeend:#flash"' in response.text
    assert_clean(response)
    db.expire_all()
    assert db.get(Transaction, keep.id).duplicate_status == "kept"

    response = client.post(f"/import/suspects/{drop_id}/delete", headers=HTMX)
    assert toasts(response.text) == ["Deleted the duplicate CSV row."]
    assert '<div id="suspects" hidden></div>' in response.text
    db.expunge_all()
    assert db.get(Transaction, drop_id) is None


def test_keep_without_javascript_redirects(client, db):
    suspect = make_suspect(db)
    response = client.post(f"/import/suspects/{suspect.id}/keep", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/import"


def test_an_unknown_or_handled_row_is_a_404(client, db):
    assert client.post("/import/suspects/999999/keep").status_code == 404
    normal = make_transaction(db, source="csv")
    assert client.post(f"/import/suspects/{normal.id}/delete").status_code == 404


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/import?done=99999999999999999999"),
        ("post", "/import/suspects/99999999999999999999/keep"),
        ("post", "/import/suspects/9223372036854775808/delete"),  # 2**63
    ],
)
def test_ids_beyond_bigint_are_bad_requests(app, make_client, method, path):
    """Review finding: they reached PostgreSQL and the user got a 500 page."""
    client = make_client(app, raise_server_exceptions=False)
    response = getattr(client, method)(path)
    assert response.status_code == 400
    assert "Bad request" in response.text


def test_a_held_row_pending_in_plaid_says_so_and_keep_asks_first(client, db):
    """Review finding: it read "no matching Plaid charge", and Keep counted it twice."""
    item = make_item(db, history_complete_at=clock.now())
    account = make_account(db, item, name="Gold Card", mask="1001")
    make_transaction(db, account, amount="40.00", date="2025-10-01", authorized_date="2025-09-30")
    make_transaction(
        db, account, amount="64.10", date="2025-10-20", pending=True, name='SAFEWAY "#1490"'
    )
    data = amex_csv(
        amex_row("2025-10-19", "SAFEWAY 1490 SAN FRANCISCO", "64.10"),
        amex_row("2025-10-12", "ACE HARDWARE", "18.00"),
    )
    token = preview_token(upload(client, data, account=str(account.id)))
    page = client.get(f"/import?preview={token}")
    html = page.text
    assert "1 of them pending in Plaid" in html
    assert "Rows marked “Pending in Plaid” merge on their own" in html
    assert "Pending in Plaid: SAFEWAY &#34;#1490&#34; · Oct 20, 2025" in html
    assert_clean(page)

    done = client.post("/import/commit", data={"token": token})
    html = done.text
    assert "Rows pending in Plaid merge on their own" in html
    assert "Pending in Plaid: SAFEWAY &#34;#1490&#34;, Oct 20, 2025" in html
    pending_id, plain_id = (
        s.id for s in sorted(csv_import.list_suspects(db), key=lambda s: not s.pending)
    )
    keep_forms = dict(
        re.findall(r'<form method="post" action="/import/suspects/(\d+)/keep"([^>]*)>', html)
    )
    assert "Keeping it now counts the charge twice until then." in keep_forms[str(pending_id)]
    assert "data-confirm" not in keep_forms[str(plain_id)]
    assert_clean(done)


def test_html_in_descriptions_and_file_names_is_escaped(client, db):
    data = amex_csv(
        amex_row("2025-08-01", "<script>alert(1)</script>", "1.00", statement_as="<b>x</b>")
    )
    token = preview_token(upload(client, data, filename='"><img src=x>.csv'))
    page = client.get(f"/import?preview={token}")
    assert "<script>alert(1)</script>" not in page.text and "<img src=x>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert_clean(page)


def test_held_row_names_are_escaped_in_attributes(client, db):
    item = make_item(db, history_complete_at=clock.now())
    account = make_account(db, item)
    make_transaction(db, account, amount="40.00", date="2025-10-01")
    evil = 'X" onmouseover="alert(1)" data-x="<script>alert(2)</script> & co'
    token = preview_token(
        upload(client, amex_csv(amex_row("2025-10-05", evil, "12.00")), account=str(account.id))
    )
    page = client.post("/import/commit", data={"token": token})
    assert 'onmouseover="alert(1)"' not in page.text
    assert "<script>alert(2)</script>" not in page.text
    (confirm,) = re.findall(r'data-confirm="(Delete this CSV row[^"]*)"', page.text)
    assert "X&#34; onmouseover=&#34;alert(1)&#34;" in confirm
    assert "If you import a file with this row again, it is held for review again." in confirm
    # (no csp_problems() here: its regex sees " onmouseover=" inside the escaped value itself)


# --------------------------------------------------------------------------- attaching


def test_attach_a_csv_only_account(client, db):
    manual = make_account(db, manual=True)
    make_transaction(db, manual, source="csv", amount="9.00", date="2025-06-01")
    linked = make_account(db, name="Gold Card", mask="1001")
    manual_id = manual.id
    page = client.get("/import")
    html = page.text
    assert "Attach your CSV history to Gold Card ··1001" in html
    assert f'<input type="hidden" name="manual_id" value="{manual.id}">' in html
    assert f'<input type="hidden" name="account_id" value="{linked.id}">' in html
    assert_clean(page)

    response = client.post(
        "/import/attach",
        data={"manual_id": manual.id, "account_id": linked.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/import")
    assert toasts(page.text) == ["Attached: 1 transaction moved to the linked card."]
    db.expunge_all()
    assert db.get(Account, manual_id) is None
    assert "Attach your CSV history" not in page.text


def test_attach_asks_which_card_when_there_are_several(client, db):
    item = make_item(db)
    make_account(db, item, name="Gold Card", mask="1001")
    make_account(db, item, name="Blue Card", mask="2002")
    make_account(db, manual=True)
    page = client.get("/import")
    assert "Attach your CSV history to your linked card" in page.text
    assert "Choose a card" in page.text
    assert_clean(page)


@pytest.mark.parametrize("data", [{"manual_id": "x", "account_id": "1"}, {}])
def test_attach_needs_both_accounts(client, data):
    page = client.post("/import/attach", data=data)
    assert toasts(page.text) == ["Choose the linked card to attach to."]


@pytest.mark.parametrize(
    "data",
    [
        {"manual_id": "99999999999999999999", "account_id": "1"},
        {"manual_id": "1", "account_id": "9223372036854775808"},
        {"manual_id": "²", "account_id": "1"},  # a digit to str.isdigit(), not to int()
    ],
)
def test_attach_refuses_ids_that_cannot_name_an_account(app, make_client, data):
    """Review finding: an id beyond BIGINT gave a 500 page."""
    client = make_client(app, raise_server_exceptions=False)
    page = client.post("/import/attach", data=data)
    assert page.status_code == 200
    assert toasts(page.text) == ["Choose the linked card to attach to."]


def test_an_upload_into_an_impossible_account_is_refused(client):
    response = upload(client, fixture("activity_cp1252.csv"), account="99999999999999999999")
    assert response.status_code == 422
    assert response.context["errors"] == {"account": "Choose the account to import into."}


def test_attach_refuses_wrong_accounts(client, db):
    linked = make_account(db)
    other = make_account(db)
    page = client.post("/import/attach", data={"manual_id": linked.id, "account_id": other.id})
    assert toasts(page.text) == ["That CSV account no longer exists. Reload the page."]
    assert db.get(Account, linked.id) is not None
