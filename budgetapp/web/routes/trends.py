"""Trends page (INS-2): spending per category per calendar month, with the change from last
month; with the "Show income" switch, income and net per month (wave 3, INC-4).

* ``GET /trends?months=12|24|36&through=YYYY-MM&category=<id>``: ``months`` ending with
  ``through`` (default: this month); ``category`` shows one category month by month. With
  ``HX-Request`` (the filter bar) only the results (``trends/_results.html``) are returned.
* ``POST /trends/show-income``: the "Show income" switch (INC-2): saves
  ``app_settings.show_income``. An HTMX request sends the filter bar's values too
  (``hx-include``) and gets the results for them back, with a toast; a plain form post is
  redirected to ``next`` (this page with its filters).

Numbers come from :func:`budgetapp.services.insights.trends` (``spending.monthly_spending`` and
``monthly_income``: calendar months by purchase date, the budgets' pending setting). A month
links to the Transactions page with the Budgets page's own :func:`~budgetapp.web.routes.
budgets.transactions_url` (category, the month's first and last day, ``status=posted`` when
pending charges don't count), which filters by the same purchase date; an income figure's link
names its income categories (:func:`~budgetapp.web.routes.dashboard.categories_url`), so the
list's Net is the figure, signed as Plaid signs money in (negative).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import Category, get_app_settings
from budgetapp.periods import Period
from budgetapp.services import insights
from budgetapp.services.insights import IncomeSwitch, Trends
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, is_local_path, redirect
from budgetapp.web.routes.budgets import transactions_url
from budgetapp.web.routes.dashboard import categories_url, switch_message
from budgetapp.web.templating import is_htmx, month_label, render

router = APIRouter()

NAV = "trends"
MAX_ID = 2**63 - 1
_ID = re.compile(r"\s*[0-9]{1,19}\s*")
SWITCH_PATH = "/trends/show-income"
#: The "Income and spending" chart (INC-5): spending keeps the ink accent, income takes one
#: palette colour (teal: readable on both themes' cards), and the net is a dashed line in the
#: muted grey of the app's other dashed lines (the pace chart's previous period, a category's
#: monthly average). Never a palette slot for the net: each slot is some category's colour in
#: the "Spending by month" chart right below (cat-1 is Travel's), so it would read as one.
INCOME_COLOR = "cat-16"
NET_COLOR = "muted"


def _category_id(value: str) -> tuple[int | None, bool]:
    """``(id, bad)``: the id a ``category`` value names; ``bad`` when one was given but isn't
    an id (a hand-edited link)."""
    if not value or not value.strip():
        return None, False
    if not _ID.fullmatch(value):
        return None, True
    number = int(value)
    return (number, False) if 0 < number <= MAX_ID else (None, True)


def _span(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        return insights.DEFAULT_SPAN
    return number if number in insights.SPANS else insights.DEFAULT_SPAN


def short_month(period: Period, *, year: bool = False) -> str:
    """``Oct`` (``Oct 2026`` with ``year``)."""
    return month_label(period.start) if year else f"{period.start:%b}"


def chart_config(t: Trends) -> dict[str, Any]:
    """Stacked monthly bars by category (each in its own colour, the tail folded into Other);
    one category: its bars and a dashed line at its monthly average."""
    labels = [month_label(m.period.start) for m in t.months]
    series = t.chart_series()
    if t.selected is not None:
        ref = t.selected_ref
        datasets: list[dict[str, Any]] = [
            {
                "label": ref.name if ref else "Spending",
                "data": list(t.selected.amounts),
                "color": (ref.color if ref else None) or "accent",
            }
        ]
        if t.selected.average is not None:
            datasets.append(
                {
                    "label": "Monthly average",
                    "type": "line",
                    "data": [t.selected.average if m.has_data else None for m in t.months],
                    "color": "muted",
                    "dashed": True,
                }
            )
        return {"type": "bar", "data": {"labels": labels, "datasets": datasets}}
    datasets = [
        {"label": s.label, "data": list(s.amounts), "color": s.color or "other"} for s in series
    ]
    return {"type": "bar", "stacked": True, "data": {"labels": labels, "datasets": datasets}}


def income_chart_config(t: Trends) -> dict[str, Any] | None:
    """The "Income and spending" chart (INC-4, INC-5): income bars next to spending bars per
    month and the net as a dashed line; ``None`` without income. The net is listed first
    because Chart.js draws the first dataset last, so the line stays on top of the bars."""
    if t.income is None:
        return None
    labels = [month_label(m.period.start) for m in t.months]
    datasets = [
        {
            "label": "Net (income − spending)",
            "type": "line",
            "data": list(t.income.net),
            "color": NET_COLOR,
            "dashed": True,
        },
        {"label": "Income", "data": list(t.income.totals.amounts), "color": INCOME_COLOR},
        {"label": "Spending", "data": list(t.totals.amounts), "color": "accent"},
    ]
    return {"type": "bar", "data": {"labels": labels, "datasets": datasets}}


def _categories(session: Session) -> list[Category]:
    """Spending categories for the picker (hidden ones too: their spending counts)."""
    return list(
        session.scalars(
            select(Category).where(Category.kind == "expense").order_by(func.lower(Category.name))
        )
    )


def _filters(t: Trends) -> dict[str, str]:
    return {
        "months": str(t.span),
        "through": t.through.start.strftime("%Y-%m"),
        "category": str(t.selected_ref.id) if t.selected_ref else "",
    }


def _context(
    session: Session, t: Trends, *, bad_category: bool, switch: IncomeSwitch
) -> dict[str, Any]:
    pending = t.include_pending

    def month_href(category_id: int | None, period: Period) -> str:
        return transactions_url(category_id, period.start, period.end, include_pending=pending)

    def income_href(category_ids: Iterable[int], period: Period) -> str | None:
        """The month's list of these income categories (``None`` without any)."""
        ids = list(category_ids)
        if not ids:
            return None
        return categories_url(ids, period.start, period.end, include_pending=pending)

    def trends_href(category_id: int | None = None) -> str:
        params = {"months": t.span, "through": t.through.start.strftime("%Y-%m")}
        if category_id is not None:
            params["category"] = category_id
        return "/trends?" + urlencode(params)

    filters = _filters(t)
    statement_mode = get_app_settings(session).budget_period_mode == "statement"
    return {
        "t": t,
        "chart": chart_config(t),
        "income_chart": income_chart_config(t),
        "month_href": month_href,
        "income_href": income_href,
        "trends_href": trends_href,
        "short_month": short_month,
        "categories": _categories(session),
        "bad_category": bad_category,
        "statement_mode": statement_mode,
        "switch": switch,
        "switch_path": SWITCH_PATH,
        # Income shows with all categories (``t.income`` is there while the switch shows it);
        # one spending category's view stays spending only and says where income shows.
        "show_income": t.income is not None and t.selected_ref is None,
        "income_elsewhere": t.income is not None and t.selected_ref is not None,
        # Where the switch's plain form post returns: this page with these filters.
        "switch_next": "/trends?" + urlencode({k: v for k, v in filters.items() if v}),
        "filters": filters,
    }


