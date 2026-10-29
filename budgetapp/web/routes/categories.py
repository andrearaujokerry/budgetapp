"""Categories page: placeholder from the web shell; the CAT package replaces this module."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.templating import render

router = APIRouter()


@router.get("/categories")
def categories_page(request: Request) -> Response:
    return render(request, "categories/index.html", active_nav="categories")
