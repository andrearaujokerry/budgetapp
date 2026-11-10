"""The Connection page's states (SYNC-1, SYNC-5 … SYNC-7, BANK-1 … BANK-6): not linked (Sandbox
and production), linked and healthy, several connections (cards and bank accounts), reconnect
needed, consent warning, statement details not shared, sync errors, the recent runs, edits that
couldn't be carried over, the CSV-attach choice and demo data.

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
    assert "Plaid opens a secure window where you pick your bank or card company" in words
    assert "Checking, savings and credit cards from any bank Plaid supports." in words
    assert "Read-only. Nothing here can pay, move money or change your accounts." in words
    assert 'data-dialog-open="confirm-link"' in html  # Link a bank or card opens the dialog …
    dialog = html[html.index('<dialog id="confirm-link"') :]
    assert 'data-link-mode="new"' in dialog and "Link a bank or card" in dialog
    assert "data-needs-checklist" not in dialog  # … with no checklist in the Sandbox
    assert 'id="link-different-login"' in dialog  # BANK-3: the explicit choice
    assert '<form method="post" action="/connection/sandbox-item"' in html
    assert '<option value="custom" selected>' in html
    assert "1 of your 10" not in words  # Sandbox links are free
    assert PLAID_SCRIPT in response.text  # Link needs Plaid's script, with this page's nonce
    nonce = response.context["csp_nonce"]
    assert f'nonce="{nonce}"' in response.text
    assert response.headers["content-security-policy"] == security.plaid_link_csp(nonce, "sandbox")
    assert '<script src="/static/js/link.js" defer></script>' in response.text


SANDBOX_TEST_BANKS = [
    ("ins_109508", "First Platypus Bank"),
    ("ins_109511", "Tartan Bank"),
    ("ins_109509", "First Gingham Credit Union"),
    ("ins_109512", "Houndstooth Bank"),
]


def test_the_sandbox_names_a_test_bank_that_isnt_linked_yet(db, client):
    """ux-5: linking a test bank again is refused like a real bank (BANK-3: same institution,
    and with the different-login choice user_good's same account names and masks), so the
    dialog and the Sandbox tools name the first non-OAuth Sandbox bank (RESEARCH T3.1) that
    has no connection yet, never one already linked."""

    def advice(html: str) -> tuple[str, str]:
        dialog = html[html.index('<dialog id="confirm-link"') :]
        tools = re.search(r'<section[^>]*id="sandbox-tools".*?</section>', html, re.S)
        return text(dialog[: dialog.index("</dialog>")]), text(tools.group(0)) if tools else ""

    dialog, tools = advice(page(client))
    assert "pick First Platypus Bank and sign in with user_good and pass_good" in dialog
    assert tools == ""  # not linked: the Sandbox card says the same, untouched
    nexts = [name for _, name in SANDBOX_TEST_BANKS[1:]] + ["a test bank you haven't linked yet"]
    for (institution_id, name), suggested in zip(SANDBOX_TEST_BANKS, nexts, strict=True):
        item = make_item(db, institution_id=institution_id, institution_name=name,
                         consent_expires_at=None)  # fmt: skip
        make_account(db, item, name="Plaid Checking", mask="0000", type="depository",
                     subtype="checking")  # fmt: skip
        dialog, tools = advice(page(client))
        assert f"pick {suggested} and sign in with user_good and pass_good" in dialog, name
        assert f"pick {suggested} and sign in with user_good and pass_good" in tools, name
        assert f"pick {name}" not in dialog + tools
        assert "Linking a test bank again is refused, as it would be for a real bank." in tools
        assert "unless you tick" not in tools


def test_production_shows_the_checklist_and_confirms_the_one_time_link(prod_client):
    response = prod_client.get("/connection")
    html = main(page(prod_client))
    words = text(html)
    assert "Each bank you link uses 1 of your 10 Plaid connections." in words
    assert "0 of your 10 Plaid connections used." in words
    checklist = text(html[html.index("Before you link") : html.index('<dialog id="confirm-link"')])
    assert "American Express" not in checklist  # BANK-2: the checklist is about no one bank
    for label in (
        "A Data Transparency use case is selected",
        "Your team may use banks that sign in through their own window (OAuth)",
    ):
        assert words.count(label) == 2  # the checklist card and the confirm dialog's boxes
    assert html.count('href="https://dashboard.plaid.com/') == 2
    assert 'hx-get="/connection/preflight" hx-trigger="load"' in html
    assert 'data-dialog-open="confirm-link"' in html  # the page's button opens the dialog …
    dialog = html[html.index('<dialog id="confirm-link"') :]
    assert (
        "This uses 1 of your 10 Plaid connections, permanently. The app has created 0 of 10 so "
        "far. Make sure the checklist is done" in text(dialog)
    )
    # … whose Link button stays disabled until every box is ticked (static/js/link.js)
    assert re.search(r'data-link-mode="new" id="link-new" data-needs-checklist disabled', dialog)
    assert "A different login at a bank I've already linked" in text(dialog)
    assert "/connection/sandbox-item" not in html
    assert (
        "connect-src 'self' https://production.plaid.com/;"
        in (response.headers["content-security-policy"])
    )


def test_production_blocks_a_new_link_while_one_is_in_progress(prod_client, secrets):
    app_secrets.set_link_in_progress(store=secrets)
    html = main(page(prod_client))
    assert "Linking is paused." in text(html)
    assert re.search(r'data-dialog-open="confirm-link" id="link-open" disabled', html)


def test_production_offers_to_re_attach_a_saved_token_instead_of_a_new_link(prod_client, secrets):
    secrets.set(app_secrets.LEGACY_PRODUCTION_TOKEN, "access-production-kept")
    html = main(page(prod_client))
    assert "A saved connection was found." in text(html)
    assert '<form method="post" action="/connection/recover">' in html
    assert re.search(r'data-dialog-open="confirm-link" id="link-open" disabled', html)


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
    assert "Plaid answers with your Production keys" in response.text
    assert prod_plaid.calls_to("search_institutions") == [
        {"query": "Bank", "products": ("transactions",)}
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
    assert "Plaid's last update from American Express Oct 20, 2026," in words
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
    # BANK-5: the connection's own actions, each a Plaid update-mode session (no new connection)
    actions = re.search(r'<div class="cluster connection-actions">.*?</div>', html, re.S).group(0)
    assert f'data-link-mode="accounts" data-item-id="{item.id}" id="accounts-{item.id}"' in actions
    assert f'id="renew-{item.id}"' in actions and f'id="reconnect-{item.id}"' in actions
    assert "Reconnect needed." not in words and "Renew by" not in actions
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
    words = text(main(page(client)))
    assert "American Express needs you to sign in again through Plaid" in words


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
        ("unsupported", "American Express doesn't provide them"),
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
    assert "American Express isn't answering Plaid right now" in words


def test_rejected_plaid_keys_say_how_to_fix_them(db, client):
    """Final review ops-4: the page promised a retry that can't succeed."""
    linked_item(db, status="error", error_code="INVALID_API_KEYS")
    words = text(main(page(client)))
    assert "The last sync failed (INVALID_API_KEYS)." in words
    assert "./run setup --keys-only" in words
    assert "the next one tries again" not in words


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


def test_production_loads_plaid_whenever_the_page_offers_a_link_action(
    db, prod_client, make_app, make_client, production
):
    """BANK-5: every connection offers Add accounts and Reconnect, so a linked page always loads
    Plaid's script (with its CSP); a page that can't open Link (no keys yet, demo) never does."""
    linked_item(db)
    healthy = prod_client.get("/connection")
    assert PLAID_SCRIPT in healthy.text
    assert (
        "connect-src 'self' https://production.plaid.com/;"
        in (healthy.headers["content-security-policy"])
    )
    assert "sandbox-tools" not in healthy.text

    def no_keys(_settings):
        raise app_secrets.MissingSecretError(app_secrets.PLAID_CLIENT_ID)

    keyless = make_client(make_app(production, gateway_factory=no_keys)).get("/connection")
    assert PLAID_SCRIPT not in keyless.text
    assert "plaid" not in keyless.headers["content-security-policy"]


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
    assert "Keychain item" not in text(html)  # demo data never saves a token
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
    assert re.search(r'data-dialog-open="confirm-link" id="link-open" disabled', html)
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
        ({"consent_expires_at": NOW + dt.timedelta(days=5)}, ("renew", "accounts")),
        ({}, ("accounts", "renew", "reconnect")),
    ],
    ids=["reconnect-and-grant", "renew", "actions"],
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
    db, make_app, make_client, production, prod_plaid
):
    """A page loaded while Link couldn't open (the Plaid keys weren't saved yet) has no Plaid
    script, and its CSP forbids loading one later. Once a poll finds Link actions available
    (Reconnect, Add accounts), the page reloads (HX-Refresh) instead of swapping in buttons that
    can't open Link."""
    keys = {"saved": False}

    def gateway_factory(_settings):
        if not keys["saved"]:
            raise app_secrets.MissingSecretError(app_secrets.PLAID_CLIENT_ID)
        return prod_plaid

    client = make_client(make_app(production, gateway_factory=gateway_factory))
    item, _ = linked_item(db)
    full = page(client)
    assert PLAID_SCRIPT not in full and """hx-vals='{"plaid": "0"}'""" in full
    quiet = client.get("/connection?plaid=0", headers=HTMX)
    assert quiet.template.name == "connection/_live.html" and "HX-Refresh" not in quiet.headers
    keys["saved"] = True
    swap = client.get("/connection?plaid=0", headers=HTMX)
    assert swap.headers.get("HX-Refresh") == "true" and swap.text == ""
    reloaded = page(client)
    assert PLAID_SCRIPT in reloaded and """hx-vals='{"plaid": "1"}'""" in reloaded
    polled = client.get("/connection?plaid=1", headers=HTMX)
    assert polled.template.name == "connection/_live.html"
    assert f'data-link-mode="update" data-item-id="{item.id}" id="reconnect-{item.id}"' in (
        polled.text
    )


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


