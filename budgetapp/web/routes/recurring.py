"""Recurring page (INS-3): subscriptions and other repeating charges, with their monthly cost and
next expected date (``services.recurring``).

* ``GET /recurring``: the page (an HTMX request gets the ``#recurring-content`` partial).
* ``POST /recurring/override``: ``key`` (the merchant key) and ``status`` = ``ignored``,
  ``confirmed`` or ``reset``. HTMX gets the refreshed content plus a toast; a plain post is
  redirected (303) back to the page.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from budgetapp.services import recurring
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

#: Form value → (override status, toast text).
ACTIONS = {
    "ignored": (recurring.IGNORED, "Ignored: {name} won't be listed as recurring."),
    "confirmed": (recurring.CONFIRMED, "Kept: {name} stays on the list."),
    "reset": (None, "{name} is back to automatic detection."),
}


@router.get("/recurring")
def recurring_page(request: Request, session: SessionDep) -> Response:
    context = {"overview": recurring.detect(session)}
    template = "recurring/_content.html" if is_htmx(request) else "recurring/index.html"
    return render(request, template, context, active_nav="recurring")


@router.post("/recurring/override")
def recurring_override(request: Request, session: SessionDep, form: FormDep) -> Response:
    action = ACTIONS.get(str(form.get("status", "")))
    key = str(form.get("key", ""))
    if action is None:
        raise HTTPException(400, "That action isn't available.")
    status, message = action
    name = str(form.get("name", "")).strip()[:80] or "This charge"
    try:
        recurring.set_override(session, key, status)
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    session.commit()
    text = message.format(name=name)
    if is_htmx(request):
        context = {"overview": recurring.detect(session)}
        return render(
            request,
            "recurring/_content.html",
            context,
            active_nav="recurring",
            oob=flash_oob("success", text),
        )
    return redirect(request, "/recurring", "success", text)
