"""The web shell: pages and layout, ``render()``, flash messages, error pages, deps and filters.

Templates render with ``StrictUndefined`` under pytest, so every page here also proves that
``base.html`` and the sync partial read only fields that exist on the real objects.

Feature packages replace the placeholder pages, so probe routes here render only templates the
shell owns (``errors/*``, ``components/*``, ``dev/ui_kit.html``), and the real pages are checked
only for what every page must keep: it renders with an empty database, inside the full layout.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import jinja2
import pytest
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy import exc as sa_exc
from sqlalchemy import select

from budgetapp import __version__
from budgetapp import db as app_db
from budgetapp.config import load_settings
from budgetapp.db import DatabaseUnavailable, SchemaMismatch
from budgetapp.models import Category
from budgetapp.plaid_gateway import PlaidError, PlaidTransportError
from budgetapp.secrets import KeychainApprovalNeeded, MissingSecretError
from budgetapp.services.status import ConnectionStatus
from budgetapp.web import flash as flash_module
from budgetapp.web.app import STATIC_DIR
from budgetapp.web.deps import FormDep, GatewayDep, NotifierDep, SessionDep, SettingsDep
from budgetapp.web.flash import flash, flash_oob, is_local_path, redirect
from budgetapp.web.routes import ALL_ROUTERS
from budgetapp.web.routes import connection as connection_routes
from budgetapp.web.templating import (
    NAV_ITEMS,
    date_long,
    date_short,
    is_htmx,
    make_environment,
    money_filter,
    money_signed,
    month_label,
    pct,
    pluralize,
    relative_time,
    render,
    templates,
)
from tests.factories import make_item, make_sync_run, set_app_settings
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

NOW = dt.datetime(2026, 10, 15, 16, 0, tzinfo=dt.UTC)

# --------------------------------------------------------------------------- probe routes

probe = APIRouter()


def probe_page(request: Request, context: dict | None = None, *, nav: str | None = "budgets"):
    """A full page that only the shell owns (the generic notice page)."""
    page = {
        "title": "Probe page",
        "message": "A full page for the shell's tests.",
        **(context or {}),
    }
    return render(request, "errors/error.html", page, active_nav=nav)


@probe.get("/_probe/page")
def page(request: Request) -> Response:
    return probe_page(request)


@probe.post("/_probe/saved")
def saved(request: Request) -> Response:
    return redirect(request, "/_probe/page", "success", "Rule saved")


@probe.post("/_probe/two-messages")
def two_messages() -> Response:
    response = RedirectResponse("/_probe/page", status_code=303)
    flash(response, "success", "First")
    return flash(response, "error", "Second <b>bold</b>")


@probe.get("/_probe/status")
def status_page_or_partial(request: Request) -> Response:
    """Full page normally; the sync partial plus a toast for HTMX (the SYNC route shape)."""
    if is_htmx(request):
        return render(
            request,
            "components/sync_status.html",
            active_nav="connection",
            oob=flash_oob("success", "Sync started"),
        )
    return probe_page(request, nav="connection")


@probe.get("/_probe/bad-nav")
def bad_nav(request: Request) -> Response:
    return probe_page(request, nav="dashbord")


@probe.get("/_probe/no-conn")
def no_conn(request: Request) -> Response:
    return probe_page(request, {"conn": None})


FAILURES = {
    "schema": lambda: SchemaMismatch("0002_seed_defaults", "0003_f1_verification_fixes"),
    "keychain": lambda: KeychainApprovalNeeded("db.password.budgetapp_live", -25308),
    "plaid-keys": lambda: MissingSecretError("plaid.client_id"),
    "db-password": lambda: MissingSecretError("db.password.budgetapp_dev"),
    "database": lambda: DatabaseUnavailable("refused; access-sandbox-0000 in the detail"),
    "operational": lambda: sa_exc.OperationalError("SELECT 1", {}, Exception("server closed")),
    "plaid": lambda: PlaidError(400, "ITEM_ERROR", "X", "internal detail", "Amex is busy."),
    "plaid-network": lambda: PlaidTransportError("timed out talking to Plaid"),
    "conflict": lambda: HTTPException(409),
    "custom": lambda: HTTPException(400, "That budget amount isn't a number."),
}


@probe.get("/_probe/fail/{what}")
def fail(what: str) -> Response:
    raise FAILURES[what]()


@probe.get("/_probe/number")
def number(n: int) -> Response:
    return PlainTextResponse(str(n))


@probe.post("/_probe/write")
def write(session: SessionDep, fail: bool = False) -> Response:
    session.add(Category(name="Probe category", kind="expense", color="cat-3"))
    session.flush()
    if fail:
        raise RuntimeError("failed after writing")
    session.commit()
    return PlainTextResponse("saved")


@probe.post("/_probe/form")
def posted_form(form: FormDep) -> Response:
    """A user-facing form: text in, validated by the route, 422 with the problem otherwise."""
    name = form.get("name", "").strip()
    if not name:
        return PlainTextResponse("Give the category a name.", status_code=422)
    upload = form.get("file")
    attached = f" {upload.filename}={upload.file.read().decode()}" if upload else ""
    return PlainTextResponse(f"{name} {form.getlist('tag')}{attached}")


@probe.get("/_probe/deps")
def deps(gateway: GatewayDep, notifier: NotifierDep, settings: SettingsDep) -> Response:
    return PlainTextResponse(f"{type(gateway).__name__} {type(notifier).__name__} {settings.env}")


@pytest.fixture
def probe_app(app):
    app.include_router(probe)
    return app


@pytest.fixture
def probe_client(probe_app, make_client):
    return make_client(probe_app)


@pytest.fixture
def sandbox_client(make_app, make_client):
    """The app with the Sandbox settings ``./run`` serves (still on the test database)."""
    app = make_app(load_settings("sandbox"))
    app.include_router(probe)
    return make_client(app)


@pytest.fixture
def at_now(freeze_today):
    freeze_today(now=NOW)


# --------------------------------------------------------------------------- helpers


def current_links(html: str) -> list[str]:
    """The hrefs of nav links marked ``aria-current="page"`` (sidebar + mobile menu)."""
    return re.findall(r'<a class="nav__link" href="([^"]*)" aria-current="page">', html)


def nav_links(html: str) -> list[str]:
    return re.findall(r'<a class="nav__link" href="([^"]*)"', html)


def pill(html: str) -> tuple[str, str]:
    """(state, text) of the sync-status pill."""
    match = re.search(r'class="pill pill--(\w+)".*?<span class="pill__text">([^<]*)<', html, re.S)
    assert match, "no sync-status pill"
    return match.group(1), match.group(2)


def toast_texts(html: str) -> list[str]:
    return re.findall(r'<div class="toast__text">([^<]*)</div>', html)


# --------------------------------------------------------------------------- pages and layout


def test_routers_follow_the_nav_order():
    assert len(ALL_ROUTERS) == len(NAV_ITEMS) + 1  # + dev
    for router, item in zip(ALL_ROUTERS, [*NAV_ITEMS, {"href": "/dev/ui-kit"}], strict=True):
        pages = [r for r in router.routes if r.path == item["href"] and "GET" in r.methods]
        assert pages, f"{item['href']} must be served by router #{ALL_ROUTERS.index(router)}"


@pytest.mark.parametrize("item", NAV_ITEMS, ids=lambda item: item["key"])
def test_every_nav_page_renders_with_the_layout(sandbox_client, item):
    """Every page, placeholder or built: 200 on an empty database, inside the full layout."""
    response = sandbox_client.get(item["href"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.context["active_nav"] == item["key"]
    html = response.text
    assert nav_links(html) == [i["href"] for i in NAV_ITEMS] * 2  # sidebar + mobile menu
    assert current_links(html) == [item["href"], item["href"]]
    assert '<div class="env-bar env-bar--sandbox" role="note">' in html
    assert "<strong>Sandbox</strong> — test data" in html
    assert re.search(r"<title>[^<]+ · Budget Tracker \(Sandbox\)</title>", html)
    assert html.count('<h1 class="page-header__title">') == 1
    assert '<meta name="htmx-config" content=\'{"includeIndicatorStyles":false}\'>' in html
    assert '<script src="/static/vendor/htmx.min.js" defer></script>' in html
    assert '<script src="/static/js/app.js" defer></script>' in html
    assert csp_problems(html, rendered=True) == []  # macro-built attributes included


PLACEHOLDER = "Placeholder from the web shell"


@pytest.mark.parametrize("item", NAV_ITEMS, ids=lambda item: item["key"])
def test_placeholder_pages_say_coming_soon(client, item):
    template = templates.env.loader.get_source(templates.env, f"{item['key']}/index.html")[0]
    if PLACEHOLDER not in template:
        pytest.skip("built by its feature package")
    response = client.get(item["href"])
    assert response.template.name == f"{item['key']}/index.html"
    assert f'<h1 class="page-header__title">{item["label"]}</h1>' in response.text
    assert "Coming soon" in response.text


def test_render_adds_the_layout_context(probe_client):
    context = probe_client.get("/_probe/page").context
    assert context["env"] == "test"
    assert context["nav_items"] == NAV_ITEMS
    assert context["active_nav"] == "budgets"
    assert isinstance(context["conn"], ConnectionStatus)
    assert context["conn"].state == "not_linked"  # pages render before anything is linked
    assert context["messages"] == []
    assert context["app_version"] == __version__
    assert context["csp_nonce"] and context["request"].url.path == "/_probe/page"
    assert context["title"] == "Probe page"  # the route's own context is kept


def test_render_rejects_an_unknown_nav_key(probe_client):
    with pytest.raises(ValueError, match="unknown active_nav 'dashbord'"):
        probe_client.get("/_probe/bad-nav")


def test_conn_none_hides_the_sync_status(probe_client):
    html = probe_client.get("/_probe/no-conn").text
    assert 'id="sync-status"' not in html and 'class="notices"' not in html


@pytest.mark.parametrize(
    ("app_env", "bar", "brand", "suffix"),
    [
        ("test", '<div class="env-bar env-bar--test"', 'brand__env--test">Test', " (Test)"),
        ("sandbox", '<div class="env-bar env-bar--sandbox"', '--sandbox">Sandbox', " (Sandbox)"),
        ("production", None, 'brand__env--production">Live', ""),
        ("demo", '<div class="env-bar env-bar--demo"', 'brand__env--demo">Demo', " (Demo)"),
    ],
)
def test_every_environment_is_labelled(make_app, make_client, app_env, bar, brand, suffix):
    app = make_app(load_settings("test" if app_env == "demo" else app_env))
    if app_env == "demo":
        app.state.demo_name = "shell"  # what serve --demo sets
    response = make_client(app).get("/no-such-page")
    html = response.text
    assert response.context["env"] == app_env
    assert brand in html
    assert f"<title>Page not found · Budget Tracker{suffix}</title>" in html
    if bar is None:
        assert "env-bar" not in html
    else:
        assert bar in html
    if app_env == "demo":
        assert "<strong>Demo data</strong> — nothing here is real" in html


def test_unknown_pages_get_the_404_page_with_the_layout(client):
    response = client.get("/no-such-page")
    assert response.status_code == 404
    assert response.template.name == "errors/404.html"
    assert response.text.count("<h1") == 1
    assert '<h1 class="empty__title">Page not found</h1>' in response.text
    assert "env-bar--test" in response.text
    assert len(nav_links(response.text)) == 2 * len(NAV_ITEMS)
    assert current_links(response.text) == []


def test_wrong_method_gets_a_405_page(probe_client):
    response = probe_client.post("/_probe/page")
    assert response.status_code == 405
    assert response.template.name == "errors/error.html"
    assert response.text.count("<h1") == 1
    assert '<h1 class="empty__title">Not allowed here</h1>' in response.text
    assert '<div class="empty__icon empty__icon--warn">' in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("vendor/htmx.min.js", "text/javascript"),
        ("vendor/chart.umd.min.js", "text/javascript"),
        ("js/app.js", "text/javascript"),
        ("css/app.css", "text/css"),
        ("img/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_and_vendored_files_are_served(raw_client, path, content_type):
    response = raw_client.get(f"/static/{path}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.content == (STATIC_DIR / path).read_bytes()


@pytest.mark.parametrize("path", ["/static/nope.js", "/static/%2e%2e/app.py", "/static/js/"])
def test_missing_static_files_are_a_plain_404(raw_client, path):
    response = raw_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")


def test_the_ui_kit_renders_outside_production(client):
    response = client.get("/dev/ui-kit")
    assert response.status_code == 200 and response.template.name == "dev/ui_kit.html"
    assert current_links(response.text) == []
    assert csp_problems(response.text, rendered=True) == []


def test_the_ui_kit_is_never_served_in_production(make_app, make_client):
    response = make_client(make_app(load_settings("production"))).get("/dev/ui-kit")
    assert response.status_code == 404
    assert response.template.name == "errors/404.html"


# --------------------------------------------------------------------------- connection states


def test_not_linked(probe_client, at_now):
    html = probe_client.get("/_probe/page").text
    assert pill(html) == ("not_linked", "Not linked")
    assert '<a class="btn btn--sm btn--primary" href="/connection">' in html
    assert "Link account" in html and 'class="notices"' not in html


def test_healthy(probe_client, db, at_now):
    make_item(
        db,
        linked_at=NOW - dt.timedelta(days=30),
        last_sync_success_at=NOW - dt.timedelta(minutes=5),
    )
    html = probe_client.get("/_probe/page").text
    assert pill(html) == ("healthy", "Synced 5 min ago")
    assert 'data-tooltip="American Express via Plaid"' in html
    assert 'hx-post="/connection/sync"' in html and 'action="/connection/sync"' in html
    assert 'class="notices"' not in html and 'hx-trigger="every 4s"' not in html


def test_syncing_polls_and_disables_sync_now(probe_client, db, at_now):
    item = make_item(db, last_sync_success_at=NOW - dt.timedelta(hours=6))
    make_sync_run(db, item, status="running", started_at=NOW - dt.timedelta(minutes=1))
    html = probe_client.get("/_probe/page").text
    assert pill(html) == ("syncing", "Syncing…")
    assert 'hx-get="/connection/sync-status" hx-trigger="every 4s"' in html
    assert 'hx-post="/connection/sync"' not in html


def test_stale(probe_client, db, at_now):
    make_item(
        db, linked_at=NOW - dt.timedelta(days=30), last_sync_success_at=NOW - dt.timedelta(hours=40)
    )
    assert pill(probe_client.get("/_probe/page").text) == ("stale", "Synced 1 day ago")


def test_sync_error(probe_client, db, at_now):
    make_item(db, status="error", error_code="INSTITUTION_DOWN", error_message="Amex is down")
    html = probe_client.get("/_probe/page").text
    assert pill(html) == ("error", "Sync problem")
    assert 'data-tooltip="Amex is down"' in html and 'class="notices"' not in html


def test_needs_reconnect_shows_the_banner(probe_client, db, at_now):
    make_item(
        db,
        status="needs_reconnect",
        error_code="ITEM_LOGIN_REQUIRED",
        consent_expires_at=NOW - dt.timedelta(days=1),  # waits behind the reconnect banner
    )
    html = probe_client.get("/_probe/page").text
    assert pill(html) == ("needs_reconnect", "Reconnect needed")
    assert '<div class="banner banner--over" role="alert" id="reconnect-banner">' in html
    assert 'id="consent-banner"' not in html


@pytest.mark.parametrize(
    ("offset", "tone", "words"),
    [
        (dt.timedelta(days=20), "warn", "(20 days left)"),
        (dt.timedelta(0), "over", "Your Amex consent expires today."),
        (dt.timedelta(days=-3), "over", "Your Amex consent has expired."),
    ],
    ids=["due-soon", "today", "expired"],
)
def test_consent_banner(probe_client, db, at_now, offset, tone, words):
    set_app_settings(db, consent_reminder_days=30)
    due = NOW + offset
    make_item(db, consent_expires_at=due)
    html = probe_client.get("/_probe/page").text
    assert f'<div class="banner banner--{tone}" role="' in html and 'id="consent-banner"' in html
    assert words in html
    if offset:
        assert date_long(due.astimezone().date()) in html
    assert ("Renew access" in html) == (offset < dt.timedelta(0))


@pytest.mark.parametrize(
    "item",
    [
        {"consent_expires_at": NOW + dt.timedelta(days=20)},
        {"status": "needs_reconnect", "error_code": "ITEM_LOGIN_REQUIRED"},
    ],
    ids=["consent", "reconnect"],
)
def test_the_connection_page_shows_no_layout_banners(probe_client, db, at_now, item):
    """The Connection page carries Reconnect / Renew itself; a banner would link to itself."""
    set_app_settings(db, consent_reminder_days=30)
    make_item(db, **item)
    assert 'class="notices"' in probe_client.get("/_probe/page").text  # nav "budgets"
    on_connection = probe_client.get("/_probe/status").text  # nav "connection"
    assert 'class="notices"' not in on_connection and 'id="sync-status"' in on_connection


def test_two_items_show_the_worst(probe_client, db, at_now):
    make_item(db, last_sync_success_at=NOW - dt.timedelta(minutes=1))
    make_item(db, status="error", error_code="INTERNAL_SERVER_ERROR")
    assert pill(probe_client.get("/_probe/page").text) == ("error", "Sync problem")


def test_htmx_gets_the_partial_with_an_out_of_band_toast(probe_client, db, at_now):
    make_item(db, last_sync_success_at=NOW - dt.timedelta(hours=3))
    response = probe_client.get("/_probe/status", headers=HTMX)
    body = response.text.strip()
    assert response.template.name == "components/sync_status.html"
    assert body.startswith('<div id="sync-status" class="sync-status">')
    assert "<html" not in body and "env-bar" not in body
    assert pill(body) == ("healthy", "Synced 3 h ago")
    assert '<div hx-swap-oob="beforeend:#flash">' in body
    assert toast_texts(body) == ["Sync started"]
    assert "HX-Request" in response.headers["vary"]


@pytest.fixture
def sync_placeholder():
    """The placeholder's toast text, as it appears in HTML."""
    if not hasattr(connection_routes, "NOT_BUILT"):
        pytest.skip("routes/connection.py is built by the SYNC package")
    return str(Markup.escape(connection_routes.NOT_BUILT))


