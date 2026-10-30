"""The Connection page's endpoints: Sync now and its status pill, the Plaid Link JSON endpoints
(link token, exchange, update mode, exit), test Items, re-attaching, the CSV attach and
dismissing lost edits — and the background jobs they queue (SYNC-1, SYNC-2, SYNC-5, SYNC-6).

Background jobs never run by themselves under pytest: ``app.state.jobs`` is a
``BackgroundJobs(start=False)``; a test runs the queue with ``jobs.run_all()`` in its own thread
(the test's connection is not shared across threads).
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import logging
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Account, PlaidItem, SyncRun, Transaction, TransactionTombstone
from budgetapp.services import linking, reconcile, userstate
from budgetapp.services.background import BackgroundJobs
from budgetapp.web import flash as flash_module
from budgetapp.web.routes import connection as routes
from tests.factories import make_account, make_item, make_sync_run, make_tombstone
from tests.fake_plaid import FakePlaidGateway
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
AMEX = {"institution": {"institution_id": "ins_10", "name": "American Express"}}


@pytest.fixture(autouse=True)
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def jobs(app) -> BackgroundJobs:
    """The app's job queue (made here the way the route makes it under pytest)."""
    queue = BackgroundJobs(start=False)
    app.state.jobs = queue
    return queue


@pytest.fixture
def production(tmp_path, monkeypatch):
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
def prod_app(make_app, production, prod_plaid):
    app = make_app(production, gateway_factory=lambda _settings: prod_plaid)
    app.state.jobs = BackgroundJobs(start=False)
    return app


@pytest.fixture
def prod_client(prod_app, make_client):
    return make_client(prod_app)


@pytest.fixture
def demo_client(make_app, make_client):
    app = make_app()
    app.state.demo_name = "preview"
    app.state.jobs = BackgroundJobs(start=False)
    return make_client(app)


def linked(db, fake_plaid, secrets, settings, *, item_id: str = "item-amex", **values):
    fake_item = fake_plaid.add_item(item_id=item_id)
    app_secrets.store_access_token(settings, item_id, fake_item.access_token, store=secrets)
    values.setdefault("last_sync_success_at", NOW - dt.timedelta(hours=3))
    item = make_item(db, plaid_item_id=item_id, **values)
    make_account(db, item, plaid_account_id=fake_item.default_account_id, mask="3005")
    return fake_item, item


def runs(db) -> list[SyncRun]:
    db.expire_all()
    return list(db.scalars(select(SyncRun).order_by(SyncRun.id)))


def pill(html: str) -> tuple[str, str]:
    match = re.search(r'class="pill pill--(\w+)".*?<span class="pill__text">([^<]*)<', html, re.S)
    assert match, "no status pill"
    return match.group(1), match.group(2)


def toasts(page: str) -> list[str]:
    return [
        html_lib.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', page)
    ]


def flashed(response) -> list[dict[str, str]]:
    """The toasts a response queued for the next full page (its flash cookie)."""
    cookie = response.cookies.get(flash_module.COOKIE_NAME)
    return flash_module.decode(cookie)


# --------------------------------------------------------------------------- Sync now


def test_sync_now_marks_the_run_and_answers_with_the_syncing_pill(
    db, client, jobs, fake_plaid, secrets, settings
):
    fake_item, item = linked(db, fake_plaid, secrets, settings)
    fake_plaid.add_transaction(fake_item, name="Coffee", amount=Decimal("4.50"))
    response = client.post("/connection/sync", headers=HTMX)
    assert response.status_code == 200
    assert response.template.name == "components/sync_status.html"
    assert pill(response.text) == ("syncing", "Syncing…")
    assert 'hx-get="/connection/sync-status" hx-trigger="every 4s"' in response.text
    assert toasts(response.text) == ["Sync started"]
    (run,) = runs(db)
    assert (run.trigger, run.status) == ("manual", "running")
    assert jobs.waiting_keys() == [f"refresh:{item.id}"]
    assert fake_plaid.calls_to("transactions_refresh") == []  # nothing ran in the request

    jobs.run_all()
    assert len(fake_plaid.calls_to("transactions_refresh")) == 1
    statuses = [r.status for r in runs(db)]
    assert statuses == ["success"] * 4  # now, then about 15 s, 45 s and 2 min later
    assert runs(db)[0].id == run.id and runs(db)[0].added_count == 1
    assert db.scalars(select(Transaction)).one().name == "Coffee"
    polled = client.get("/connection/sync-status", headers=HTMX)
    assert pill(polled.text) == ("healthy", "Synced just now")
    assert "hx-trigger" not in polled.text


