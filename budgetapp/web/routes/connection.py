"""Connection page: placeholder from the web shell; the SYNC package replaces this module.

The two sync routes keep the top-bar "Sync now" button (``components/sync_status.html``, on every
page) honest until then: it answers "Syncing isn't built yet" instead of a 404.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

NOT_BUILT = "Syncing isn't built yet."


@router.get("/connection")
def connection_page(request: Request) -> Response:
    return render(request, "connection/index.html", active_nav="connection")


@router.post("/connection/sync")
def sync_now(request: Request) -> Response:
    """The Sync now button: the status partial plus a toast (no JS: back to this page)."""
    if is_htmx(request):
        return render(
            request,
            "components/sync_status.html",
            active_nav="connection",
            oob=flash_oob("info", NOT_BUILT),
        )
    return redirect(request, "/connection", "info", NOT_BUILT)


@router.get("/connection/sync-status")
def sync_status(request: Request) -> Response:
    """Polled while a sync runs: the current status partial."""
    return render(request, "components/sync_status.html", active_nav="connection")