def test_sync_now_answers_until_sync_is_built(client, db, at_now, sync_placeholder):
    """The top-bar button on every page posts here: never a 404 ("That item no longer exists")."""
    make_item(db, last_sync_success_at=NOW - dt.timedelta(hours=3))
    response = client.post("/connection/sync", headers=HTMX)
    assert response.status_code == 200
    assert response.template.name == "components/sync_status.html"
    assert pill(response.text) == ("healthy", "Synced 3 h ago")
    assert toast_texts(response.text) == [sync_placeholder]
    plain = client.post("/connection/sync", follow_redirects=False)  # no JS: the form posts
    assert plain.status_code == 303 and plain.headers["location"] == "/connection"
    assert toast_texts(client.get("/connection").text) == [sync_placeholder]
    polled = client.get("/connection/sync-status", headers=HTMX)
    assert polled.status_code == 200 and pill(polled.text)[0] == "healthy"


# --------------------------------------------------------------------------- flash messages


def test_flash_round_trip(probe_client):
    response = probe_client.post("/_probe/saved", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/_probe/page"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{flash_module.COOKIE_NAME}=")
    for part in ("HttpOnly", "Max-Age=120", "Path=/", "SameSite=strict"):
        assert part in cookie

    page = probe_client.get("/_probe/page")
    assert page.context["messages"] == [{"level": "success", "text": "Rule saved"}]
    assert toast_texts(page.text) == ["Rule saved"]
    assert '<div class="toast toast--ok" data-toast>' in page.text
    assert "Max-Age=0" in page.headers["set-cookie"]  # shown once, then cleared
    assert toast_texts(probe_client.get("/_probe/page").text) == []


