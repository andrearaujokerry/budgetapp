"""Import page: placeholder from the web shell; the CSV package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/import")
def imports_page(request: Request) -> Response:
    return render(request, "imports/index.html", active_nav="imports")
