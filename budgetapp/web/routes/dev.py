"""Developer pages: the UI kit at ``/dev/ui-kit`` (every component, live). Never in production."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from budgetapp.web.deps import SettingsDep
from budgetapp.web.templating import render

router = APIRouter()


@router.get("/dev/ui-kit")
def ui_kit(request: Request, settings: SettingsDep) -> Response:
    if settings.is_production:
        raise HTTPException(status_code=404)
    return render(request, "dev/ui_kit.html", active_nav=None)