def test_following_the_redirect_shows_the_toast(probe_client):
    page = probe_client.post("/_probe/saved")
    assert page.url.path == "/_probe/page" and toast_texts(page.text) == ["Rule saved"]


def test_several_messages_keep_their_order_and_are_escaped(probe_client):
    page = probe_client.post("/_probe/two-messages")
    assert page.context["messages"] == [
        {"level": "success", "text": "First"},
        {"level": "error", "text": "Second <b>bold</b>"},
    ]
    assert "Second &lt;b&gt;bold&lt;/b&gt;" in page.text and "<b>bold</b>" not in page.text
    assert 'class="toast toast--over" role="alert" data-persist data-toast' in page.text


def test_htmx_partials_leave_the_flash_for_the_next_page(probe_client):
    probe_client.post("/_probe/saved", follow_redirects=False)
    partial = probe_client.get("/_probe/status", headers=HTMX)
    assert "Rule saved" not in partial.text and "set-cookie" not in partial.headers
    assert toast_texts(probe_client.get("/_probe/page").text) == ["Rule saved"]


def test_htmx_redirect(probe_client):
    response = probe_client.post("/_probe/saved", headers=HTMX)
    assert response.status_code == 200 and response.headers["hx-redirect"] == "/_probe/page"
    assert response.text == "" and "set-cookie" in response.headers
    assert toast_texts(probe_client.get("/_probe/page").text) == ["Rule saved"]


