"""Templates, filters and ``render()``, which every page, partial and error page goes through.

``render(request, template, context, *, active_nav)`` adds the layout context that ``base.html``
needs (``env``, ``nav_items``, ``active_nav``, ``conn``, ``messages``, ``app_version``) plus
``request`` and ``csp_nonce``, and returns an HTML response. The same call renders a full page
(the template extends ``base.html``) or an HTMX partial (a fragment template); routes pick the
template with ``is_htmx(request)``. ``error_page()`` renders a full error or notice page. See
``budgetapp/web/README.md``.

Filters: ``money``, ``money_signed``, ``pct``, ``date_short``, ``date_long``, ``month_label``,
``relative_time``, ``pluralize``. Under pytest, templates render with ``StrictUndefined``, so an
undefined variable or attribute fails the test instead of rendering blank.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import jinja2
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from starlette.types import Receive, Scope, Send

from budgetapp import __version__, clock
from budgetapp.money import MINUS, format_money
from budgetapp.services.status import ConnectionStatus, connection_status
from budgetapp.web import flash
from budgetapp.web.security import csp_nonce

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: Sidebar and mobile menu, in display order (``active_nav`` is one of these keys).
NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"key": "dashboard", "label": "Dashboard", "href": "/", "icon": "home", "group": "Money"},
    {
        "key": "transactions",
        "label": "Transactions",
        "href": "/transactions",
        "icon": "list",
        "group": "Money",
    },
    {"key": "budgets", "label": "Budgets", "href": "/budgets", "icon": "target", "group": "Money"},
    {
        "key": "trends",
        "label": "Trends",
        "href": "/trends",
        "icon": "trending",
        "group": "Insights",
    },
    {
        "key": "recurring",
        "label": "Recurring",
        "href": "/recurring",
        "icon": "repeat",
        "group": "Insights",
    },
    {
        "key": "statements",
        "label": "Statements",
        "href": "/statements",
        "icon": "receipt",
        "group": "Insights",
    },
    {
        "key": "categories",
        "label": "Categories",
        "href": "/categories",
        "icon": "tag",
        "group": "Organize",
    },
    {"key": "rules", "label": "Rules", "href": "/rules", "icon": "wand", "group": "Organize"},
    {"key": "imports", "label": "Import", "href": "/import", "icon": "upload", "group": "Data"},
    {
        "key": "connection",
        "label": "Connection",
        "href": "/connection",
        "icon": "link",
        "group": "Data",
    },
    {
        "key": "settings",
        "label": "Settings",
        "href": "/settings",
        "icon": "settings",
        "group": "Data",
    },
)
NAV_KEYS = frozenset(item["key"] for item in NAV_ITEMS)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------- filters


def money_filter(
    value: Decimal | int | str | None, signed: bool = False, symbol: bool = True, none: str = "—"
) -> str:
    """``{{ x|money }}`` → ``$1,234.56`` / ``−$54.99`` / ``—`` (``money.format_money``)."""
    return format_money(value, signed=signed, symbol=symbol, none=none)


def money_signed(value: Decimal | int | str | None, none: str = "—") -> str:
    """``{{ delta|money_signed }}`` → ``+$12.00`` / ``−$3.50`` / ``$0.00``."""
    return format_money(value, signed=True, none=none)


def pct(value: Decimal | int | float | None, places: int = 0, none: str = "—") -> str:
    """``{{ 84.6|pct }}`` → ``85%``; ``{{ x|pct(1) }}`` → ``84.6%``. The value is already a percent.

    Rounds half-up; negatives use the typographic minus (``−5%``); ``None`` → ``—``.
    """
    if value is None:
        return none
    if isinstance(value, bool):
        raise TypeError("pct needs a number, not a bool")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{abs(rounded):,.{places}f}%"
    return f"{MINUS}{text}" if rounded < 0 else text


def _as_date(value: date | datetime | str) -> date:
    """A calendar date; an aware datetime becomes the Mac's local date."""
    if isinstance(value, datetime):
        return (value.astimezone() if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        return date.fromisoformat(f"{text}-01" if len(text) == 7 else text[:10])
    raise TypeError(f"expected a date, got {type(value).__name__}")


def date_short(value: date | datetime | str | None, none: str = "—") -> str:
    """``Oct 27``."""
    if value is None:
        return none
    d = _as_date(value)
    return f"{MONTHS[d.month - 1]} {d.day}"


def date_long(value: date | datetime | str | None, none: str = "—") -> str:
    """``Oct 27, 2026``."""
    if value is None:
        return none
    d = _as_date(value)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def month_label(value: date | datetime | str | None, none: str = "—") -> str:
    """``Oct 2026`` (also accepts ``"2026-10"``)."""
    if value is None:
        return none
    d = _as_date(value)
    return f"{MONTHS[d.month - 1]} {d.year}"


def relative_time(value: datetime | None, none: str = "never") -> str:
    """``just now`` · ``5 min ago`` · ``3 h ago`` · ``2 days ago`` · then ``Sep 3, 2026``.

    Measured from ``clock.now()``; a naive datetime is taken as UTC, a future one as "just now".
    """
    if value is None:
        return none
    if not isinstance(value, datetime):
        raise TypeError(f"relative_time needs a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = (clock.now() - value).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    if days < 30:
        return "1 day ago" if days == 1 else f"{days} days ago"
    return date_long(value)


def pluralize(count: int | Decimal, singular: str, plural: str | None = None) -> str:
    """``{{ n|pluralize("charge") }}`` → ``1 charge`` / ``1,234 charges``.

    Pass ``plural`` for irregular words: ``{{ n|pluralize("category", "categories") }}``.
    """
    word = singular if count == 1 else (plural if plural is not None else f"{singular}s")
    return f"{count:,} {word}"


FILTERS = {
    "money": money_filter,
    "money_signed": money_signed,
    "pct": pct,
    "date_short": date_short,
    "date_long": date_long,
    "month_label": month_label,
    "relative_time": relative_time,
    "pluralize": pluralize,
}


def make_environment(*, strict: bool | None = None) -> jinja2.Environment:
    """The Jinja environment (autoescape on; ``StrictUndefined`` under pytest)."""
    if strict is None:
        strict = os.environ.get("BUDGETAPP_ENV") == "test"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined if strict else jinja2.Undefined,
    )
    env.filters.update(FILTERS)
    # |tojson (chart configs) accepts Decimal: it becomes a numeric string the chart helper reads.
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": str}
    return env


#: The one template engine of the app.
templates = Jinja2Templates(env=make_environment())


# --------------------------------------------------------------------------- render


class PageResponse(HTMLResponse):
    """HTML rendered from a template, plus optional out-of-band snippets after it.

    Like Starlette's TemplateResponse it hands ``template`` and ``context`` to the TestClient, so
    tests can check ``response.template.name`` and ``response.context["…"]``.
    """

    def __init__(
        self,
        template: jinja2.Template,
        context: dict[str, Any],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        oob: Iterable[Markup] = (),
    ) -> None:
        self.template = template
        self.context = context
        content = template.render(context) + "".join(str(escape(snippet)) for snippet in oob)
        super().__init__(content, status_code=status_code, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if "http.response.debug" in scope.get("extensions", {}):
            await send(
                {
                    "type": "http.response.debug",
                    "info": {"template": self.template, "context": self.context},
                }
            )
        await super().__call__(scope, receive, send)


def is_htmx(request: Request) -> bool:
    """True for an HTMX request (``HX-Request: true``) that wants a partial, not a full page."""
    headers = request.headers
    return (
        headers.get("hx-request") == "true" and headers.get("hx-history-restore-request") != "true"
    )


def layout_env(request: Request) -> str:
    """``env`` for the layout: ``"demo"`` when serving a demo database, else the settings' env."""
    state = request.app.state
    if getattr(state, "demo_name", None):
        return "demo"
    return state.settings.env


def current_connection_status(request: Request) -> ConnectionStatus | None:
    """``services.status.connection_status`` in a short session of its own (``None`` if it fails).

    In the test environment (pytest, demo databases) a failure is raised instead of hidden.
    """
    state = request.app.state
    try:
        with state.database.session_factory() as session:
            return connection_status(session)
    except Exception as exc:
        if state.settings.is_test:
            raise
        log.warning("Connection status unavailable for the layout (%s).", type(exc).__name__)
        log.debug("Connection status failure", exc_info=True)
        return None


def render(
    request: Request,
    template: str,
    context: Mapping[str, Any] | None = None,
    *,
    active_nav: str | None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    oob: Markup | Iterable[Markup] = (),
) -> PageResponse:
    """Render ``template`` with the layout context; the one way pages and partials respond.

    ``active_nav``: the nav key of the page (``"transactions"``) or ``None``. ``context`` keys
    override the layout's (pass ``conn=None`` to skip the connection-status query); ``messages``
    are added after the flash cookie's. ``oob``: out-of-band snippets appended after the template,
    e.g. ``flash.flash_oob("success", "Saved")`` (plain strings are escaped).

    A full (non-HTMX) render shows the messages waiting in the flash cookie and clears it.
    """
    if active_nav is not None and active_nav not in NAV_KEYS:
        raise ValueError(f"unknown active_nav {active_nav!r}; use one of {sorted(NAV_KEYS)}")
    page = dict(context or {})
    flashed = [] if is_htmx(request) else flash.pending_messages(request)
    extra_messages = list(page.pop("messages", None) or [])
    ctx: dict[str, Any] = {
        "request": request,
        "env": layout_env(request),
        "nav_items": NAV_ITEMS,
        "active_nav": active_nav,
        "app_version": __version__,
        "csp_nonce": csp_nonce(request),
    }
    if "conn" not in page:
        ctx["conn"] = current_connection_status(request)
    ctx.update(page)
    ctx["messages"] = [*flashed, *extra_messages]
    snippets = [oob] if isinstance(oob, str) else list(oob)
    response = PageResponse(
        templates.get_template(template),
        ctx,
        status_code=status_code,
        headers=headers,
        oob=snippets,
    )
    response.headers.add_vary_header("HX-Request")
    if flashed:
        flash.clear(response)
    return response


def error_page(
    request: Request,
    status_code: int,
    title: str,
    message: str | None = None,
    *,
    command: str | None = None,
    icon: str = "alert",
    refresh: bool = False,
    headers: Mapping[str, str] | None = None,
) -> PageResponse:
    """A full error or notice page (``errors/error.html``) with the layout.

    ``message`` is plain words for the user and ``command`` a fix to run in Terminal; never pass
    exception text. The page shows no connection status (it may be what failed).
    ``refresh=True``: an HTMX request gets ``HX-Refresh``, so the browser reloads and shows this
    page with its fix instead of a generic error toast.
    """
    headers = dict(headers or {})
    if refresh and is_htmx(request):
        headers["HX-Refresh"] = "true"
    context = {
        "status_code": status_code,
        "title": title,
        "message": message,
        "command": command,
        "icon": icon,
        "conn": None,
    }
    return render(
        request,
        "errors/error.html",
        context,
        active_nav=None,
        status_code=status_code,
        headers=headers,
    )
