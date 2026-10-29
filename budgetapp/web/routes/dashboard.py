"""Dashboard page: placeholder from the web shell; the DASH package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/")
def dashboard_page(request: Request) -> Response:
    return render(request, "dashboard/index.html", active_nav="dashboard")