def _tampered(value: str) -> str:
    """``value`` with one character in the middle of its signature changed: always a different
    signature (overwriting the last two characters with "AA" left it valid about 1 run in 256)."""
    payload, _, signature = value.partition(".")
    i = len(signature) // 2
    flipped = "B" if signature[i] == "A" else "A"
    return f"{payload}.{signature[:i]}{flipped}{signature[i + 1 :]}"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        "a.b",
        _tampered(flash_module.encode([("success", "x")])),
        flash_module._b64(b'[["success","forged"]]') + ".AAAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_a_tampered_or_foreign_flash_cookie_is_ignored(probe_client, value):
    cookie = {"Cookie": f"{flash_module.COOKIE_NAME}={value}"}
    page = probe_client.get("/_probe/page", headers=cookie)
    assert page.status_code == 200 and page.context["messages"] == []


def test_flash_checks_level_and_text():
    with pytest.raises(ValueError, match="unknown flash level"):
        flash(Response(), "fatal", "x")
    with pytest.raises(ValueError, match="needs text"):
        flash(Response(), "info", "   ")
    assert flash_module.decode("é.x") == []
    long = flash_module.decode(flash_module.encode([flash_module._check("info", "a  b\n" * 200)]))
    assert long[0]["text"].startswith("a b a b") and len(long[0]["text"]) == flash_module.MAX_TEXT


