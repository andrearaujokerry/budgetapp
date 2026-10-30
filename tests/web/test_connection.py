"""The Connection page's states (SYNC-1, SYNC-5 … SYNC-7): not linked (Sandbox and production),
linked and healthy, reconnect needed, consent warning, statement details not shared, sync errors,
the recent runs, edits that couldn't be carried over, the CSV-attach choice and demo data.

Production pages use ``load_settings("production", <temp paths>)`` with the test database, the
in-memory secret store and ``FakePlaidGateway``: nothing reads the real Keychain or calls Plaid.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import re
from decimal import Decimal
from pathlib import Path

import pytest

from budgetapp import secrets as app_secrets
from budgetapp import web
from budgetapp.config import load_settings
from budgetapp.services import linking, userstate
from budgetapp.web import security
from tests.factories import (
    get_category,
    make_account,
    make_item,
    make_sync_run,
    make_tombstone,
    set_app_settings,
)
from tests.fake_plaid import FakePlaidGateway
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
PLAID_SCRIPT = '<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js" nonce="'


@pytest.fixture(autouse=True)
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def production(tmp_path, monkeypatch):
    """Production settings whose state folder (recovery files) is a temp folder."""
    monkeypatch.setattr(app_secrets, "fallback_recovery_dir", lambda: tmp_path / "fallback")
    return load_settings(
        "production",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        install_dir=tmp_path / "venv",
    )


@pytest.fixture
def prod_plaid() -> FakePlaidGateway:
    return FakePlaidGateway(plaid_env="production")


@pytest.fixture
def prod_client(make_app, make_client, production, prod_plaid):
    return make_client(make_app(production, gateway_factory=lambda _settings: prod_plaid))


@pytest.fixture
def demo_client(make_app, make_client):
    app = make_app()
    app.state.demo_name = "preview"
    return make_client(app)


def page(client, path: str = "/connection") -> str:
    response = client.get(path)
    assert response.status_code == 200
    assert response.template.name == "connection/index.html"
    assert csp_problems(response.text, rendered=True) == []
    return response.text


def main(html: str) -> str:
    """The page's own content (without the layout's top bar and sidebar)."""
    return html[html.index('<main id="main"') : html.index("</main>")]


def text(html: str) -> str:
    """The visible text, tags removed and spaces collapsed."""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


# --------------------------------------------------------------------------- not linked


def test_not_linked_in_the_sandbox_explains_linking_and_offers_both_ways(client):
    response = client.get("/connection")
    html = main(page(client))
    words = text(html)
    assert "Plaid opens a secure window where you sign in to American Express" in words
    assert "Read-only. Nothing here can pay, move money or change your card." in words
    assert 'data-link-mode="new"' in html and "Link with Plaid Link" in html
    assert '<form method="post" action="/connection/sandbox-item"' in html
    assert '<option value="custom" selected>' in html
    assert "1 of your 10" not in words  # Sandbox links are free
    assert PLAID_SCRIPT in response.text  # Link needs Plaid's script, with this page's nonce
    nonce = response.context["csp_nonce"]
    assert f'nonce="{nonce}"' in response.text
    assert response.headers["content-security-policy"] == security.plaid_link_csp(nonce, "sandbox")
    assert '<script src="/static/js/link.js" defer></script>' in response.text


def test_production_shows_the_checklist_and_confirms_the_one_time_link(prod_client):
    response = prod_client.get("/connection")
    html = main(page(prod_client))
    words = text(html)
    assert "This uses 1 of your 10 Plaid connections." in words
    for label in (
        "A Data Transparency use case is selected",
        "The App and Company profiles are complete",
        "American Express shows as enabled for OAuth",
    ):
        assert words.count(label) == 2  # the checklist card and the confirm dialog's boxes
    assert html.count('href="https://dashboard.plaid.com/') == 3
    assert 'hx-get="/connection/preflight" hx-trigger="load"' in html
    assert 'data-dialog-open="confirm-link"' in html  # the page's button opens the dialog …
    dialog = html[html.index('<dialog id="confirm-link"') :]
    assert (
        "This uses 1 of your 10 Plaid connections, permanently. Make sure the checklist is done"
        in text(dialog)
    )
    # … whose Link button stays disabled until every box is ticked (static/js/link.js)
    assert re.search(r'data-link-mode="new" data-needs-checklist disabled', dialog)
    assert "/connection/sandbox-item" not in html
    assert (
        "connect-src 'self' https://production.plaid.com/;"
        in (response.headers["content-security-policy"])
    )


def test_production_blocks_a_new_link_while_one_is_in_progress(prod_client, secrets):
    app_secrets.set_link_in_progress(store=secrets)
    html = main(page(prod_client))
    assert "Linking is paused." in text(html)
    assert re.search(r'data-dialog-open="confirm-link" disabled', html)


def test_production_offers_to_re_attach_a_saved_token_instead_of_a_new_link(prod_client, secrets):
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, "access-production-kept")
    html = main(page(prod_client))
    assert "A saved connection was found." in text(html)
    assert '<form method="post" action="/connection/recover">' in html
    assert re.search(r'data-dialog-open="confirm-link" disabled', html)


def test_the_sandbox_offers_to_recover_test_items_the_database_lost(
    client, fake_plaid, secrets, settings
):
    fake_item = fake_plaid.add_item(item_id="lost-item")
    app_secrets.store_access_token(settings, "lost-item", fake_item.access_token, store=secrets)
    html = main(page(client))
    assert "Keychain holds 1 Sandbox test connection that this database doesn't know" in text(html)
    assert '<form method="post" action="/connection/recover">' in html


def test_the_preflight_check_reports_what_plaid_says(prod_client, prod_plaid):
    response = prod_client.get("/connection/preflight", headers=HTMX)
    assert response.template.name == "connection/_preflight.html"
    assert "Plaid lists American Express with statement details" in response.text
    assert prod_plaid.calls_to("search_institutions") == [
        {"query": "American Express", "products": ("liabilities",)}
    ]
    prod_plaid.queue_error("search_institutions", prod_plaid.transport_error())
    failed = prod_client.get("/connection/preflight", headers=HTMX)
    assert failed.status_code == 200 and "This check is optional" in failed.text


def test_the_preflight_is_production_only(client, fake_plaid):
    response = client.get("/connection/preflight", headers=HTMX)
    assert response.status_code == 200 and "preflight__result" not in response.text
    assert fake_plaid.calls_to("search_institutions") == []


# --------------------------------------------------------------------------- linked


def linked_item(db, **values):
    values.setdefault("last_sync_success_at", NOW - dt.timedelta(hours=3))
    values.setdefault("history_complete_at", NOW - dt.timedelta(days=20))
    values.setdefault("liabilities_status", "ok")
    values.setdefault("consent_expires_at", NOW + dt.timedelta(days=200))
    item = make_item(db, linked_at=NOW - dt.timedelta(days=20), **values)
    account = make_account(
        db,
        item,
        name="Blue Cash Preferred",
        official_name="Blue Cash Preferred® Card",
        mask="3005",
        current_balance=Decimal("1234.56"),
        available_balance=Decimal("8765.44"),
        credit_limit=Decimal("10000.00"),
        balances_as_of=NOW - dt.timedelta(hours=3),
        statement_balance=Decimal("987.65"),
        statement_issue_date=dt.date(2026, 10, 3),
        minimum_payment=Decimal("35.00"),
        payment_due_date=dt.date(2026, 10, 24),
        last_payment_amount=Decimal("500.00"),
        last_payment_date=dt.date(2026, 10, 1),
        liabilities_as_of=NOW - dt.timedelta(hours=3),
    )
    return item, account


def test_a_healthy_connection_shows_its_status_cards_and_runs(db, client):
    item, account = linked_item(db, plaid_last_update_at=NOW - dt.timedelta(hours=5))
    for n in range(25):
        make_sync_run(
            db,
            item,
            started_at=NOW - dt.timedelta(hours=6 * n + 3),
            finished_at=NOW - dt.timedelta(hours=6 * n + 3) + dt.timedelta(seconds=2),
            added_count=n,
            update_status="HISTORICAL_UPDATE_COMPLETE",
        )
    html = main(page(client))
    words = text(html)
    assert '<h2 class="section__title" id="item-' in html and "American Express" in words
    assert '<span class="pill pill--healthy">' in html and "Synced 3 h ago" in words
    assert "Last successful sync Oct 20, 2026," in words and "(3 h ago)" in words
    assert "Plaid's last update from Amex Oct 20, 2026," in words
    assert "History All received (Sep 30, 2026)" in words
    assert "Consent Renew by May 8, 2027 (200 days left)" in words
    assert "Statement details Shared" in words
    assert "Blue Cash Preferred ··3005" in words
    assert "$1,234.56" in words and "Available $8,765.44" in words
    assert "$987.65" in words and "Statement of Oct 3, 2026" in words
    assert "$35.00" in words and "Due Oct 24, 2026" in words
    assert "$10,000" in words and "Last payment $500.00 on Oct 1" in words
    assert "Balances as of Oct 20, 2026," in words and "Statement details as of" in words
    assert html.count('<tr>\n<td class="table__date">') == 20  # the last 20 runs only
    assert 'data-link-mode="update"' not in html and "Renew consent" not in words
    assert 'data-link-mode="liabilities"' not in html
    live = re.search(r'<div id="connection-live"[^>]*>', html).group(0)
    assert 'hx-trigger="connection-changed from:body"' in live  # nothing to poll for


def test_a_due_date_within_a_week_is_highlighted_and_overdue_is_red(db, client):
    _, account = linked_item(db)
    html = main(page(client))
    assert re.search(r'class="stat stat--warn">.*?Minimum payment.*?Due Oct 24, 2026', html, re.S)
    account.is_overdue = True
    db.flush()
    html = main(page(client))
    assert re.search(
        r'class="stat stat--over">.*?Minimum payment.*?Overdue since Oct 24', html, re.S
    )


def test_a_charge_card_has_no_preset_limit(db, client):
    _, account = linked_item(db)
    account.credit_limit = None
    db.flush()
    assert "No preset limit" in text(main(page(client)))


def test_reconnect_needed_shows_plaids_message_and_update_mode(db, client):
    item, _ = linked_item(
        db,
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
        error_message="the login details of this item have changed",
        error_display_message="Please sign in again to American Express.",
    )
    html = main(page(client))
    words = text(html)
    assert "Reconnect needed. Please sign in again to American Express." in words
    assert f'data-link-mode="update" data-item-id="{item.id}"' in html
    assert '<span class="pill pill--needs_reconnect">' in html
    assert 'class="notices"' not in client.get("/connection").text  # no layout banner here


def test_reconnect_falls_back_to_plain_words_without_a_display_message(db, client):
    linked_item(db, status="needs_reconnect", error_code="ITEM_LOGIN_REQUIRED")
    assert "Amex needs you to sign in again through Plaid" in text(main(page(client)))


@pytest.mark.parametrize(
    ("days", "tone", "title"),
    [
        (25, "warn", "Renew by Nov 14, 2026 (25 days left)."),
        (0, "over", "Expires today (Oct 20, 2026)."),
        (-3, "over", "Expired on Oct 17, 2026."),
    ],
)
def test_the_consent_warning_shows_the_due_date_and_renews_in_update_mode(
    db, client, days, tone, title
):
    set_app_settings(db, consent_reminder_days=30)
    item, _ = linked_item(db, consent_expires_at=NOW + dt.timedelta(days=days))
    html = main(page(client))
    live = html[html.index('id="connection-live"') :]
    banner = re.search(rf'<div class="banner banner--{tone}"[^>]*>.*?</div>\s*</div>', live, re.S)
    assert banner and title in text(banner.group(0))
    assert f'data-link-mode="update" data-item-id="{item.id}"' in banner.group(0)
    assert "Renew consent" in banner.group(0)


def test_consent_without_a_plaid_date_is_estimated_from_the_last_confirmation(db, client):
    linked_item(db, consent_expires_at=None, last_consented_at=NOW - dt.timedelta(days=100))
    words = text(main(page(client)))
    assert "Renew by Jul 12, 2027 (265 days left) (estimated" in words


def test_statement_details_not_shared_offer_update_mode_with_liabilities(db, client):
    item, account = linked_item(
        db, consented_products=["transactions"], liabilities_status="not_consented"
    )
    account.statement_balance = account.minimum_payment = account.payment_due_date = None
    account.statement_issue_date = account.liabilities_as_of = None
    db.flush()
    html = main(page(client))
    words = text(html)
    assert "Statement details aren't shared." in words
    assert f'data-link-mode="liabilities" data-item-id="{item.id}"' in html
    assert "Statement details Not shared" in words
    assert "Not shared" in words  # under the statement balance and the minimum payment


@pytest.mark.parametrize(
    ("status", "words"),
    [
        ("unsupported", "American Express doesn't provide them for this card"),
        ("error", "Couldn't be read last time; the next sync tries again"),
        (None, "Shared; read on the next sync"),
    ],
)
def test_the_liabilities_status_in_words(db, client, status, words):
    linked_item(db, liabilities_status=status)
    assert f"Statement details {words}" in text(main(page(client)))


def test_a_failed_sync_shows_the_error(db, client):
    linked_item(
        db,
        status="error",
        error_code="INSTITUTION_NOT_RESPONDING",
        error_message="this institution is not currently responding",
    )
    words = text(main(page(client)))
    assert "The last sync failed (INSTITUTION_NOT_RESPONDING)." in words
    assert "Amex isn't answering Plaid right now" in words


def test_stale_data_is_called_out(db, client):
    linked_item(db, last_sync_success_at=NOW - dt.timedelta(hours=40))
    words = text(main(page(client)))
    assert "Your data may be out of date." in words


def test_history_still_loading(db, client):
    linked_item(db, history_complete_at=None, transactions_update_status="INITIAL_UPDATE_COMPLETE")
    assert "History Recent transactions are in; older ones follow" in text(main(page(client)))


def test_an_account_notice_from_churn_is_shown(db, client):
    _, account = linked_item(db)
    account.notice = "Plaid re-issued this card's account on Oct 19, 2026."
    db.flush()
    assert "Check this card. Plaid re-issued this card's account" in text(main(page(client)))


def test_failed_and_skipped_runs_explain_themselves(db, client):
    item, _ = linked_item(db)
    make_sync_run(
        db,
        item,
        trigger="schedule",
        status="error",
        started_at=NOW - dt.timedelta(hours=1),
        error_code="ITEM_LOGIN_REQUIRED",
        error_message="the login details of this item have changed",
    )
    make_sync_run(
        db,
        item,
        status="skipped",
        started_at=NOW - dt.timedelta(minutes=30),
        error_message="Another sync of this connection was running.",
    )
    runs = main(page(client))
    runs = runs[runs.index('id="sync-runs"') :]
    words = text(runs)
    assert "Skipped" in words and "Another sync of this connection was running." in words
    assert "Scheduled Failed ITEM_LOGIN_REQUIRED" in words


def test_no_runs_yet(db, client):
    linked_item(db)
    assert "No syncs yet" in text(main(page(client)))


def test_production_loads_plaid_only_when_an_update_mode_action_is_offered(db, prod_client):
    linked_item(db)
    healthy = prod_client.get("/connection")
    assert PLAID_SCRIPT not in healthy.text
    assert "plaid" not in healthy.headers["content-security-policy"]
    assert "sandbox-tools" not in healthy.text
    db.expire_all()
    from budgetapp.models import PlaidItem

    item = db.query(PlaidItem).one()
    item.status = "needs_reconnect"
    db.flush()
    broken = prod_client.get("/connection")
    assert PLAID_SCRIPT in broken.text
    assert (
        "connect-src 'self' https://production.plaid.com/;"
        in (broken.headers["content-security-policy"])
    )


# --------------------------------------------------------------------------- edits, attaching


def test_edits_that_found_no_replacement_are_listed(db, client):
    _, account = linked_item(db)
    groceries = get_category(db, "Groceries")
    state = userstate.UserState(category_id=groceries.id, notes="Party supplies", tags=("party",))
    make_tombstone(
        db,
        account,
        plaid_transaction_id="gone-1",
        merchant_name="Whole Foods",
        amount=Decimal("54.17"),
        removed_at=NOW - dt.timedelta(days=2),
        user_state=userstate.to_json(state),
    )
    html = main(page(client))
    lost = html[html.index('id="lost-edits"') :]
    words = text(lost)
    assert "Edits we couldn't carry over" in words
    assert "Whole Foods" in words and "$54.17" in words
    assert "Groceries · tags: party · “Party supplies”" in words
    assert 'hx-post="/connection/lost-edits/gone-1/dismiss"' in lost


def test_no_lost_edits_no_card(db, client):
    linked_item(db)
    assert 'id="lost-edits"' not in page(client)


def test_imported_csv_history_asks_which_card_it_belongs_to(db, client):
    item, card = linked_item(db)
    make_account(db, manual=True, name="Amex CSV", mask=None)
    make_account(db, manual=True, name="Old Gold CSV", mask=None)
    html = main(page(client))
    attach = html[html.index('id="attach"') :]
    assert '<form method="post" action="/connection/attach"' in attach
    assert "Amex CSV (0 imported transactions)" in attach
    assert f'<option value="{card.id}">Blue Cash Preferred ··3005</option>' in attach


# --------------------------------------------------------------------------- demo, keys


def test_demo_data_shows_the_page_but_never_reaches_plaid(db, demo_client):
    linked_item(db, consent_expires_at=NOW + dt.timedelta(days=10))
    response = demo_client.get("/connection")
    html = main(response.text)
    assert "Demo data." in text(html)
    assert PLAID_SCRIPT not in response.text
    assert "plaid" not in response.headers["content-security-policy"]
    assert re.search(r'data-link-mode="update" data-item-id="\d+" id="renew-\d+" disabled', html)
    assert re.search(
        r'<button class="btn" type="submit" id="page-sync-button" disabled><svg[^>]*>.*?</svg>'
        r"<span>Sync now",
        html,
        re.S,
    )


def test_missing_plaid_keys_are_explained_and_linking_disabled(make_app, make_client):
    def no_keys(_settings):
        raise app_secrets.MissingSecretError(app_secrets.PLAID_CLIENT_ID)

    client = make_client(make_app(gateway_factory=no_keys))
    response = client.get("/connection")
    html = main(response.text)
    assert "./run setup --keys-only" in text(html)
    assert re.search(r'data-link-mode="new" disabled', html)
    assert PLAID_SCRIPT not in response.text


# --------------------------------------------------------------------------- live region


def test_htmx_gets_only_the_live_region(db, client):
    linked_item(db)
    response = client.get("/connection", headers=HTMX)
    assert response.template.name == "connection/_live.html"
    assert response.text.lstrip().startswith('<div id="connection-live"')
    assert "<html" not in response.text and csp_problems(response.text, rendered=True) == []
    # the header's Sync now comes along out of band, enabled again once a sync has finished
    oob = '<form id="page-sync" method="post" action="/connection/sync" hx-swap-oob="true">'
    assert oob in response.text
    assert re.search(
        r'id="page-sync"[^>]*hx-swap-oob="true">\s*'
        r'<button class="btn" type="submit" id="page-sync-button">',
        response.text,
    )
    full = client.get("/connection").text
    assert '<form id="page-sync" method="post" action="/connection/sync">' in full
    assert full.count('id="page-sync"') == 1


def test_the_live_region_polls_while_a_sync_runs(db, client):
    item, _ = linked_item(db)
    make_sync_run(db, item, status="running", started_at=NOW - dt.timedelta(seconds=5))
    html = client.get("/connection").text
    live = '<div id="connection-live" class="stack stack--xl" hx-get="/connection"'
    assert f'{live} hx-trigger="every 4s, connection-changed from:body" hx-swap="outerHTML"' in html
    assert '<span class="pill pill--syncing">' in html
    assert re.search(r'<button class="btn" type="submit" id="page-sync-button" disabled>', html)


# --------------------------------------------------------------------------- review fixes


def test_the_paused_banner_explains_itself_in_plain_words_and_local_time(
    db, prod_client, prod_plaid, production, secrets
):
    """A tab that ran Link was reloaded or closed (no onExit): linking stays paused for up to
    4 hours, and the page says why and until when, in the Mac's time."""
    linking.new_link_token(db, prod_plaid, production, store=secrets)
    banner = text(main(page(prod_client)))
    unlocks = (NOW + app_secrets.LINK_TOKEN_LIFETIME).astimezone().strftime("%-I:%M %p")
    assert "Linking is paused." in banner
    assert "close the Plaid window there" in banner and f"linking unlocks at {unlocks}" in banner
    assert "UTC" not in banner and "marker" not in banner


