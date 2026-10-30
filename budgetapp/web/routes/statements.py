"""Statements page (INS-4, REL-3, Gate D): every statement period of a card, checked against the
Amex statement to the cent. The numbers come from ``services.statements`` only.

* ``GET /statements?account=<id>``: the card's cycles, newest first.
* ``POST /statements``: add a statement by closing date (``account_id``, ``closing_date``).
* ``GET /statements/<account_id>/<closing date>``: one cycle laid out like the Amex summary box.
* ``POST /statements/<account_id>/<closing date>``: save the values typed from the statement,
  a corrected closing date, the earliest statement's period start and notes.
* ``POST /statements/<account_id>/<closing date>/checked``: mark it checked (``checked=1``) or not.
* ``POST /statements/<account_id>/<closing date>/remove``: remove a statement the user added.

Forms are plain posts that redirect (303) on success and re-render with status 422 on errors.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from budgetapp import clock
from budgetapp.services import statements
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import redirect
from budgetapp.web.templating import render

router = APIRouter()

#: Fields the statement form posts (the reported amounts, then the rest).
FORM_FIELDS = (
    *(name for name, _ in statements.REPORTED_FIELDS),
    "closing_date",
    "period_start",
    "notes",
)


def _detail_url(account_id: int, closing_date: dt.date) -> str:
    return f"/statements/{account_id}/{closing_date.isoformat()}"


def _list_context(
    session: Any, account: int | None, *, add_form: dict[str, Any] | None = None
) -> dict[str, Any]:
    options = statements.accounts(session)
    if account is not None:
        selected = next((o for o in options if o.id == account), None)
        if selected is None:
            raise HTTPException(404)
    else:
        selected = statements.default_account(session, options)
    rows = statements.overview(session, selected.id) if selected else []
    closed = [r for r in rows if r.cycle.status != statements.OPEN]
    tabs = [
        {
            "key": str(o.id),
            "label": o.name + (" (CSV)" if o.is_manual else ""),
            "href": f"/statements?account={o.id}",
        }
        for o in options
        if not o.is_hidden or (selected and o.id == selected.id)
    ]
    return {
        "accounts": options,
        "account_tabs": tabs,
        "account": selected,
        "rows": rows,
        "current": next((r for r in rows if r.cycle.status == statements.OPEN), None),
        "latest": closed[0] if closed else None,
        "mismatches": [r for r in rows if r.check.failed],
        "today": clock.today(),
        "add_form": add_form or {"values": {}, "errors": {}},
    }


@router.get("/statements")
def statements_page(request: Request, session: SessionDep, account: int | None = None) -> Response:
    context = _list_context(session, account)
    return render(request, "statements/index.html", context, active_nav="statements")


@router.post("/statements")
def add_statement(request: Request, session: SessionDep, form: FormDep) -> Response:
    raw_account = str(form.get("account_id", "")).strip()
    if not raw_account.isdigit():
        raise HTTPException(404)
    account_id = int(raw_account)
    values = {"closing_date": str(form.get("closing_date", ""))}
    try:
        row = statements.add_statement(session, account_id, values["closing_date"])
    except LookupError:
        raise HTTPException(404) from None
    except statements.StatementError as err:
        session.rollback()
        context = _list_context(
            session, account_id, add_form={"values": values, "errors": err.errors}
        )
        return render(
            request,
            "statements/index.html",
            context,
            active_nav="statements",
            status_code=422,
        )
    closing = row.closing_date
    session.commit()
    return redirect(
        request,
        _detail_url(account_id, closing),
        "success",
        f"Statement closing {closing:%b} {closing.day}, {closing.year} added",
    )


def _detail_context(
    session: Any,
    account_id: int,
    closing_date: dt.date,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    found = statements.detail(session, account_id, closing_date)
    if found is None:
        raise HTTPException(404)
    return {
        "detail": found,
        "cycle": found.cycle,
        "summary": found.summary,
        "account": found.account,
        "today": clock.today(),
        "form_values": values if values is not None else _initial_values(found.cycle),
        "form_errors": errors or {},
        "reported_fields": statements.REPORTED_FIELDS,
    }


def _initial_values(cycle: statements.StatementCycle) -> dict[str, Any]:
    """The form as stored: Payments/Credits shown with its minus, as the statement prints it."""
    reported = cycle.reported
    payments_credits = reported.payments_credits
    return {
        "closing_date": cycle.closing_date.isoformat(),
        "period_start": (
            cycle.period_start_override.isoformat() if cycle.period_start_override else ""
        ),
        "previous_balance": reported.previous_balance,
        "payments_credits": -payments_credits if payments_credits else payments_credits,
        "new_charges": reported.new_charges,
        "fees": reported.fees,
        "interest": reported.interest,
        "new_balance": reported.new_balance,
        "notes": cycle.notes or "",
    }


@router.get("/statements/{account_id}/{closing_date}")
def statement_detail(
    request: Request, session: SessionDep, account_id: int, closing_date: dt.date
) -> Response:
    context = _detail_context(session, account_id, closing_date)
    return render(request, "statements/detail.html", context, active_nav="statements")


@router.post("/statements/{account_id}/{closing_date}")
def save_statement(
    request: Request, session: SessionDep, form: FormDep, account_id: int, closing_date: dt.date
) -> Response:
    values = {name: str(form.get(name, "")) for name in FORM_FIELDS if name in form}
    try:
        before = statements.find_cycle(session, account_id, closing_date)
        row = statements.save_statement(session, account_id, closing_date, values)
    except LookupError:
        raise HTTPException(404) from None
    except statements.StatementError as err:
        session.rollback()
        context = _detail_context(
            session, account_id, closing_date, values=values, errors=err.errors
        )
        return render(
            request,
            "statements/detail.html",
            context,
            active_nav="statements",
            status_code=422,
        )
    new_closing = row.closing_date
    combined = before is not None and before.statement_id not in (None, row.id)
    session.commit()
    message = (
        f"Statement saved and combined with the one Amex reported for {_long(new_closing)}"
        if combined
        else "Statement saved"
    )
    return redirect(request, _detail_url(account_id, new_closing), "success", message)


@router.post("/statements/{account_id}/{closing_date}/checked")
def mark_checked(
    request: Request, session: SessionDep, form: FormDep, account_id: int, closing_date: dt.date
) -> Response:
    checked = str(form.get("checked", "1")) == "1"
    try:
        statements.mark_checked(session, account_id, closing_date, checked=checked)
    except LookupError:
        raise HTTPException(404) from None
    except statements.StatementError as err:
        session.rollback()
        message = next(iter(err.errors.values()))
        return redirect(request, _detail_url(account_id, closing_date), "error", message)
    session.commit()
    message = "Marked as checked" if checked else "No longer marked as checked"
    return redirect(request, _detail_url(account_id, closing_date), "success", message)


@router.post("/statements/{account_id}/{closing_date}/remove")
def remove_statement(
    request: Request, session: SessionDep, account_id: int, closing_date: dt.date
) -> Response:
    try:
        removed = statements.remove_statement(session, account_id, closing_date)
    except LookupError:
        raise HTTPException(404) from None
    except statements.StatementError as err:
        session.rollback()
        message = next(iter(err.errors.values()))
        return redirect(request, _detail_url(account_id, closing_date), "error", message)
    session.commit()
    return redirect(
        request,
        f"/statements?account={account_id}",
        "success",
        f"Statement closing {_long(removed)} removed",
    )


def _long(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"
