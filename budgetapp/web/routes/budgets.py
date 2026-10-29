"""Budgets page: placeholder from the web shell; the BUD package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/budgets")
def budgets_page(request: Request) -> Response:
    return render(request, "budgets/index.html", active_nav="budgets")
