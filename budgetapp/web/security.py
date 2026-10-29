"""Request checks and security headers (ARCHITECTURE §9; SEC-1, SEC-4).

Two pure ASGI middlewares (not ``BaseHTTPMiddleware``, which buffers responses):

* :class:`HostOriginMiddleware` refuses requests that did not come from this app:

  * the ``Host`` header must be ``127.0.0.1`` or ``localhost`` (any port), else **400**. This
    blocks DNS rebinding (an evil site whose name resolves to 127.0.0.1).
  * Fetch Metadata (resource isolation), every method: when the browser says the request comes
    from another site (``Sec-Fetch-Site`` other than ``same-origin``/``none``), only a top-level
    ``GET`` navigation passes (``Sec-Fetch-Mode: navigate``, ``Sec-Fetch-Dest: document``: a
    link or bookmark from elsewhere). Images, scripts, frames and ``fetch`` from other sites get
    **403**, so another website can't load the app's pages in the background and time them
    (XS-Search), and a page served on the other loopback name by another process can't send
    requests with an Origin the list below accepts. Clients that send no ``Sec-Fetch-Site``
    (curl, old browsers) skip this check.
  * ``POST``/``PUT``/``PATCH``/``DELETE`` need an ``Origin`` of this app
    (``http://127.0.0.1:<web_port>`` or ``http://localhost:<web_port>``). Without an ``Origin``
    they are refused only when ``Sec-Fetch-Site`` is present and is not ``same-origin`` or
    ``none``. Both refusals are **403**. This blocks cross-site form posts to localhost, and
    non-browser clients such as curl, which send neither header, still work.
  * ``Origin: null`` passes only together with ``Sec-Fetch-Site: same-origin`` (or ``none``).
    Under ``Referrer-Policy: no-referrer`` browsers send ``Origin: null`` on the app's own plain
    ``<form method="post">`` submissions (Fetch spec; checked in Chrome), while htmx requests
    carry the real origin. ``Sec-Fetch-Site`` is set by the browser alone, so a cross-site page
    cannot fake it: its posts say ``cross-site`` (or, in a browser too old to send it, nothing)
    and are refused.

* :class:`SecurityHeadersMiddleware` gives every request a fresh CSP nonce
  (``request.state.csp_nonce``) and adds the security headers to every response, refusals
  included. It never sets Cross-Origin-Opener-Policy, because Plaid's OAuth pop-up needs
  ``window.opener``.

CSP: every page gets :data:`DEFAULT_CSP` (no inline scripts or ``<style>`` elements; ``style=""``
attributes are allowed). A route that opens Plaid Link calls :func:`allow_plaid_link` to switch
its own response to :func:`plaid_link_csp`.
"""

from __future__ import annotations

import secrets
from collections.abc import MutableMapping

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
#: ``Sec-Fetch-Site`` values that a browser sends for the app's own pages (or typed URLs).
SAFE_FETCH_SITES = frozenset({"same-origin", "none"})

PLAID_LINK_SCRIPT = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"
PLAID_LINK_FRAME = "https://cdn.plaid.com/"
PLAID_API_ORIGINS = {
    "sandbox": "https://sandbox.plaid.com/",
    "production": "https://production.plaid.com/",
}

_COMMON = "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"

#: The Content Security Policy of every response (ARCHITECTURE §9).
DEFAULT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
    f"img-src 'self' data:; {_COMMON}"
)

#: Headers on every response, besides the CSP and Cache-Control.
STATIC_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}

BAD_HOST_TEXT = "Bad request: this app answers only on 127.0.0.1 and localhost."
CROSS_SITE_TEXT = "Blocked: this request did not come from the app's own pages."


def new_nonce() -> str:
    """A fresh CSP nonce (128 random bits, URL-safe base64)."""
    return secrets.token_urlsafe(16)


def plaid_link_csp(nonce: str, plaid_env: str) -> str:
    """The CSP for a page that opens Plaid Link (RESEARCH T2.2, nonce variant)."""
    api = PLAID_API_ORIGINS[plaid_env]
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' {PLAID_LINK_SCRIPT}; "
        f"style-src 'self' 'nonce-{nonce}'; "
        f"style-src-elem 'self' 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data:; "
        f"frame-src {PLAID_LINK_FRAME}; "
        f"connect-src 'self' {api}; "
        f"{_COMMON}"
    )


def csp_nonce(request: Request) -> str:
    """This request's CSP nonce (set by :class:`SecurityHeadersMiddleware`)."""
    nonce = getattr(request.state, "csp_nonce", None)
    if not nonce:
        nonce = request.state.csp_nonce = new_nonce()
    return nonce


