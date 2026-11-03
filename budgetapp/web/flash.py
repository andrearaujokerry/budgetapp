"""Flash messages: toasts shown after a redirect, or appended to an HTMX partial.

* Form post that leaves the page: ``return redirect(request, "/rules", "success", "Rule saved")``
  (a ``303``, or ``HX-Redirect`` for an HTMX request). ``flash(response, level, text)`` adds a
  message to any other response the same way. The message travels in a short-lived cookie
  (``HttpOnly``, ``SameSite=Strict``, ``Path=/``, signed with a per-process key) and the next full
  page ``render()`` shows it as a toast and clears the cookie. HTMX partial renders leave the
  cookie alone.
* HTMX request → partial: pass ``oob=flash_oob("success", "Saved")`` to ``render()``. The snippet
  is an out-of-band swap that appends the toast to ``#flash``.

Levels: ``success``, ``info``, ``warning``, ``error`` (aliases ``ok``, ``warn``, ``danger``,
``over``). Errors stay until dismissed; the others fade after 6 s.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import TYPE_CHECKING

from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

if TYPE_CHECKING:
    from jinja2 import Environment

LEVELS = frozenset({"success", "info", "warning", "error", "ok", "warn", "danger", "over"})
COOKIE_NAME = "budgetapp_flash"
MAX_AGE = 120  # seconds: long enough for the redirect, short enough to never resurface later
MAX_MESSAGES = 5
MAX_TEXT = 300

_KEY = secrets.token_bytes(32)  # per process: a cookie from another app or run is ignored


def _check(level: str, text: str) -> tuple[str, str]:
    if level not in LEVELS:
        raise ValueError(f"unknown flash level {level!r}; use one of {sorted(LEVELS)}")
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("a flash message needs text")
    return level, text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(_KEY, payload.encode("ascii"), hashlib.sha256).digest()[:16])


def encode(messages: list[tuple[str, str]]) -> str:
    """The cookie value for ``messages`` (signed JSON, URL-safe)."""
    payload = _b64(json.dumps(messages[-MAX_MESSAGES:], separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def decode(value: str | None) -> list[dict[str, str]]:
    """Messages from a cookie value; ``[]`` for a missing, foreign, tampered or broken cookie."""
    if not value or "." not in value or not value.isascii():
        return []
    payload, _, signature = value.partition(".")
    try:
        if not hmac.compare_digest(signature, _sign(payload)):
            return []
        items = json.loads(_unb64(payload))
    except ValueError:  # bad base64, JSON or UTF-8
        return []
    messages = []
    for item in items if isinstance(items, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 2
            and item[0] in LEVELS
            and isinstance(item[1], str)
        ):
            messages.append({"level": item[0], "text": item[1]})
    return messages[:MAX_MESSAGES]


def flash(response: Response, level: str, text: str) -> Response:
    """Queue a toast for the next full page (call it on the redirect); returns ``response``.

    Several calls on one response keep every message, in order.
    """
    pending: list[tuple[str, str]] = getattr(response, "_flash_messages", [])
    pending.append(_check(level, text))
    response._flash_messages = pending  # type: ignore[attr-defined]
    # One Set-Cookie line holding every message (drop the one an earlier call added).
    response.raw_headers[:] = [
        (name, value)
        for name, value in response.raw_headers
        if not (name == b"set-cookie" and value.startswith(COOKIE_NAME.encode() + b"="))
    ]
    response.set_cookie(
        COOKIE_NAME,
        encode(pending),
        max_age=MAX_AGE,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return response


_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


def is_local_path(value: object) -> bool:
    """True for a path on this app (``/rules?page=2``), never an address elsewhere.

    Refuses ``//evil.example``, ``https://…``, backslashes and control characters or spaces
    (browsers turn ``/\\evil`` and ``/\\t/evil`` into ``//evil``). Check a user-supplied ``next``
    value with this before redirecting to it.
    """
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and not _UNSAFE_URL_CHARS.search(value)
    )


def redirect(
    request: Request, url: str, level: str | None = None, text: str | None = None
) -> Response:
    """Answer a successful form post by sending the browser to ``url`` (a path on this app).

    A plain form post gets ``303 See Other``. An HTMX request gets ``HX-Redirect`` instead, so
    htmx loads ``url`` as a full page rather than swapping the reply into the form. With
    ``level`` and ``text``, that page shows the toast.

    Browsers send no ``Referer`` to this app (``Referrer-Policy: no-referrer``): to return to the
    page a form came from, post its path in a hidden ``next`` field and check it with
    :func:`is_local_path` before passing it here.
    """
    if not is_local_path(url):
        raise ValueError(f"redirect() needs a path on this app (like '/rules'), got {url!r}")
    from budgetapp.web.templating import is_htmx

    if is_htmx(request):
        response: Response = Response(status_code=200, headers={"HX-Redirect": url})
    else:
        response = RedirectResponse(url, status_code=303)
    if level is not None or text is not None:
        flash(response, level or "", text or "")
    return response


def pending_messages(request: Request) -> list[dict[str, str]]:
    """Messages waiting in the request's flash cookie (``render()`` shows and clears them)."""
    return decode(request.cookies.get(COOKIE_NAME))


def clear(response: Response) -> None:
    """Delete the flash cookie on ``response``."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")


def flash_oob(level: str, text: str, *, env: Environment | None = None) -> Markup:
    """An HTMX out-of-band toast (``<div hx-swap-oob="beforeend:#flash">``) for a partial.

    Pass it to ``render(..., oob=flash_oob("success", "Saved"))``. The text is escaped.
    """
    level, text = _check(level, text)
    if env is None:
        from budgetapp.web.templating import templates

        env = templates.env
    return Markup(env.get_template("components/ui.html").module.flash_oob(level, text))