def test_flash_oob_is_an_escaped_out_of_band_toast():
    snippet = flash_oob("error", "Couldn't save <script>")
    assert isinstance(snippet, Markup)
    assert snippet.startswith('<div hx-swap-oob="beforeend:#flash">')
    assert "&lt;script&gt;" in snippet and 'role="alert"' in snippet


@pytest.mark.parametrize(
    ("value", "local"),
    [
        ("/rules", True),
        ("/transactions?q=uber&page=2", True),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("/\t/evil.example", False),
        ("https://evil.example/", False),
        ("rules", False),
        ("", False),
        (None, False),
    ],
)
def test_is_local_path(value, local):
    assert is_local_path(value) is local


def test_redirect_refuses_other_sites(probe_client):
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    with pytest.raises(ValueError, match="path on this app"):
        redirect(request, "https://evil.example/")


# --------------------------------------------------------------------------- error pages


@pytest.mark.parametrize(
    ("what", "status", "title", "command"),
    [
        ("schema", 503, "Upgrade needed", "./run migrate"),
        ("keychain", 503, "Keychain approval needed", "./run doctor"),
        ("plaid-keys", 503, "Setup isn't finished", "./run setup --keys-only"),
        ("db-password", 503, "Setup isn't finished", "./run setup"),
        ("database", 503, "The database isn't responding", "./run db start"),
        ("operational", 503, "The database isn't responding", "./run db start"),
        ("plaid", 502, "Plaid couldn't do that", None),
        ("conflict", 409, "Something changed", None),
        ("custom", 400, "Bad request", None),
    ],
)
def test_known_failures_get_a_page_with_the_fix(probe_client, what, status, title, command):
    response = probe_client.get(f"/_probe/fail/{what}")
    assert response.status_code == status
    assert response.template.name == "errors/error.html"
    context = response.context
    assert context["title"] == title and context["command"] == command
    assert context["env"] == "test" and context["conn"] is None
    html = response.text
    assert f"<title>{title.replace("'", '&#39;')} · Budget Tracker (Test)</title>" in html
    assert html.count("<h1") == 1  # the page's title, for screen readers too
    assert f'<h1 class="empty__title">{title.replace("'", "&#39;")}</h1>' in html
    if command:
        assert f"<code>{command}</code>" in html
    for secret_ish in ("access-sandbox", "internal detail", "Traceback", "server closed"):
        assert secret_ish not in html


