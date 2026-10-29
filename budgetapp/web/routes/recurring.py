"""Recurring page: placeholder from the web shell; the STMT package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/recurring")
def recurring_page(request: Request) -> Response:
    return render(request, "recurring/index.html", active_nav="recurring")
