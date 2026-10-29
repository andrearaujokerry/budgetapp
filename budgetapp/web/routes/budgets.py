"""Budgets page (BUD-1..5): monthly budgets with rollover, the overall target and annual budgets.

* ``GET  /budgets?period=YYYY-MM-DD``: the budget period containing that day (default: today).
* ``POST /budgets/category/{id}``: monthly amount; empty = no budget from this period on.
* ``POST /budgets/category/{id}/annual``: annual amount; empty = none from this period on.
* ``POST /budgets/category/{id}/rollover``: rollover on (``enabled=1``) or off (field missing).
* ``POST /budgets/overall``: the overall monthly target; empty = none.
* ``POST /budgets/add``: a monthly or annual budget for any visible spending category.

Every POST carries ``period``, a day inside the budget period being edited (its middle, see
:func:`form_day`): the change applies from that period on and earlier periods keep their amounts
(``services.budgets``). An HTMX edit of
a row answers with that row plus out-of-band updates of the summaries it changes; adding a budget
(or switching a category between monthly and annual) reloads the page (``HX-Redirect``) so the
row lands in its sorted place. Plain form posts redirect back to the page (303). Invalid amounts
come back with status 422 and the message next to the field.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.models import Category
from budgetapp.money import format_money
from budgetapp.periods import MONTH, Period
from budgetapp.services import budgets
from budgetapp.services.budgets import ANNUAL, MONTHLY, BudgetChange, BudgetError, BudgetOverview
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import date_long, date_short, is_htmx, render

router = APIRouter()

NAV = "budgets"

#: Years the page shows. Others are refused (400): the period navigator, rollover and
#: suggestions step past a period's ends, which overflows near years 1 and 9999.
YEARS = range(1900, 3000)


# --------------------------------------------------------------------------- helpers


def page_url(period: Period | None = None) -> str:
    """``/budgets`` for a period (the current one without a parameter)."""
    return "/budgets" if period is None else f"/budgets?period={period.start.isoformat()}"


def form_day(period: Period) -> dt.date:
    """The day a form names its period by: the middle of it, so that a closing date which moves
    by a few days after the page was drawn (a new or corrected statement) still finds that
    cycle rather than its neighbour."""
    return period.start + (period.end - period.start) // 2


def transactions_url(category_id: int | None, start: dt.date, end: dt.date) -> str:
    """The Transactions page filtered to a category and a date range (the "spent" links)."""
    category = "" if category_id is None else f"category={category_id}&"
    return f"/transactions?{category}from={start.isoformat()}&to={end.isoformat()}"


def period_label(period: Period, today: dt.date) -> str:
    """``Oct 2026``; a statement cycle as ``Sep 28 – Oct 27`` (with years when not this year)."""
    if period.kind == MONTH:
        return period.label
    if period.start.year == period.end.year == today.year:
        return f"{date_short(period.start)} – {date_short(period.end)}"
    return f"{date_long(period.start)} – {date_long(period.end)}"


def _labels(ov: BudgetOverview, suggestions: budgets.Suggestions) -> dict[str, str]:
    statement = ov.period.kind != MONTH
    averaged = suggestions.periods
    if not averaged:
        basis = ""
    elif statement:
        basis = (
            f"average of the last {len(averaged)} complete cycle{'s' if len(averaged) > 1 else ''}"
        )
    elif len(averaged) == 1:
        basis = f"spending in {averaged[0].label}"
    else:
        first, last = averaged[-1], averaged[0]
        span = f"{first.start:%b}–{last.start:%b %Y}"
        if first.start.year != last.start.year:
            span = f"{first.start:%b %Y}–{last.start:%b %Y}"
        basis = f"average of {span}"
    return {
        "period": period_label(ov.period, ov.today),
        "previous": period_label(ov.previous_period, ov.today),
        "next": period_label(ov.next_period, ov.today),
        "previous_short": "the previous cycle" if statement else f"{ov.previous_period.start:%b}",
        "kind": "Statement cycle" if statement else "Calendar month",
        "unit": "cycle" if statement else "month",
        "current_link": "This cycle" if statement else "This month",
        "suggest_basis": basis,
        "param": form_day(ov.period).isoformat(),
    }


def _context(session: Session, period: Period, today: dt.date, **extra: Any) -> dict[str, Any]:
    """Everything the page and its partials read."""
    ov = budgets.overview(session, period, today=today)
    suggestions = budgets.suggestions(session, today=today, before=period)
    context: dict[str, Any] = {
        "ov": ov,
        "annual": budgets.annual_overview(session, period.end.year, today=today, as_of=period.end),
        "sugg": suggestions,
        "labels": _labels(ov, suggestions),
        "spent_href": lambda category_id, start=period.start, end=period.end: transactions_url(
            category_id, start, end
        ),
        "categories": _visible_categories(session),
        "errors": {},
        "dialog": None,
    }
    context.update(extra)
    return context


def _visible_categories(session: Session) -> list[Category]:
    """Spending categories that can get a budget (hidden ones are left out)."""
    return list(
        session.scalars(
            select(Category)
            .where(Category.kind == "expense", Category.is_hidden.is_(False))
            .order_by(func.lower(Category.name))
        )
    )


def _period(session: Session, value: object, today: dt.date) -> Period:
    """The budget period a form's ``period`` field (a day inside it) names."""
    if not value:
        return budgets.period_for(session, today)
    try:
        day = dt.date.fromisoformat(str(value))
    except ValueError:
        day = None
    if day is None or day.year not in YEARS:
        raise HTTPException(400, "That budget period isn't valid. Reload the page and try again.")
    return budgets.period_for(session, day)