def test_messages_written_for_the_user_are_shown(probe_client):
    assert "Amex is busy." in probe_client.get("/_probe/fail/plaid").text
    assert "isn&#39;t a number" in probe_client.get("/_probe/fail/custom").text
    network = probe_client.get("/_probe/fail/plaid-network")
    assert network.status_code == 502 and "Plaid couldn&#39;t be reached" in network.text


def test_htmx_failures_that_need_a_fix_reload_the_page(probe_client):
    response = probe_client.get("/_probe/fail/keychain", headers=HTMX)
    assert response.status_code == 503 and response.headers["hx-refresh"] == "true"


def test_htmx_plaid_errors_become_a_toast(probe_client):
    response = probe_client.get("/_probe/fail/plaid", headers=HTMX)
    assert response.status_code == 200 and response.headers["hx-reswap"] == "none"
    assert response.text.startswith('<div hx-swap-oob="beforeend:#flash">')
    assert toast_texts(response.text) == ["Amex is busy."]


def test_production_failures_name_the_production_fixes(make_app, make_client):
    app = make_app(load_settings("production"))
    app.include_router(probe)
    client = make_client(app)
    keychain = client.get("/_probe/fail/keychain").context
    assert keychain["command"] == "./run doctor --env production"
    assert keychain["title"] == "Budget Tracker needs Keychain approval"
    assert client.get("/_probe/fail/schema").context["command"] == "./run live"