# ---------------------------------------------------------- W3-BANK review fixes (safety, UX)


def test_a_v1_copy_left_behind_still_lets_the_owner_link_another_bank(
    db, prod_client, prod_plaid, secrets
):
    """Safety review (major): the v1 token moved, but its old copy couldn't be deleted (the web
    agent runs without Keychain prompts). The page offers Link, and clicking it works (it used
    to be refused with a reason that blamed a failed save)."""
    fake = prod_plaid.add_item(item_id="item-amex")
    item = make_item(db, plaid_item_id="item-amex")
    make_account(db, item, plaid_account_id=fake.default_account_id, mask="1005")
    secrets.values[app_secrets.production_access_token_key("item-amex")] = fake.access_token
    secrets.values[app_secrets.PRODUCTION_ITEM_INDEX] = '["item-amex"]'
    legacy = app_secrets.LEGACY_PRODUCTION_TOKEN
    secrets.values[legacy] = fake.access_token
    secrets.fail_on[("delete", legacy)] = app_secrets.KeychainApprovalNeeded(legacy, -25308)
    html = main(page(prod_client))
    assert re.search(r'data-dialog-open="confirm-link" id="link-open">', html)
    assert "Linking is paused." not in text(html) and "A saved connection" not in text(html)
    answer = prod_client.post("/connection/link-token", json={"mode": "new"})
    assert answer.status_code == 200 and answer.json()["attempt"]