def _category(session: Session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404)
    return category


def _saved(name: str, change: BudgetChange, amount: Any, label: str, *, annual: bool) -> str:
    kind = "an annual" if annual else "a monthly"
    if amount is None:
        return f"{name} has no {'annual ' if annual else ''}budget from {label} on."
    text = f"{name} has {kind} budget of {format_money(amount)} from {label} on."
    if change.ended is not None:
        other = "monthly" if annual else "annual"
        text += f" Its {other} budget ends then."
    return text


def _page(request: Request, context: dict[str, Any], *, status_code: int = 200) -> Response:
    template = "budgets/_page.html" if is_htmx(request) else "budgets/index.html"
    return render(request, template, context, active_nav=NAV, status_code=status_code)


# --------------------------------------------------------------------------- page


@router.get("/budgets")
def budgets_page(request: Request, session: SessionDep, period: dt.date | None = None) -> Response:
    today = clock.today()
    if period is not None and period.year not in YEARS:
        raise HTTPException(
            400, "That period is too far away to show. Open Budgets from the menu for this month."
        )
    shown = budgets.period_for(session, period or today)
    return _page(request, _context(session, shown, today))


# --------------------------------------------------------------------------- monthly amounts


@router.post("/budgets/category/{category_id}")
def set_monthly_budget(
    request: Request, category_id: int, session: SessionDep, form: FormDep
) -> Response:
    """A monthly amount from a budget row, or "Add budget" on an unbudgeted row."""
    today = clock.today()
    category = _category(session, category_id)
    period = _period(session, form.get("period"), today)
    raw = str(form.get("amount", ""))
    adding = form.get("row") == "unbudgeted"
    try:
        amount = budgets.parse_amount(raw)
        if adding and amount is None:
            raise BudgetError("Enter an amount, like 250.")
        change = budgets.set_budget(session, category.id, amount, effective_from=period)
    except BudgetError as err:
        errors = {category.id: {"raw": raw, "message": str(err)}}
        context = _context(session, period, today, errors=errors, category=category)
        if not is_htmx(request):
            return _page(request, context, status_code=422)
        template = "budgets/_unbudgeted_row.html" if adding else "budgets/_line_update.html"
        return render(request, template, context, active_nav=NAV, status_code=422)
    session.commit()
    label = period_label(period, today)
    message = _saved(category.name, change, amount, label, annual=False)
    if adding or not is_htmx(request):
        return redirect(request, page_url(period), "success", message)
    context = _context(session, period, today, category=category)
    oob = flash_oob("success", message) if change.changed else ()
    return render(request, "budgets/_line_update.html", context, active_nav=NAV, oob=oob)


@router.post("/budgets/category/{category_id}/rollover")
def set_rollover(
    request: Request, category_id: int, session: SessionDep, form: FormDep
) -> Response:
    today = clock.today()
    category = _category(session, category_id)
    period = _period(session, form.get("period"), today)
    enabled = form.get("enabled") == "1"
    try:
        change = budgets.set_rollover(session, category.id, enabled, effective_from=period)
    except BudgetError as err:
        raise HTTPException(409, str(err)) from None
    session.commit()
    label = period_label(period, today)
    unit = "cycle" if period.kind != MONTH else "month"
    message = (
        f"Rollover is on for {category.name} from {label}: what's left carries into the "
        f"next {unit}."
        if enabled
        else f"Rollover is off for {category.name} from {label}."
    )
    if not is_htmx(request):
        return redirect(request, page_url(period), "success", message)
    context = _context(session, period, today, category=category)
    oob = flash_oob("success", message) if change.changed else ()
    return render(request, "budgets/_line_update.html", context, active_nav=NAV, oob=oob)


