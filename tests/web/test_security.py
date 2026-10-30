"""Security middleware, headers and CSP rules (ARCHITECTURE §9; SEC-1, SEC-4)."""

from __future__ import annotations

import re

import jinja2
import pytest
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from budgetapp.config import load_settings
from budgetapp.web import security
from budgetapp.web.security import DEFAULT_CSP, PLAID_LINK_SCRIPT, allow_plaid_link, host_allowed
from budgetapp.web.templating import TEMPLATES_DIR, render, templates
from tests.web.csp import csp_problems

EVIL = "http://evil.example"

#: ARCHITECTURE §9, written out: the tests compare headers with these literals, never with the
#: module's own constants, so weakening the policy in security.py fails here.
SPEC_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def spec_plaid_csp(nonce: str, api: str) -> str:
    """ARCHITECTURE §9's Plaid Link variant, written out."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' "
        "https://cdn.plaid.com/link/v2/stable/link-initialize.js; "
        f"style-src 'self' 'nonce-{nonce}'; style-src-elem 'self' 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; img-src 'self' data:; frame-src https://cdn.plaid.com/; "
        f"connect-src 'self' {api}; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


probe = APIRouter()


@probe.api_route("/_probe/echo", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def echo(request: Request) -> Response:
    return PlainTextResponse(f"{request.method} ok")


@probe.get("/_probe/nonce")
def show_nonce(request: Request) -> Response:
    return PlainTextResponse(request.state.csp_nonce)


@probe.get("/_probe/plaid")
def plaid_page(request: Request) -> Response:
    """Stands in for the Connection page (a shell-owned template: SYNC replaces the real one)."""
    allow_plaid_link(request)
    return render(request, "errors/error.html", {"title": "Link"}, active_nav="connection")


@probe.get("/_probe/boom")
def boom() -> Response:
    raise RuntimeError("secret detail access-sandbox-1234 in the traceback")


@pytest.fixture
def probe_app(app):
    app.include_router(probe)
    return app


@pytest.fixture
def raw(probe_app, make_client):
    return make_client(probe_app, origin=False)


def assert_security_headers(response, *, static: bool = False) -> None:
    headers = response.headers
    assert headers["content-security-policy"] == SPEC_CSP
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert "cross-origin-opener-policy" not in headers  # Plaid's OAuth pop-up needs window.opener
    assert headers["cache-control"] == ("no-cache" if static else "no-store")


# --------------------------------------------------------------------------- Host header


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8766",
        "127.0.0.1.evil.example",
        "localhost.evil.example:8766",
        "localhost.",
        "[::1]:8766",
        "0.0.0.0:8766",
        "127.0.0.1:port",
    ],
)
def test_a_foreign_host_is_refused(raw, host):
    response = raw.get("/", headers={"Host": host})
    assert response.status_code == 400
    assert response.text == security.BAD_HOST_TEXT
    assert_security_headers(response)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8766", "localhost:8766", "LocalHost:1"])
def test_loopback_hosts_are_served(raw, host):
    assert raw.get("/_probe/echo", headers={"Host": host}).status_code == 200


def test_host_allowed():
    assert host_allowed("127.0.0.1:8766") and host_allowed("localhost")
    assert not host_allowed("") and not host_allowed("127.0.0.2") and not host_allowed("::1")


# --------------------------------------------------------------------------- Origin checks


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "origin",
    [EVIL, "null", "http://127.0.0.1:8765", "https://127.0.0.1:8766", "http://localhost:3000"],
)
def test_cross_origin_unsafe_requests_are_refused(raw, method, origin):
    response = raw.request(method, "/_probe/echo", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.text == security.CROSS_SITE_TEXT
    assert_security_headers(response)


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_fetch_metadata_without_origin_must_be_same_origin(raw, site):
    response = raw.post("/_probe/echo", headers={"Sec-Fetch-Site": site})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl and other non-browser clients send neither header
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://127.0.0.1:8766"},
        {"Origin": "http://localhost:8766"},
        {"Origin": "http://127.0.0.1:8766", "Sec-Fetch-Site": "same-origin"},
    ],
)
def test_same_origin_posts_pass_the_middleware(raw, headers):
    response = raw.post("/_probe/echo", headers=headers)
    assert response.status_code == 200 and response.text == "POST ok"


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_a_plain_form_post_from_the_apps_own_page_passes(raw, site):
    """Under Referrer-Policy: no-referrer, Chrome sends ``Origin: null`` on the app's own plain
    ``<form method="post">`` submissions (htmx requests carry the real origin)."""
    response = raw.post("/_probe/echo", headers={"Origin": "null", "Sec-Fetch-Site": site})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},  # an old browser without Fetch Metadata: unknown, so refused
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},  # an evil page with no-referrer
        {"Origin": "null", "Sec-Fetch-Site": "same-site"},  # another port on this Mac
        {"Origin": EVIL, "Sec-Fetch-Site": "same-origin"},  # a foreign Origin never passes
    ],
)
def test_a_null_origin_needs_the_browsers_same_origin_word(raw, headers):
    assert raw.post("/_probe/echo", headers=headers).status_code == 403


def test_the_client_fixture_posts_as_the_apps_own_pages(probe_app, client):
    assert client.post("/_probe/echo").status_code == 200
    assert client.delete("/_probe/echo").status_code == 200


def test_safe_methods_are_not_origin_checked(raw):
    assert raw.get("/_probe/echo", headers={"Origin": EVIL}).status_code == 200


def test_origin_checks_follow_the_apps_port(make_app, make_client):
    production = make_app(load_settings("production"))
    production.include_router(probe)
    client = make_client(production, origin=False)
    assert client.post("/_probe/echo", headers={"Origin": "http://127.0.0.1:8765"}).is_success
    refused = client.post("/_probe/echo", headers={"Origin": "http://127.0.0.1:8766"})
    assert refused.status_code == 403


# --------------------------------------------------------------------------- Fetch Metadata


def fetch(site: str, mode: str, dest: str) -> dict[str, str]:
    return {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize(
    ("mode", "dest"),
    [
        ("no-cors", "image"),  # <img src="http://127.0.0.1:8766/transactions?q=…">
        ("no-cors", "script"),
        ("navigate", "iframe"),
        ("cors", "empty"),  # fetch()
        ("no-cors", "empty"),
    ],
)
@pytest.mark.parametrize("path", ["/", "/transactions?q=netflix", "/static/css/app.css"])
def test_other_sites_cannot_load_the_apps_pages_in_the_background(raw, site, mode, dest, path):
    """Cross-site subresource requests could time the app's pages (XS-Search): refused."""
    response = raw.get(path, headers=fetch(site, mode, dest))
    assert response.status_code == 403
    assert response.text == security.CROSS_SITE_TEXT
    assert_security_headers(response)


