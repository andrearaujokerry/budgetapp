"""Dashboard page (INS-1): spending against budget this period, top categories and merchants,
days left; with the "Show income" switch, income and net (wave 3, INC-3), and every account's
balance.

* ``GET /``: the current budget period (calendar month or statement cycle, as configured).
* ``POST /dashboard/show-income``: the "Show income" switch (INC-2): saves
  ``app_settings.show_income``; an HTMX request gets the headline tiles back
  (``dashboard/_kpis.html``) with a toast, a plain form post a redirect to ``/``.

Every figure comes from :func:`budgetapp.services.insights.dashboard`, whose budget numbers are
``budgets.overview`` of the current period: the same figures as the Budgets page. Links into
``/transactions`` are built by the Budgets page's own :func:`~budgetapp.web.routes.budgets.
transactions_url` (category, the period's dates, ``basis=posted_date`` in statement mode and
``status=posted`` when pending charges don't count), so a list holds what its figure counts.
A top merchant's link adds ``merchant=`` and the spending categories its figure is in, so the
merchant's card payments and credits (which no figure counts) stay out of the list. The Income
tile's link names every income category with money in the period (:func:`categories_url`), so
its list's Net is the income figure, signed as Plaid signs money in (negative).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp import clock
from budgetapp.money import ZERO
from budgetapp.periods import MONTH, STATEMENT
from budgetapp.services import insights
from budgetapp.services.insights import Dashboard
from budgetapp.services.spending import BASIS_SPEND_DATE, MerchantSpending
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.routes.budgets import period_label, transactions_url
from budgetapp.web.templating import date_short, is_htmx, render

router = APIRouter()

NAV = "dashboard"
SWITCH_PATH = "/dashboard/show-income"


def switch_message(on: bool) -> str:
    """The toast after the "Show income" switch changed (both pages share the choice)."""
    if on:
        return "Income is shown on the Dashboard and Trends."
    return "Income is hidden on the Dashboard and Trends."


def categories_url(
    category_ids: Iterable[int],
    start: dt.date,
    end: dt.date,
    *,
    basis: str = BASIS_SPEND_DATE,
    include_pending: bool = True,
) -> str:
    """:func:`transactions_url` for several categories at once (``category=`` once per id): the
    links of the income figures, whose lists hold every income category's rows."""
    base = transactions_url(None, start, end, basis=basis, include_pending=include_pending)
    path, _, query = base.partition("?")
    chosen = urlencode([("category", str(cid)) for cid in category_ids])
    return f"{path}?{chosen}&{query}" if chosen else base


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

    def merchant_href(m: MerchantSpending) -> str:
        params = [("category", str(cid)) for cid in m.category_ids]
        return spent_href(None) + "&" + urlencode([*params, ("merchant", m.merchant)])

    income_href = None
    if d.income is not None and d.income.category_ids:
        income_href = categories_url(
            d.income.category_ids,
            ov.period.start,
            ov.period.end,
            basis=basis,
            include_pending=pending,
        )

    words = labels(d)
    return {
        "d": d,
        "ov": ov,
        "words": words,
        "spent_href": spent_href,
        "merchant_href": merchant_href,
        "income_href": income_href,
        "switch_path": SWITCH_PATH,
        "pace_chart": pace_chart(d, words),
    }


@router.get("/")
def dashboard_page(request: Request, session: SessionDep) -> Response:
    d = insights.dashboard(session, today=clock.today())
    return render(request, "dashboard/index.html", _context(d), active_nav=NAV)


@router.post(SWITCH_PATH)
def show_income(request: Request, session: SessionDep, form: FormDep) -> Response:
    """The "Show income" switch (INC-2): an unchecked switch sends nothing, so a missing
    ``show_income`` is "off"."""
    on = bool(form.get("show_income"))
    insights.set_show_income(session, on)
    session.commit()
    message = switch_message(on)
    if not is_htmx(request):
        return redirect(request, "/", "info", message)
    d = insights.dashboard(session, today=clock.today())
    return render(
        request, "dashboard/_kpis.html", _context(d), active_nav=NAV, oob=flash_oob("info", message)
    )
