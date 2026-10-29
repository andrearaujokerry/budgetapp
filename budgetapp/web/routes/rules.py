"""Rules page: placeholder from the web shell; the CAT package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/rules")
def rules_page(request: Request) -> Response:
    return render(request, "rules/index.html", active_nav="rules")
