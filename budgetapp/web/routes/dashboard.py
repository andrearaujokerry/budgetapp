"""Dashboard page (INS-1): spending against budget this period, top categories and merchants,
days left.

* ``GET /``: the current budget period (calendar month or statement cycle, as configured).

Every figure comes from :func:`budgetapp.services.insights.dashboard`, whose budget numbers are
``budgets.overview`` of the current period: the same figures as the Budgets page. Links into
``/transactions`` are built by the Budgets page's own :func:`~budgetapp.web.routes.budgets.
transactions_url` (category, the period's dates, ``basis=posted_date`` in statement mode and
``status=posted`` when pending charges don't count), so a list holds what its figure counts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp import clock
from budgetapp.money import ZERO
from budgetapp.periods import MONTH, STATEMENT
from budgetapp.services import insights
from budgetapp.services.insights import Dashboard
from budgetapp.web.deps import SessionDep
from budgetapp.web.routes.budgets import period_label, transactions_url
from budgetapp.web.templating import date_short, render

router = APIRouter()

NAV = "dashboard"


def labels(d: Dashboard) -> dict[str, str]:
    """The words the page uses for this period and the previous one."""
    ov = d.overview
    period, previous = ov.period, ov.previous_period
    statement = period.kind == STATEMENT
    end_word = "closes" if statement else "ends"
    if period.kind == MONTH:
        name = f"{period.start:%B %Y}"
        previous_name = f"{previous.start:%B}"
    else:
        name = period_label(period, d.today)
        previous_name = "the last cycle"
    return {
        "period": name,
        "heading": f"Statement cycle {name}" if statement else name,
        "unit": "cycle" if statement else "month",
        "this": "This cycle" if statement else "This month",
        "kind": "Statement cycle" if statement else "Calendar month",
        "previous": previous_name,
        "previous_title": "Last cycle" if statement else previous_name,
        "ends": f"{end_word} {date_short(period.end)}",
    }


def pace_chart(d: Dashboard, words: dict[str, str]) -> dict[str, Any]:
    """The chart config: spending added up day by day, this period and the previous one (by
    day number). Both lines start from $0 at a "Start" point before the first day, so even the
    first day of a period draws a line."""
    pace = d.pace
    datasets: list[dict[str, Any]] = [
        {
            "label": words["this"],
            "data": [ZERO, *pace.current],
            "color": "accent",
            "fill": True,
        }
    ]
    if any(value is not None for value in pace.previous):
        datasets.append(
            {
                "label": words["previous_title"],
                "data": [ZERO, *pace.previous],
                "color": "muted",
                "dashed": True,
            }
        )
    labels = ["Start", *(date_short(day) for day in pace.days)]
    return {"type": "line", "format": "usd0", "data": {"labels": labels, "datasets": datasets}}


def _context(d: Dashboard) -> dict[str, Any]:
    ov = d.overview
    basis, pending = ov.spending.basis, ov.spending.include_pending

    def spent_href(
        category_id: int | None, start: dt.date = ov.period.start, end: dt.date = ov.period.end
    ) -> str:
        return transactions_url(category_id, start, end, basis=basis, include_pending=pending)

    def merchant_href(merchant: str) -> str:
        return spent_href(None) + "&" + urlencode({"merchant": merchant})

    words = labels(d)
    return {
        "d": d,
        "ov": ov,
        "words": words,
        "spent_href": spent_href,
        "merchant_href": merchant_href,
        "pace_chart": pace_chart(d, words),
    }


@router.get("/")
def dashboard_page(request: Request, session: SessionDep) -> Response:
    d = insights.dashboard(session, today=clock.today())
    return render(request, "dashboard/index.html", _context(d), active_nav=NAV)