def allow_plaid_link(request: Request) -> str:
    """Switch this request's response to the Plaid Link CSP; returns the nonce.

    Only the Connection page calls this. Put the nonce on the Link script tag, e.g.
    ``<script src="…/link-initialize.js" nonce="{{ csp_nonce }}" defer></script>``; Plaid uses it
    for the styles it injects.
    """
    request.state.plaid_link = True
    return csp_nonce(request)


def apply_security_headers(
    headers: MutableHeaders,
    *,
    nonce: str,
    plaid_env: str,
    plaid_link: bool = False,
    static: bool = False,
) -> None:
    """Set the CSP and the other security headers on a response's headers.

    ``Cache-Control`` is left alone when the response already has one; otherwise it is
    ``no-store`` (pages, partials, redirects, exports: financial data is never cached) or
    ``no-cache`` for ``/static`` files (always revalidated, so an upgrade never shows stale JS).
    """
    headers["Content-Security-Policy"] = (
        plaid_link_csp(nonce, plaid_env) if plaid_link else DEFAULT_CSP
    )
    for name, value in STATIC_HEADERS.items():
        headers[name] = value
    if "cache-control" not in headers:
        headers["Cache-Control"] = "no-cache" if static else "no-store"


def host_allowed(value: str) -> bool:
    """True for ``127.0.0.1`` or ``localhost``, with or without a port."""
    name, sep, port = value.partition(":")
    if sep and not port.isdigit():
        return False
    return name.lower() in ALLOWED_HOSTS


def _values(headers: Headers, name: str) -> list[str]:
    return [value.strip().lower() for value in headers.getlist(name)]


def fetch_metadata_allowed(method: str, headers: Headers) -> bool:
    """Resource isolation: a request another site started may only navigate to a page.

    True when the browser says the request comes from this app (``Sec-Fetch-Site:
    same-origin``), from the user (``none``: typed URL, bookmark), or says nothing (curl);
    otherwise only a top-level ``GET`` navigation passes (a link from elsewhere).
    """
    if all(site in SAFE_FETCH_SITES for site in _values(headers, "sec-fetch-site")):
        return True
    return (
        method == "GET"
        and _values(headers, "sec-fetch-mode") == ["navigate"]
        and _values(headers, "sec-fetch-dest") == ["document"]
    )


def _refusal(text: str, status_code: int) -> PlainTextResponse:
    """A refused request: plain text, never cached (``/static`` paths included)."""
    return PlainTextResponse(text, status_code=status_code, headers={"Cache-Control": "no-store"})


class HostOriginMiddleware:
    """Refuse a foreign ``Host`` (400); a request from another site that isn't a page navigation,
    or an unsafe method that didn't come from the app's own pages (403)."""

    def __init__(self, app: ASGIApp, *, web_port: int) -> None:
        self.app = app
        self.allowed_origins = frozenset(
            {f"http://127.0.0.1:{web_port}", f"http://localhost:{web_port}"}
        )

    def origin_allowed(self, headers: Headers) -> bool:
        origins = [o.strip().lower() for o in headers.getlist("origin")]
        sites = _values(headers, "sec-fetch-site")
        same_origin_fetch = all(s in SAFE_FETCH_SITES for s in sites)
        if not origins:
            return same_origin_fetch
        if all(o in self.allowed_origins for o in origins):
            return True
        # A plain form post from the app's own page under Referrer-Policy: no-referrer.
        return bool(sites) and same_origin_fetch and all(o == "null" for o in origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or not host_allowed(hosts[0].strip()):
            await _refusal(BAD_HOST_TEXT, 400)(scope, receive, send)
            return
        method = scope["method"]
        if not fetch_metadata_allowed(method, headers) or (
            method in UNSAFE_METHODS and not self.origin_allowed(headers)
        ):
            await _refusal(CROSS_SITE_TEXT, 403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Create the per-request CSP nonce and add the security headers to every response."""

    def __init__(self, app: ASGIApp, *, plaid_env: str) -> None:
        self.app = app
        self.plaid_env = plaid_env

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state: MutableMapping[str, object] = scope.setdefault("state", {})
        nonce = new_nonce()
        state["csp_nonce"] = nonce
        static = scope["path"].startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(
                    MutableHeaders(scope=message),
                    nonce=nonce,
                    plaid_env=self.plaid_env,
                    plaid_link=bool(state.get("plaid_link")),
                    static=static,
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)