def _respond(
    request: Request,
    session: Session,
    *,
    months: str,
    through: str,
    category: str,
    template: str,
    oob: Any = (),
) -> Response:
    category_id, bad = _category_id(category)
    switch = insights.income_switch(session)
    t = insights.trends(
        session,
        through=insights.month_param(through),
        span=_span(months),
        category_id=category_id,
        today=clock.today(),
        income=switch.showing,
    )
    bad = bad or (category_id is not None and t.selected_ref is None)
    context = _context(session, t, bad_category=bad, switch=switch)
    return render(request, template, context, active_nav=NAV, oob=oob)


@router.get("/trends")
def trends_page(
    request: Request,
    session: SessionDep,
    months: str = "",
    through: str = "",
    category: str = "",
) -> Response:
    template = "trends/_results.html" if is_htmx(request) else "trends/index.html"
    return _respond(
        request, session, months=months, through=through, category=category, template=template
    )


@router.post(SWITCH_PATH)
def show_income(request: Request, session: SessionDep, form: FormDep) -> Response:
    """The "Show income" switch (INC-2): an unchecked switch sends nothing, so a missing
    ``show_income`` is "off". The filters come from the filter bar (``hx-include``)."""
    on = bool(form.get("show_income"))
    insights.set_show_income(session, on)
    session.commit()
    message = switch_message(on)
    if not is_htmx(request):
        target = form.get("next", "")
        if not (is_local_path(target) and str(target).split("?", 1)[0] == "/trends"):
            target = "/trends"
        return redirect(request, str(target), "info", message)

    def text(name: str) -> str:
        value = form.get(name, "")
        return value if isinstance(value, str) else ""

    return _respond(
        request,
        session,
        months=text("months"),
        through=text("through"),
        category=text("category"),
        template="trends/_results.html",
        oob=flash_oob("info", message),
    )