def test_an_account_plaid_no_longer_shares_is_marked_on_its_tile(
    db, client, fake_plaid, secrets, settings, session_factory, notifier
):
    """UX review: an account unticked in Add accounts dropped out of Plaid's answers but still
    looked live. After the next sync its tile says so, and the summary counts it apart."""
    from budgetapp.services import sync as sync_service
    from tests.factories import plaid_account

    def bank_account(account_id, name, mask, subtype, current):
        return plaid_account(
            account_id=account_id,
            name=name,
            official_name=None,
            mask=mask,
            type="depository",
            subtype=subtype,
            current=current,
            available=current,
        )

    fake = fake_plaid.add_item(
        item_id="tartan",
        institution_id="ins_109511",
        institution_name="Tartan Bank",
        consented_products=("transactions",),
        accounts=[
            bank_account("tartan-chk", "Checking", "4417", "checking", "100.00"),
            bank_account("tartan-sav", "Savings", "9902", "savings", "5000.00"),
        ],
    )
    result = linking.exchange(
        session_factory,
        fake_plaid,
        secrets,
        settings,
        fake_plaid.public_token_for(fake),
        {"institution": {"institution_id": "ins_109511", "name": "Tartan Bank"}},
    )

    def sync_tartan():
        sync_service.sync_item(
            session_factory,
            fake_plaid,
            secrets,
            settings,
            result.item_id,
            trigger="update",
            notifier=notifier,
            sleep=lambda _s: None,
        )

    sync_tartan()
    fake.accounts = [a for a in fake.accounts if a["account_id"] != "tartan-sav"]
    sync_tartan()
    html = main(page(client))
    words = text(html)
    assert "1 bank account (1 no longer shared) · linked" in words
    tile = html[html.index('id="account-') :]
    tile = text(tile[tile.index("Savings ··9902") :])
    assert tile.startswith("Savings ··9902 Savings No longer shared. Plaid stopped sharing")
    assert "the balance shown is the last one Plaid sent" in tile


def test_each_connection_names_its_keychain_item(db, prod_client, production, secrets):
    """UX review: going-live.md asks for a copy of each connection's token; the page says which
    Keychain item is which connection (its name only, never the token)."""
    for pid, name, inst in (
        ("item-amex", "American Express", "ins_10"),
        ("item-tartan", "Tartan Bank", "ins_109511"),
    ):
        make_item(db, plaid_item_id=pid, institution_name=name, institution_id=inst)
        app_secrets.store_access_token(production, pid, f"access-production-{pid}", store=secrets)
    html = main(page(prod_client))
    for pid in ("item-amex", "item-tartan"):
        assert f'<code class="keychain-item">plaid.access_token.production.{pid}</code>' in html
    assert "access-production-item-" not in html


def test_two_logins_at_one_bank_have_their_own_titles(db, client):
    first = make_item(db, institution_name="Chase", institution_id="ins_3")
    make_account(db, first, name="Sapphire Preferred", mask="1234")
    second = make_item(db, institution_name="Chase", institution_id="ins_3")
    make_account(db, second, name="Freedom Unlimited", mask="5678")
    html = main(page(client))
    assert f'id="item-{first.id}-title">Chase (Sapphire Preferred ··1234)</h2>' in html
    assert f'id="item-{second.id}-title">Chase (Freedom Unlimited ··5678)</h2>' in html
    assert "Plaid's last update from Chase" in text(html)  # sentences name the bank itself