@pytest.mark.parametrize("site", ["cross-site", "same-site", "same-origin", "none"])
def test_a_link_or_bookmark_from_anywhere_opens_the_page(raw, site):
    response = raw.get("/", headers=fetch(site, "navigate", "document"))
    assert response.status_code == 200 and "<title>" in response.text


@pytest.mark.parametrize(("mode", "dest"), [("cors", "empty"), ("no-cors", "image")])
def test_the_apps_own_requests_pass(raw, mode, dest):
    """htmx, fetch and subresources from the app's pages say same-origin."""
    assert raw.get("/_probe/echo", headers=fetch("same-origin", mode, dest)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        # A page on the other loopback name (another process holding [::1]:<port>) has an Origin
        # the allow-list accepts, but the browser knows it is another site.
        {"Origin": "http://localhost:8766", **fetch("cross-site", "cors", "empty")},
        {"Origin": "http://127.0.0.1:8766", **fetch("same-site", "cors", "empty")},
        # A cross-site form post is a navigation, but never a GET.
        {"Origin": "http://127.0.0.1:8766", **fetch("cross-site", "navigate", "document")},
        {"Origin": "null", **fetch("cross-site", "navigate", "document")},
    ],
)
def test_an_allowed_origin_never_overrules_the_browsers_cross_site_word(raw, headers):
    assert raw.post("/_probe/echo", headers=headers).status_code == 403