@pytest.mark.parametrize(
    ("values", "ids"),
    [
        (
            {
                "status": "needs_reconnect",
                "error_code": "ITEM_LOGIN_REQUIRED",
                "consented_products": ["transactions"],
            },
            ("reconnect", "grant"),
        ),
        ({"consent_expires_at": NOW + dt.timedelta(days=5)}, ("renew",)),
    ],
    ids=["reconnect-and-grant", "renew"],
)
def test_every_control_of_the_live_region_keeps_one_id_across_polls(db, client, values, ids):
    """htmx puts keyboard focus back after a swap only on an element with the same id; without
    one, a keyboard user on Reconnect is thrown back to the page's start at every poll."""
    item, _ = linked_item(db, **values)
    partial = client.get("/connection?plaid=1", headers=HTMX)
    assert partial.template.name == "connection/_live.html"
    buttons = re.findall(r"<button[^>]*>", partial.text)
    assert buttons and all(' id="' in b for b in buttons), buttons
    for prefix in ids:
        assert f'id="{prefix}-{item.id}"' in partial.text
    assert 'id="page-sync-button"' in partial.text and 'id="page-sync-button"' in page(client)


def test_a_link_action_arriving_by_polling_reloads_a_page_without_plaid(
    db, prod_client, production, prod_plaid, secrets
):
    """A linked, healthy production page loads no Plaid script, and its CSP forbids loading one
    later. When a poll finds that Reconnect is needed, the page reloads (HX-Refresh) instead of
    swapping in a button that can't open Link."""
    fake = prod_plaid.add_item(item_id="item-amex")
    app_secrets.store_access_token(production, "item-amex", fake.access_token, store=secrets)
    item, _ = linked_item(db, plaid_item_id="item-amex")
    full = page(prod_client)
    assert PLAID_SCRIPT not in full and """hx-vals='{"plaid": "0"}'""" in full
    quiet = prod_client.get("/connection?plaid=0", headers=HTMX)
    assert quiet.template.name == "connection/_live.html" and "HX-Refresh" not in quiet.headers
    item.status, item.error_code = "needs_reconnect", "ITEM_LOGIN_REQUIRED"
    db.flush()
    swap = prod_client.get("/connection?plaid=0", headers=HTMX)
    assert swap.headers.get("HX-Refresh") == "true" and swap.text == ""
    reloaded = page(prod_client)
    assert PLAID_SCRIPT in reloaded and """hx-vals='{"plaid": "1"}'""" in reloaded
    polled = prod_client.get("/connection?plaid=1", headers=HTMX)
    assert polled.template.name == "connection/_live.html"
    assert re.search(r'data-link-mode="update" data-item-id="\d+" id="reconnect-\d+">', polled.text)


def test_the_link_dialog_shows_nothing_behind_its_modal(prod_client):
    """Errors and the one-time token box live in the page, outside the confirm dialog, and
    link.js closes the dialog before it shows anything; the dialog's opener shows the busy state
    while Link and the exchange run."""
    html = page(prod_client)
    dialog = re.search(r'<dialog[^>]*id="confirm-link".*?</dialog>', html, re.S).group(0)
    assert 'id="link-error"' not in dialog and 'id="link-token-rescue"' not in dialog
    assert 'id="link-token-rescue"' in html and "hidden" in re.search(
        r'<div[^>]*id="link-token-rescue"[^>]*>', html
    ).group(0)
    source = (Path(web.__file__).parent / "static" / "js" / "link.js").read_text()
    start = source[source.index("function start(button)") :]
    assert start.index("dialog.close()") < start.index("showError(")
    assert "setBusy([button, opener]" in start
