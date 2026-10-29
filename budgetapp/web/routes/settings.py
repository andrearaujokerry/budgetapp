"""Settings page: placeholder from the web shell; the OPS package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request) -> Response:
    return render(request, "settings/index.html", active_nav="settings")