def test_a_cross_site_head_or_option_request_is_refused(raw):
    headers = fetch("cross-site", "navigate", "document")
    assert raw.head("/", headers=headers).status_code == 403
    assert raw.options("/", headers=headers).status_code == 403


def test_fetch_metadata_allowed():
    from starlette.datastructures import Headers

    def allowed(method: str, **headers: str) -> bool:
        raw_headers = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
        return security.fetch_metadata_allowed(method, Headers(raw=raw_headers))

    assert allowed("GET") and allowed("POST")  # curl and old browsers: nothing to check
    assert allowed("GET", sec_fetch_site="Same-Origin", sec_fetch_dest="image")
    link = {"sec_fetch_site": "cross-site", "sec_fetch_mode": "navigate"}
    assert allowed("GET", **link, sec_fetch_dest="document")
    assert not allowed("GET", sec_fetch_site="cross-site", sec_fetch_mode="navigate")
    assert not allowed("GET", sec_fetch_site="cross-site", sec_fetch_dest="document")
    assert not allowed("GET", sec_fetch_site="something-new")


# --------------------------------------------------------------------------- headers and CSP


@pytest.mark.parametrize("path", ["/", "/transactions", "/dev/ui-kit", "/no-such-page"])
def test_pages_get_the_security_headers(raw, path):
    response = raw.get(path)
    assert response.headers["content-type"].startswith("text/html")
    assert_security_headers(response)


def test_static_files_get_the_security_headers_and_revalidate(raw):
    assert_security_headers(raw.get("/static/css/app.css"), static=True)
    assert_security_headers(raw.get("/static/vendor/htmx.min.js"), static=True)


def test_redirects_and_refusals_are_never_cached(raw):
    assert raw.post("/_probe/echo", headers={"Origin": EVIL}).headers["cache-control"] == "no-store"


def test_each_request_gets_a_fresh_nonce(raw):
    first, second = raw.get("/_probe/nonce").text, raw.get("/_probe/nonce").text
    assert first != second and len(first) >= 22
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


def test_default_pages_have_no_nonce_and_no_plaid(raw):
    csp = raw.get("/").headers["content-security-policy"]
    assert "nonce" not in csp and "plaid" not in csp and "unsafe-inline" in csp
    assert "script-src 'self';" in csp and "style-src 'self';" in csp


def test_allow_plaid_link_switches_only_that_response(raw):
    response = raw.get("/_probe/plaid")
    nonce = response.context["csp_nonce"]
    csp = response.headers["content-security-policy"]
    assert csp == security.plaid_link_csp(nonce, "sandbox")
    assert f"script-src 'self' 'nonce-{nonce}' {PLAID_LINK_SCRIPT};" in csp
    assert f"style-src 'self' 'nonce-{nonce}';" in csp
    assert f"style-src-elem 'self' 'nonce-{nonce}';" in csp
    assert "style-src-attr 'unsafe-inline';" in csp
    assert "frame-src https://cdn.plaid.com/;" in csp
    assert "connect-src 'self' https://sandbox.plaid.com/;" in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert raw.get("/").headers["content-security-policy"] == DEFAULT_CSP


def test_plaid_link_connects_to_the_environments_plaid_host(make_app, make_client):
    production = make_app(load_settings("production"))
    production.include_router(probe)
    response = make_client(production).get("/_probe/plaid")
    csp = response.headers["content-security-policy"]
    assert "connect-src 'self' https://production.plaid.com/;" in csp
    assert "sandbox.plaid.com" not in csp