# --------------------------------------------------------------------------- annual amounts


@router.post("/budgets/category/{category_id}/annual")
def set_annual_budget(
    request: Request, category_id: int, session: SessionDep, form: FormDep
) -> Response:
    today = clock.today()
    category = _category(session, category_id)
    period = _period(session, form.get("period"), today)
    raw = str(form.get("amount", ""))
    try:
        amount = budgets.parse_amount(raw)
        change = budgets.set_budget(
            session, category.id, amount, effective_from=period, period=ANNUAL
        )
    except BudgetError as err:
        errors = {category.id: {"raw": raw, "message": str(err)}}
        context = _context(session, period, today, errors=errors, category=category)
        if not is_htmx(request):
            return _page(request, context, status_code=422)
        return render(
            request, "budgets/_annual_update.html", context, active_nav=NAV, status_code=422
        )
    session.commit()
    message = _saved(category.name, change, amount, period_label(period, today), annual=True)
    if not is_htmx(request) or change.ended is not None:
        return redirect(request, page_url(period), "success", message)
    context = _context(session, period, today, category=category)
    oob = flash_oob("success", message) if change.changed else ()
    return render(request, "budgets/_annual_update.html", context, active_nav=NAV, oob=oob)


# --------------------------------------------------------------------------- overall target


@router.post("/budgets/overall")
def set_overall_target(request: Request, session: SessionDep, form: FormDep) -> Response:
    today = clock.today()
    period = _period(session, form.get("period"), today)
    raw = str(form.get("amount", ""))
    try:
        amount = budgets.parse_amount(raw)
        change = budgets.set_budget(session, None, amount, effective_from=period)
    except BudgetError as err:
        errors = {"overall": {"raw": raw, "message": str(err)}}
        context = _context(session, period, today, errors=errors)
        if not is_htmx(request):
            return _page(request, context, status_code=422)
        return render(request, "budgets/_overall.html", context, active_nav=NAV, status_code=422)
    session.commit()
    label = period_label(period, today)
    message = (
        f"No overall target from {label} on."
        if amount is None
        else f"The overall target is {format_money(amount)} from {label} on."
    )
    if not is_htmx(request):
        return redirect(request, page_url(period), "success", message)
    context = _context(session, period, today)
    oob = flash_oob("success", message) if change.changed else ()
    return render(request, "budgets/_overall.html", context, active_nav=NAV, oob=oob)


# --------------------------------------------------------------------------- add a budget


@router.post("/budgets/add")
def add_budget(request: Request, session: SessionDep, form: FormDep) -> Response:
    """The "Add a budget" dialog: any visible spending category, monthly or annual."""
    today = clock.today()
    period = _period(session, form.get("period"), today)
    kind = ANNUAL if form.get("kind") == ANNUAL else MONTHLY
    raw = str(form.get("amount", ""))
    raw_category = str(form.get("category_id", ""))
    errors: dict[str, str] = {}
    category = None
    if raw_category.isdigit():
        category = session.get(Category, int(raw_category))
    if category is None or category.kind != "expense":
        errors["category_id"] = "Choose a category."
    amount = None
    try:
        amount = budgets.parse_amount(raw)
        if amount is None:
            errors["amount"] = "Enter an amount, like 250."
    except BudgetError as err:
        errors["amount"] = str(err)
    if not errors:
        assert category is not None
        try:
            change = budgets.set_budget(
                session, category.id, amount, effective_from=period, period=kind
            )
        except BudgetError as err:
            errors["category_id"] = str(err)
    if errors:
        dialog = {
            "kind": kind,
            "category_id": raw_category,
            "amount": raw,
            "errors": errors,
        }
        context = _context(session, period, today, dialog=dialog, kind=kind)
        if not is_htmx(request):
            return _page(request, context, status_code=422)
        return render(request, "budgets/_add_form.html", context, active_nav=NAV, status_code=422)
    session.commit()
    assert category is not None
    message = _saved(
        category.name, change, amount, period_label(period, today), annual=kind == ANNUAL
    )
    return redirect(request, page_url(period), "success", message)
