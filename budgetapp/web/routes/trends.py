"""Trends page (INS-2): spending per category per calendar month, with the change from last
month.

* ``GET /trends?months=12|24|36&through=YYYY-MM&category=<id>``: ``months`` ending with
  ``through`` (default: this month); ``category`` shows one category month by month. With
  ``HX-Request`` (the filter bar) only the results (``trends/_results.html``) are returned.

Numbers come from :func:`budgetapp.services.insights.trends` (``spending.monthly_spending``:
calendar months by purchase date, the budgets' pending setting). A month links to the
Transactions page with the Budgets page's own :func:`~budgetapp.web.routes.budgets.
transactions_url` (category, the month's first and last day, ``status=posted`` when pending
charges don't count), which filters by the same purchase date.
"""

from __future__ import annotations

import re
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
from budgetapp.services.insights import Trends
from budgetapp.web.deps import SessionDep
from budgetapp.web.routes.budgets import transactions_url
from budgetapp.web.templating import is_htmx, month_label, render

router = APIRouter()

NAV = "trends"
MAX_ID = 2**63 - 1
_ID = re.compile(r"\s*[0-9]{1,19}\s*")


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


def _categories(session: Session) -> list[Category]:
    """Spending categories for the picker (hidden ones too: their spending counts)."""
    return list(
        session.scalars(
            select(Category).where(Category.kind == "expense").order_by(func.lower(Category.name))
        )
    )


def _context(session: Session, t: Trends, *, bad_category: bool) -> dict[str, Any]:
    pending = t.include_pending

    def month_href(category_id: int | None, period: Period) -> str:
        return transactions_url(category_id, period.start, period.end, include_pending=pending)

    def trends_href(category_id: int | None = None) -> str:
        params = {"months": t.span, "through": t.through.start.strftime("%Y-%m")}
        if category_id is not None:
            params["category"] = category_id
        return "/trends?" + urlencode(params)

    statement_mode = get_app_settings(session).budget_period_mode == "statement"
    return {
        "t": t,
        "chart": chart_config(t),
        "month_href": month_href,
        "trends_href": trends_href,
        "short_month": short_month,
        "categories": _categories(session),
        "bad_category": bad_category,
        "statement_mode": statement_mode,
        "filters": {
            "months": str(t.span),
            "through": t.through.start.strftime("%Y-%m"),
            "category": str(t.selected_ref.id) if t.selected_ref else "",
        },
    }


@router.get("/trends")
def trends_page(
    request: Request,
    session: SessionDep,
    months: str = "",
    through: str = "",
    category: str = "",
) -> Response:
    category_id, bad = _category_id(category)
    t = insights.trends(
        session,
        through=insights.month_param(through),
        span=_span(months),
        category_id=category_id,
        today=clock.today(),
    )
    bad = bad or (category_id is not None and t.selected_ref is None)
    context = _context(session, t, bad_category=bad)
    template = "trends/_results.html" if is_htmx(request) else "trends/index.html"
    return render(request, template, context, active_nav=NAV)