def test_the_500_page_has_the_security_headers_and_no_details(probe_app, make_client):
    client = make_client(probe_app, raise_server_exceptions=False)
    response = client.get("/_probe/boom")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "RuntimeError" not in response.text and "access-sandbox" not in response.text
    assert "Traceback" not in response.text
    assert_security_headers(response)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_api_docs_are_disabled(raw, path):
    assert raw.get(path).status_code == 404


# --------------------------------------------------------------------------- templates


def test_templates_follow_the_csp():
    """Every template, feature pages included: no inline JS/CSS, no event attributes."""
    found = {
        str(path.relative_to(TEMPLATES_DIR)): csp_problems(path.read_text())
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
    }
    assert "base.html" in found and "components/ui.html" in found
    assert {name: problems for name, problems in found.items() if problems} == {}


def test_the_template_check_catches_violations():
    assert csp_problems("<script>alert(1)</script>")
    assert csp_problems('<script src="https://cdn.example.com/x.js"></script>')
    assert csp_problems("<style>p{}</style>")
    assert csp_problems('<button onclick="go()">')
    assert csp_problems('<button {% if x %}onclick="go()"{% endif %}>')
    assert csp_problems('<a href="javascript:go()">')
    assert csp_problems('<a href="javascript:{{ x }}">')
    assert csp_problems('<div hx-on:click="x">')
    assert not csp_problems('<script src="/static/js/page.js" defer></script>')
    assert not csp_problems('<script type="application/json" id="c">{}</script>')
    assert not csp_problems(f'<script src="{PLAID_LINK_SCRIPT}" nonce="n" defer></script>')
    assert not csp_problems('{# never <style> or onclick= here #}<p style="--x: 1">')
    assert not csp_problems("{% if key[:5] == 'hx-on' %}{{ fail('javascript: URL') }}{% endif %}")
    assert csp_problems("{{ x }}<b onclick=go()>", rendered=True)
    # user text, escaped inside an attribute value, is not an attribute (reported by TXN, CAT, CSV)
    escaped = "<p title=\"&lt;img src=x onerror=alert(1)&gt;\" data-x='a onload=b'>"
    assert not csp_problems(escaped, rendered=True)
    assert csp_problems('<p title="a > b" onclick="go()">', rendered=True)
    assert csp_problems("<p title='x' onmouseover=go()>", rendered=True)


@pytest.mark.parametrize(
    "call",
    [
        '{{ ui.button("Save", onclick="save()") }}',
        '{{ ui.button("Save", hx_on__click="save()") }}',
        '{{ ui.button("Save", data_hx_on_click="save()") }}',
        '{{ ui.button("Save", hx_get="javascript:save()") }}',
        '{{ form.field("q", "Search", onfocus="steal()") }}',
        '{{ form.select_field("s", "Status", [("a", "A")], onchange="go()") }}',
        '{{ form.filter_bar("/x", onsubmit="go()") }}',
    ],
)
def test_macros_refuse_inline_event_handlers(call):
    """The CSP would silently block these in the browser; the macros fail loudly instead."""
    source = '{% import "components/ui.html" as ui %}{% import "components/forms.html" as form %}'
    with pytest.raises(jinja2.UndefinedError, match="CSP blocks"):
        templates.env.from_string(source + call).render()


def test_macros_keep_ordinary_attributes():
    source = '{% import "components/ui.html" as ui %}'
    html = templates.env.from_string(
        source + '{{ ui.button("Save", hx_post="/x", data_confirm="Sure?", one_more=true) }}'
    ).render()
    assert 'hx-post="/x"' in html and 'data-confirm="Sure?"' in html


def test_the_csp_is_the_one_architecture_section_9_specifies():
    assert DEFAULT_CSP == SPEC_CSP
    apis = {"sandbox": "https://sandbox.plaid.com/", "production": "https://production.plaid.com/"}
    for env, api in apis.items():
        assert security.plaid_link_csp("N0nce", env) == spec_plaid_csp("N0nce", api)
