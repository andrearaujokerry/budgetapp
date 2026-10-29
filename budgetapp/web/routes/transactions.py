"""Transactions page: placeholder from the web shell; the TXN package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/transactions")
def transactions_page(request: Request) -> Response:
    return render(request, "transactions/index.html", active_nav="transactions")
