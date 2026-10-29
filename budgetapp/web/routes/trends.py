"""Trends page: placeholder from the web shell; the DASH package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/trends")
def trends_page(request: Request) -> Response:
    return render(request, "trends/index.html", active_nav="trends")