def test_a_demo_with_an_old_schema_is_recreated(probe_app, make_client):
    probe_app.state.demo_name = "shell"
    page = make_client(probe_app).get("/_probe/fail/schema")
    assert page.context["command"] == "./run demo create shell" and "env-bar--demo" in page.text


def test_bad_parameters_get_a_400_page_not_json(probe_client):
    response = probe_client.get("/_probe/number?n=abc")
    assert response.status_code == 400 and response.template.name == "errors/error.html"
    assert response.headers["content-type"].startswith("text/html")


def test_every_page_says_upgrade_needed_while_the_schema_is_behind(
    make_app, make_client, monkeypatch
):
    monkeypatch.setattr(app_db, "code_head", lambda: "9999_from_the_future")
    client = make_client(make_app())  # the app starts anyway
    for path in ("/", "/transactions", "/settings"):
        response = client.get(path)
        assert response.status_code == 503 and response.context["title"] == "Upgrade needed"
    assert client.get("/static/css/app.css").status_code == 200
    monkeypatch.undo()
    assert client.get("/dev/ui-kit").status_code == 200  # recovers without a restart


def test_a_database_that_cannot_be_read_is_not_an_upgrade(make_app, make_client, monkeypatch):
    def unreadable(_bind):
        raise sa_exc.OperationalError("SELECT version_num", {}, Exception("refused"))

    monkeypatch.setattr(app_db, "current_revision", unreadable)
    response = make_client(make_app()).get("/")
    assert response.status_code == 503
    assert response.context["title"] == "The database isn't responding"


def test_a_passed_schema_check_is_not_repeated_on_every_request(client, monkeypatch):
    monkeypatch.setattr(app_db, "code_head", lambda: "9999_from_the_future")
    assert client.get("/dev/ui-kit").status_code == 200  # checked at startup only


# --------------------------------------------------------------------------- dependencies


def test_a_route_that_commits_keeps_its_change(probe_client, db):
    assert probe_client.post("/_probe/write").text == "saved"
    assert db.scalar(select(Category).where(Category.name == "Probe category")) is not None


def test_a_route_that_fails_rolls_back(probe_app, make_client, db):
    client = make_client(probe_app, raise_server_exceptions=False)
    response = client.post("/_probe/write?fail=true")
    assert response.status_code == 500 and response.template.name == "errors/500.html"
    assert response.text.count("<h1") == 1 and "empty__icon--warn" in response.text
    assert db.scalar(select(Category).where(Category.name == "Probe category")) is None


def test_gateway_notifier_and_settings_come_from_the_app(probe_client, probe_app, fake_plaid):
    assert probe_client.get("/_probe/deps").text == "FakePlaidGateway RecordingNotifier test"
    assert probe_app.state.gateway is fake_plaid


def test_the_gateway_is_built_once_on_first_use(make_app, make_client, fake_plaid):
    built = []

    def factory(settings):
        built.append(settings.env)
        return fake_plaid

    app = make_app(gateway_factory=factory)
    app.include_router(probe)
    client = make_client(app)
    client.get("/dev/ui-kit")
    assert built == []  # pages that don't use Plaid never build it
    client.get("/_probe/deps")
    client.get("/_probe/deps")
    assert built == ["test"]