def test_sync_now_without_javascript_redirects_to_the_page(
    db, client, jobs, fake_plaid, secrets, settings
):
    linked(db, fake_plaid, secrets, settings)
    response = client.post("/connection/sync", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/connection"
    assert flashed(response) == [{"level": "info", "text": "Sync started"}]
    page = client.get("/connection")
    assert '<span class="pill pill--syncing">' in page.text
    assert 'hx-get="/connection" hx-trigger="every 4s, connection-changed from:body"' in page.text


def test_sync_now_while_a_sync_runs_starts_nothing(db, client, jobs, fake_plaid, secrets, settings):
    _, item = linked(db, fake_plaid, secrets, settings)
    make_sync_run(db, item, status="running", started_at=NOW - dt.timedelta(seconds=10))
    response = client.post("/connection/sync", headers=HTMX)
    assert response.status_code == 200 and pill(response.text)[0] == "syncing"
    assert toasts(response.text) == ["A sync is already running."]
    assert jobs.waiting_keys() == [] and len(runs(db)) == 1


def test_sync_now_with_nothing_linked(client, jobs):
    response = client.post("/connection/sync", headers=HTMX)
    assert pill(response.text) == ("not_linked", "Not linked")
    assert toasts(response.text) == ["Nothing is linked yet. Link your card first."]
    assert jobs.waiting_keys() == []


def test_a_second_press_replaces_the_waiting_follow_ups(
    db, client, jobs, fake_plaid, secrets, settings
):
    _, item = linked(db, fake_plaid, secrets, settings)
    client.post("/connection/sync", headers=HTMX)
    jobs.run_due()  # the first sync ran; its follow-up waits about 15 s
    assert jobs.is_waiting(f"refresh:{item.id}")
    client.post("/connection/sync", headers=HTMX)
    assert jobs.waiting_keys() == [f"refresh:{item.id}"]
    jobs.run_all()
    assert len(fake_plaid.calls_to("transactions_refresh")) == 2


def test_sync_now_without_plaid_keys_says_how_to_add_them(
    db, make_app, make_client, fake_plaid, secrets, settings
):
    linked(db, fake_plaid, secrets, settings)

    def no_keys(_settings):
        raise app_secrets.MissingSecretError(app_secrets.PLAID_CLIENT_ID)

    app = make_app(gateway_factory=no_keys)
    app.state.jobs = BackgroundJobs(start=False)
    response = make_client(app).post("/connection/sync", headers=HTMX)
    assert response.status_code == 200
    assert "./run setup --keys-only" in toasts(response.text)[0]
    assert runs(db) == [] and app.state.jobs.waiting_keys() == []


def test_the_job_queue_is_made_on_first_use_and_never_runs_by_itself_under_pytest(
    db, app, client, fake_plaid, secrets, settings
):
    linked(db, fake_plaid, secrets, settings)
    assert getattr(app.state, "jobs", None) is None
    client.post("/connection/sync", headers=HTMX)
    queue = app.state.jobs
    assert isinstance(queue, BackgroundJobs) and queue._thread is None
    assert queue.waiting_keys() and fake_plaid.calls_to("transactions_refresh") == []


def test_a_stopped_queue_leaves_no_running_run(db, client, jobs, fake_plaid, secrets, settings):
    linked(db, fake_plaid, secrets, settings)
    jobs.shutdown()
    response = client.post("/connection/sync", headers=HTMX)
    assert "The app is stopping" in toasts(response.text)[0]
    assert [r.status for r in runs(db)] == ["interrupted"]
    assert pill(response.text)[0] != "syncing"


def test_demo_data_never_syncs(db, demo_client, fake_plaid, secrets, settings):
    linked(db, fake_plaid, secrets, settings)
    response = demo_client.post("/connection/sync", headers=HTMX)
    assert "Not available with demo data" in toasts(response.text)[0]
    assert runs(db) == [] and fake_plaid.calls == []


# --------------------------------------------------------------------------- link tokens


def test_a_sandbox_link_token(client, fake_plaid):
    response = client.post("/connection/link-token", json={"mode": "new"})
    assert response.status_code == 200
    assert response.json()["link_token"].startswith("link-sandbox-")
    assert fake_plaid.calls_to("create_link_token") == [
        {"access_token": None, "additional_consented_products": ()}
    ]


def test_update_mode_tokens_use_the_items_own_access_token(
    db, client, fake_plaid, secrets, settings
):
    fake_item, item = linked(db, fake_plaid, secrets, settings)
    assert client.post(
        "/connection/link-token", json={"mode": "update", "item_id": item.id}
    ).is_success
    assert client.post(
        "/connection/link-token", json={"mode": "liabilities", "item_id": str(item.id)}
    ).is_success
    assert fake_plaid.calls_to("create_link_token") == [
        {"access_token": fake_item.access_token, "additional_consented_products": ()},
        {"access_token": fake_item.access_token, "additional_consented_products": ("liabilities",)},
    ]


@pytest.mark.parametrize(
    ("body", "status", "words"),
    [
        ({"mode": "steal"}, 400, "isn't a kind of Plaid Link session"),
        ({}, 400, "isn't a kind of Plaid Link session"),
        ({"mode": "update"}, 400, "Which connection?"),
        ({"mode": "update", "item_id": True}, 400, "Which connection?"),
        ({"mode": "update", "item_id": 999999}, 409, "That connection no longer exists."),
    ],
)
def test_bad_link_token_requests(client, fake_plaid, body, status, words):
    response = client.post("/connection/link-token", json=body)
    assert response.status_code == status and words in response.json()["error"]
    assert fake_plaid.calls_to("create_link_token") == []


def test_a_link_token_request_that_is_not_json(client):
    response = client.post(
        "/connection/link-token", content=b"\xff\xfe", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


def test_an_item_whose_token_is_missing_from_keychain(db, client, fake_plaid):
    item = make_item(db, plaid_item_id="tokenless")
    response = client.post("/connection/link-token", json={"mode": "update", "item_id": item.id})
    assert response.status_code == 409
    assert "never link a new connection" in response.json()["error"]


def test_a_plaid_refusal_is_returned_as_json(client, fake_plaid):
    fake_plaid.queue_error("create_link_token", fake_plaid.plaid_error("INVALID_API_KEYS"))
    response = client.post("/connection/link-token", json={"mode": "new"})
    assert response.status_code == 502 and "INVALID_API_KEYS" in response.json()["error"]


def test_production_issues_one_new_link_token_under_the_guard(db, prod_client, prod_plaid, secrets):
    first = prod_client.post("/connection/link-token", json={"mode": "new"})
    assert first.status_code == 200 and first.json()["link_token"].startswith("link-production-")
    assert first.json()["attempt"]  # goes back with Link's exit report and the exchange
    assert app_secrets.get_link_in_progress(store=secrets) is not None
    second = prod_client.post("/connection/link-token", json={"mode": "new"})
    assert second.status_code == 409 and "may still be in progress" in second.json()["error"]
    assert len(prod_plaid.calls_to("create_link_token")) == 1


def test_production_refuses_a_new_link_while_an_item_exists(db, prod_client, prod_plaid, secrets):
    make_item(db)
    response = prod_client.post("/connection/link-token", json={"mode": "new"})
    assert response.status_code == 409 and "already linked" in response.json()["error"]
    assert prod_plaid.calls_to("create_link_token") == []


def test_demo_data_never_asks_plaid_for_a_token(demo_client, fake_plaid):
    response = demo_client.post("/connection/link-token", json={"mode": "new"})
    assert response.status_code == 409 and "demo data" in response.json()["error"]
    assert fake_plaid.calls == []


def test_link_tokens_need_the_apps_own_origin(raw_client, fake_plaid):
    response = raw_client.post(
        "/connection/link-token", json={"mode": "new"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403 and fake_plaid.calls == []


# --------------------------------------------------------------------------- exchange


def test_the_exchange_saves_the_connection_and_starts_the_backfill(
    db, client, jobs, fake_plaid, secrets
):
    fake_item = fake_plaid.add_item(item_id="new-item")
    fake_plaid.add_transaction(fake_item, name="Delta", amount=Decimal("1200.00"))
    public = fake_plaid.public_token_for(fake_item)
    response = client.post("/connection/exchange", json={"public_token": public, "metadata": AMEX})
    assert response.status_code == 200
    item = db.scalars(select(PlaidItem)).one()
    assert response.json() == {"redirect": "/connection", "item_id": item.id}
    (message,) = flashed(response)
    assert message["level"] == "success" and message["text"].startswith(
        "American Express is linked."
    )
    assert (
        secrets.values[app_secrets.sandbox_access_token_key("new-item")] == fake_item.access_token
    )
    (run,) = runs(db)
    assert (run.trigger, run.status) == ("link", "running")
    assert jobs.waiting_keys() == [f"backfill:{item.id}"]
    page = client.get("/connection")
    assert '<span class="pill pill--syncing">' in page.text  # says so at once
    assert toasts(page.text) == [message["text"]]

    jobs.run_all()
    assert [r.status for r in runs(db)] == ["success"]  # history was complete at once
    assert db.scalars(select(Transaction)).one().name == "Delta"


def test_the_exchange_never_sees_a_public_token_in_the_url_or_logs(
    db, client, jobs, fake_plaid, caplog
):
    caplog.set_level(logging.DEBUG)
    fake_item = fake_plaid.add_item(item_id="quiet")
    public = fake_plaid.public_token_for(fake_item)
    client.post("/connection/exchange", json={"public_token": public, "metadata": AMEX})
    assert public not in caplog.text and fake_item.access_token not in caplog.text


def test_an_exchange_plaid_refuses_is_explained(client, jobs, fake_plaid):
    fake_item = fake_plaid.add_item(item_id="limit")
    fake_plaid.queue_error(
        "exchange_public_token", fake_plaid.plaid_error("TRIAL_CONNECTION_LIMIT")
    )
    response = client.post(
        "/connection/exchange",
        json={"public_token": fake_plaid.public_token_for(fake_item), "metadata": AMEX},
    )
    assert response.status_code == 502 and "all 10 connections" in response.json()["error"]
    assert jobs.waiting_keys() == []


def test_an_exchange_without_a_public_token(client, jobs, fake_plaid):
    response = client.post("/connection/exchange", json={"metadata": AMEX})
    assert response.status_code == 409 and "didn't send a public token" in response.json()["error"]
    assert fake_plaid.calls_to("exchange_public_token") == []


def test_production_never_exchanges_a_second_link(db, prod_client, prod_plaid, secrets):
    make_item(db, institution_id="ins_10")
    fake_item = prod_plaid.add_item(item_id="second")
    response = prod_client.post(
        "/connection/exchange",
        json={"public_token": prod_plaid.public_token_for(fake_item), "metadata": AMEX},
    )
    assert response.status_code == 409 and "already linked" in response.json()["error"]
    assert prod_plaid.calls_to("exchange_public_token") == []


def test_a_production_link_clears_the_marker_and_blocks_the_next(
    db, prod_client, prod_plaid, secrets
):
    attempt = prod_client.post("/connection/link-token", json={"mode": "new"}).json()["attempt"]
    fake_item = prod_plaid.add_item(item_id="the-amex")
    response = prod_client.post(
        "/connection/exchange",
        json={
            "public_token": prod_plaid.public_token_for(fake_item),
            "metadata": AMEX,
            "attempt": attempt,
        },
    )
    assert response.status_code == 200
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert secrets.values[app_secrets.PRODUCTION_ACCESS_TOKEN] == fake_item.access_token
    again = prod_client.post("/connection/link-token", json={"mode": "new"})
    assert again.status_code == 409


def test_after_linking_a_lone_csv_account_is_attached(db, client, jobs, fake_plaid, monkeypatch):
    calls: list[tuple[int, int]] = []

    def attach(session, manual_id, plaid_account_id):
        calls.append((manual_id, plaid_account_id))
        return reconcile.AttachResult(moved_transactions=12)

    monkeypatch.setattr(reconcile, "attach_manual_account", attach)
    manual = make_account(db, manual=True)
    fake_item = fake_plaid.add_item(item_id="it")
    response = client.post(
        "/connection/exchange",
        json={"public_token": fake_plaid.public_token_for(fake_item), "metadata": AMEX},
    )
    card = db.scalars(select(Account).where(Account.item_id.is_not(None))).one()
    assert calls == [(manual.id, card.id)]
    assert (
        "Your imported history (12 transactions) now belongs to the card."
        in (flashed(response)[0]["text"])
    )


# --------------------------------------------------------------------------- update mode, exits


def test_update_mode_marks_the_item_healthy_and_syncs_twice(
    db, client, jobs, fake_plaid, secrets, settings
):
    fake_item, item = linked(
        db,
        fake_plaid,
        secrets,
        settings,
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
        last_consented_at=NOW - dt.timedelta(days=360),
    )
    fake_item.consent_expiration_time = "2027-10-20T00:00:00Z"
    response = client.post(
        "/connection/update-complete",
        json={"item_id": item.id, "mode": "update", "metadata": {"link_session_id": "s-1"}},
    )
    assert response.status_code == 200 and response.json()["redirect"] == "/connection"
    assert flashed(response)[0]["text"] == (
        "Your Amex connection is working again. Syncing now. Access is renewed until Oct 19, 2027."
    )
    db.expire_all()
    row = db.get(PlaidItem, item.id)
    assert (row.status, row.error_code, row.last_consented_at) == ("healthy", None, NOW)
    assert fake_plaid.calls_to("exchange_public_token") == []  # update mode never exchanges
    assert [(r.trigger, r.status) for r in runs(db)] == [("update", "running")]
    jobs.run_all()
    assert [(r.trigger, r.status) for r in runs(db)] == [("update", "success")] * 2


def test_granting_statement_details(db, client, jobs, fake_plaid, secrets, settings):
    fake_item, item = linked(db, fake_plaid, secrets, settings, consented_products=["transactions"])
    response = client.post(
        "/connection/update-complete", json={"item_id": item.id, "mode": "liabilities"}
    )
    assert flashed(response)[0]["text"].startswith("Statement details are now shared")
    db.expire_all()
    assert "liabilities" in db.get(PlaidItem, item.id).consented_products


def test_update_mode_for_an_unknown_item(client, jobs):
    assert client.post("/connection/update-complete", json={}).status_code == 400
    response = client.post("/connection/update-complete", json={"item_id": 999999})
    assert response.status_code == 409


def test_link_exit_is_logged_and_clears_its_own_production_marker(prod_client, secrets, caplog):
    caplog.set_level(logging.INFO, logger="budgetapp.services.linking")
    attempt = prod_client.post("/connection/link-token", json={"mode": "new"}).json()["attempt"]
    response = prod_client.post(
        "/connection/link-exit",
        json={
            "mode": "new",
            "attempt": attempt,
            "event": "EXIT",
            "status": "requires_oauth\x07",
            "error": {"error_code": "USER_CANCELLED"},
        },
    )
    assert response.status_code == 204
    assert app_secrets.get_link_in_progress(store=secrets) is None
    assert "closed without linking" in caplog.text and "\x07" not in caplog.text


def test_an_update_mode_exit_keeps_a_new_links_marker(prod_client, secrets):
    app_secrets.set_link_in_progress(store=secrets)
    assert prod_client.post("/connection/link-exit", json={"mode": "update"}).status_code == 204
    assert app_secrets.get_link_in_progress(store=secrets) is not None


# --------------------------------------------------------------------------- Sandbox, re-attach


def test_a_sandbox_test_item_without_link(db, client, jobs, fake_plaid):
    response = client.post(
        "/connection/sandbox-item", data={"user": "custom"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/connection"
    (call,) = fake_plaid.calls_to("sandbox_create_public_token")
    assert call["override_username"] == "user_custom" and call["institution_id"] == "ins_109508"
    item = db.scalars(select(PlaidItem)).one()
    assert flashed(response)[0]["text"] == (
        "Test Item created at First Platypus Bank. Syncing its history now."
    )
    assert jobs.waiting_keys() == [f"backfill:{item.id}"]


def test_an_unknown_test_user_falls_back_to_the_custom_card(client, jobs, fake_plaid):
    client.post("/connection/sandbox-item", data={"user": "user_admin"})
    assert (
        fake_plaid.calls_to("sandbox_create_public_token")[0]["override_username"] == "user_custom"
    )


def test_production_has_no_test_items(prod_client, prod_plaid):
    response = prod_client.post("/connection/sandbox-item", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert flashed(response) == [
        {"level": "error", "text": "Test Items exist only in the Sandbox."}
    ]
    assert prod_plaid.calls == []


def test_recovering_sandbox_items_the_database_lost(
    db, client, jobs, fake_plaid, secrets, settings
):
    fake_item = fake_plaid.add_item(item_id="orphan")
    app_secrets.store_access_token(settings, "orphan", fake_item.access_token, store=secrets)
    response = client.post("/connection/recover", follow_redirects=False)
    assert (
        flashed(response)[0]["text"] == "Re-attached 1 connection. Syncing from the beginning now."
    )
    item = db.scalars(select(PlaidItem)).one()
    assert item.plaid_item_id == "orphan" and item.transactions_cursor is None
    assert jobs.waiting_keys() == [f"backfill:{item.id}"]
    assert fake_plaid.calls_to("create_link_token") == []
    nothing = client.post("/connection/recover", follow_redirects=False)
    assert flashed(nothing)[0]["text"].startswith("Nothing to re-attach")


def test_production_re_attaches_the_saved_token(db, prod_client, prod_app, prod_plaid, secrets):
    fake_item = prod_plaid.add_item(item_id="restored")
    secrets.set(app_secrets.PRODUCTION_ACCESS_TOKEN, fake_item.access_token)
    response = prod_client.post("/connection/recover", follow_redirects=False)
    assert flashed(response)[0]["level"] == "success"
    assert db.scalars(select(PlaidItem)).one().plaid_item_id == "restored"
    assert prod_plaid.calls_to("exchange_public_token") == []


# --------------------------------------------------------------------------- attach, lost edits


def test_attaching_csv_history_to_a_card(db, client, monkeypatch):
    calls: list[tuple[int, int]] = []

    def attach(session, manual_id, plaid_account_id):
        calls.append((manual_id, plaid_account_id))
        return reconcile.AttachResult(moved_transactions=1)

    monkeypatch.setattr(reconcile, "attach_manual_account", attach)
    manual = make_account(db, manual=True)
    card = make_account(db)
    response = client.post(
        "/connection/attach",
        data={"manual_id": str(manual.id), "account_id": str(card.id)},
        follow_redirects=False,
    )
    assert calls == [(manual.id, card.id)]
    assert flashed(response) == [
        {"level": "success", "text": "Moved 1 imported transaction onto the card."}
    ]


def test_attaching_needs_both_accounts_and_the_csv_package(db, client):
    manual = make_account(db, manual=True)
    card = make_account(db)
    missing = client.post("/connection/attach", data={"manual_id": "x"}, follow_redirects=False)
    assert flashed(missing)[0]["level"] == "error"
    wrong = client.post(
        "/connection/attach",
        data={"manual_id": str(card.id), "account_id": str(card.id)},
        follow_redirects=False,
    )
    assert flashed(wrong) == [{"level": "error", "text": "That imported account no longer exists."}]
    stub = client.post(  # reconcile.attach_manual_account is the CSV package's
        "/connection/attach",
        data={"manual_id": str(manual.id), "account_id": str(card.id)},
        follow_redirects=False,
    )
    assert flashed(stub)[0]["level"] == "info"


def test_dismissing_lost_edits(db, client):
    card = make_account(db)
    for tid in ("gone-1", "gone-2"):
        make_tombstone(
            db,
            card,
            plaid_transaction_id=tid,
            removed_at=NOW - dt.timedelta(days=1),
            user_state=userstate.to_json(userstate.UserState(notes="Kept")),
        )
    response = client.post("/connection/lost-edits/gone-1/dismiss", headers=HTMX)
    assert response.status_code == 200
    assert response.template.name == "connection/_lost_edits.html"
    assert "gone-2" in response.text and "gone-1" not in response.text
    assert toasts(response.text) == ["Dismissed. Those edits won't be offered again."]
    assert csp_problems(response.text, rendered=True) == []
    last = client.post("/connection/lost-edits/gone-2/dismiss", headers=HTMX)
    assert 'id="lost-edits"' not in last.text  # the card goes away
    db.expire_all()
    assert db.get(TransactionTombstone, "gone-1").user_state is None
    assert client.post("/connection/lost-edits/gone-1/dismiss").status_code == 404


def test_dismissing_without_javascript(db, client):
    card = make_account(db)
    make_tombstone(
        db,
        card,
        plaid_transaction_id="gone-3",
        user_state=userstate.to_json(userstate.UserState(notes="Kept")),
    )
    response = client.post("/connection/lost-edits/gone-3/dismiss", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/connection"


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3), ("12", 12), (" 7 ", 7), (True, None), ("x", None), (None, None), (-2, -2)],
)
def test_ids_from_the_browser(value, expected):
    assert routes._int(value) == expected


def test_labels_from_the_browser_are_shortened_for_the_log():
    assert routes._short("CLOSE_OAUTH") == "CLOSE_OAUTH"
    assert routes._short("x" * 300) == "x" * 80
    assert routes._short("\n\t") is None and routes._short(5) is None


# --------------------------------------------------------------------------- review fixes


def test_close_oauth_reports_never_reopen_linking(prod_client, prod_plaid, secrets):
    """The Amex window closed (CLOSE_OAUTH) or timed out (FAIL_OAUTH): Link itself is still open
    and can still finish, so another tab must not get a new-link token."""
    attempt = prod_client.post("/connection/link-token", json={"mode": "new"}).json()["attempt"]
    for event in ("CLOSE_OAUTH", "FAIL_OAUTH"):
        report = prod_client.post(
            "/connection/link-exit", json={"mode": "new", "event": event, "attempt": attempt}
        )
        assert report.status_code == 204
    assert app_secrets.get_link_in_progress(store=secrets) is not None
    second = prod_client.post("/connection/link-token", json={"mode": "new"})  # another tab
    assert second.status_code == 409
    assert len(prod_plaid.calls_to("create_link_token")) == 1


def test_an_exit_report_of_another_attempt_leaves_the_marker(prod_client, secrets):
    prod_client.post("/connection/link-token", json={"mode": "new"})
    for body in (
        {"mode": "new", "event": "EXIT"},
        {"mode": "new", "event": "EXIT", "attempt": "not-this-one"},
    ):
        assert prod_client.post("/connection/link-exit", json=body).status_code == 204
    assert app_secrets.get_link_in_progress(store=secrets) is not None


def test_the_exchange_endpoint_refuses_another_bank_in_production(prod_client, prod_plaid, secrets):
    attempt = prod_client.post("/connection/link-token", json={"mode": "new"}).json()["attempt"]
    chase = prod_plaid.add_item(item_id="chase", institution_id="ins_3", institution_name="Chase")
    response = prod_client.post(
        "/connection/exchange",
        json={
            "public_token": prod_plaid.public_token_for(chase),
            "metadata": {"institution": {"institution_id": "ins_3", "name": "Chase"}},
            "attempt": attempt,
        },
    )
    assert response.status_code == 409 and "not American Express" in response.json()["error"]
    assert prod_plaid.calls_to("exchange_public_token") == []
    assert app_secrets.get_link_in_progress(store=secrets) is None


def test_a_parked_token_never_comes_back_in_the_exchange_answer(prod_client, prod_plaid, secrets):
    fake_item = prod_plaid.add_item(item_id="amex")
    secrets.fail_on[("set", app_secrets.PRODUCTION_ACCESS_TOKEN)] = app_secrets.SecretStoreError(
        "locked"
    )
    response = prod_client.post(
        "/connection/exchange",
        json={"public_token": prod_plaid.public_token_for(fake_item), "metadata": AMEX},
    )
    assert response.status_code == 502 and set(response.json()) == {"error"}
    assert fake_item.access_token not in response.text
    error = response.json()["error"]
    assert "readable only by you" in error and "--access-token" in error
    assert "Don't link again" in error and "show it to the user" not in error


def test_a_token_nothing_could_save_is_shown_once_by_the_exchange_answer(
    prod_client, prod_plaid, secrets, monkeypatch
):
    """The documented exception: Keychain and both recovery files failed, so the token exists
    only in this answer; link.js shows it once for the user to copy."""
    fake_item = prod_plaid.add_item(item_id="amex")
    secrets.fail_on[("set", app_secrets.PRODUCTION_ACCESS_TOKEN)] = app_secrets.SecretStoreError(
        "locked"
    )

    def unwritable(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(app_secrets, "write_access_token_recovery_file", unwritable)
    response = prod_client.post(
        "/connection/exchange",
        json={"public_token": prod_plaid.public_token_for(fake_item), "metadata": AMEX},
    )
    assert response.status_code == 502
    data = response.json()
    assert data["access_token"] == fake_item.access_token
    assert fake_item.access_token not in data["error"] and "password manager" in data["error"]


def test_sync_now_tells_the_connection_page_to_refresh(
    db, client, demo_client, jobs, fake_plaid, secrets, settings
):
    """The top-bar Sync now answers with the pill only; on /connection the live region listens
    for connection-changed, refreshes, and polls until the sync is done."""
    assert "HX-Trigger" not in client.post("/connection/sync", headers=HTMX).headers  # not linked
    linked(db, fake_plaid, secrets, settings)
    started = client.post("/connection/sync", headers=HTMX)
    assert started.headers["HX-Trigger"] == routes.CHANGED_EVENT == "connection-changed"
    already = client.post("/connection/sync", headers=HTMX)  # e.g. started from another tab
    assert already.headers["HX-Trigger"] == "connection-changed"
    assert "HX-Trigger" not in demo_client.post("/connection/sync", headers=HTMX).headers
    live = client.get("/connection?plaid=1", headers=HTMX).text
    assert 'hx-trigger="every 4s, connection-changed from:body"' in live


def test_a_second_re_attach_at_the_same_time_gets_a_plain_answer(client, monkeypatch):
    def busy(*_args, **_kwargs):
        raise linking.LinkBusy("Another window is re-attaching or linking right now.")

    monkeypatch.setattr(linking, "recover_orphaned_items", busy)
    response = client.post("/connection/recover", follow_redirects=False)
    assert response.status_code == 303
    assert flashed(response) == [
        {"level": "info", "text": "Another window is re-attaching or linking right now."}
    ]