def test_missing_plaid_keys_explain_the_fix_and_retry(make_app, make_client, fake_plaid):
    attempts = []

    def factory(_settings):
        attempts.append(1)
        if len(attempts) == 1:
            raise MissingSecretError("plaid.secret.sandbox")
        return fake_plaid

    app = make_app(gateway_factory=factory)
    app.include_router(probe)
    client = make_client(app)
    first = client.get("/_probe/deps")
    assert first.status_code == 503 and first.context["command"] == "./run setup --keys-only"
    assert client.get("/_probe/deps").status_code == 200


def test_form_dep_gives_def_routes_the_posted_form(probe_client):
    response = probe_client.post("/_probe/form", data={"name": " Pets ", "tag": ["a", "b"]})
    assert response.text == "Pets ['a', 'b']"
    invalid = probe_client.post("/_probe/form", data={"name": ""})
    assert invalid.status_code == 422 and invalid.text == "Give the category a name."
    upload = probe_client.post(
        "/_probe/form", data={"name": "Import"}, files={"file": ("activity.csv", b"a,b")}
    )
    assert upload.text == "Import [] activity.csv=a,b"


# --------------------------------------------------------------------------- templates and filters


def test_templates_are_strict_under_pytest():
    assert templates.env.undefined is jinja2.StrictUndefined
    assert make_environment(strict=False).undefined is jinja2.Undefined
    with pytest.raises(jinja2.UndefinedError):
        templates.env.from_string("{{ nope.field }}").render()


def test_tojson_accepts_decimal():
    rendered = templates.env.from_string("{{ x|tojson }}").render(x={"b": Decimal("1.50"), "a": 2})
    assert rendered == '{"a": 2, "b": "1.50"}'


def test_money_filters():
    assert money_filter(Decimal("1234.5")) == "$1,234.50"
    assert money_filter(Decimal("-54.985")) == "−$54.99"
    assert money_filter(None) == "—"
    assert money_filter(Decimal("12"), signed=True) == "+$12.00"
    assert money_signed(Decimal("-3.5")) == "−$3.50"
    assert money_signed(Decimal("0")) == "$0.00"
    with pytest.raises(TypeError):
        money_filter(1.5)
    rendered = templates.env.from_string("{{ x|money }} {{ x|money(signed=true) }}").render(
        x=Decimal("7")
    )
    assert rendered == "$7.00 +$7.00"


def test_pct():
    assert pct(Decimal("84.5")) == "85%"
    assert pct(84.4) == "84%"
    assert pct(Decimal("79.96"), 1) == "80.0%"
    assert pct(Decimal("-5")) == "−5%"
    assert pct(1250) == "1,250%"
    assert pct(None) == "—"
    with pytest.raises(TypeError):
        pct(True)


def test_dates():
    day = dt.date(2026, 10, 27)
    assert date_short(day) == "Oct 27"
    assert date_long(day) == "Oct 27, 2026"
    assert month_label(day) == "Oct 2026"
    assert month_label("2026-03") == "Mar 2026"
    assert date_long("2026-01-05") == "Jan 5, 2026"
    assert date_short(None) == "—"
    moment = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.UTC)
    assert date_long(moment) == date_long(moment.astimezone().date())  # the Mac's local date


def test_relative_time(at_now):
    assert relative_time(NOW - dt.timedelta(seconds=20)) == "just now"
    assert relative_time(NOW + dt.timedelta(minutes=5)) == "just now"
    assert relative_time(NOW - dt.timedelta(minutes=5)) == "5 min ago"
    assert relative_time(NOW - dt.timedelta(hours=3, minutes=59)) == "3 h ago"
    assert relative_time(NOW - dt.timedelta(days=1, hours=1)) == "1 day ago"
    assert relative_time(NOW - dt.timedelta(days=12)) == "12 days ago"
    assert relative_time(NOW - dt.timedelta(days=45)) == date_long(NOW - dt.timedelta(days=45))
    assert relative_time(NOW.replace(tzinfo=None) - dt.timedelta(minutes=2)) == "2 min ago"
    assert relative_time(None) == "never"


def test_pluralize():
    assert pluralize(1, "charge") == "1 charge"
    assert pluralize(0, "charge") == "0 charges"
    assert pluralize(1234, "charge") == "1,234 charges"
    assert pluralize(2, "category", "categories") == "2 categories"
    rendered = templates.env.from_string('{{ n|pluralize("rule") }}').render(n=3)
    assert rendered == "3 rules"
