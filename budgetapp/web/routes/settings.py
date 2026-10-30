"""Settings page (``/settings``): budget period (BUD-2), alerts (ALR-1..3, SYNC-6), statements
(REL-3), a test Mac notification, and read-only facts about this installation (never a secret).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from budgetapp import clock
from budgetapp.config import Settings
from budgetapp.secrets import SecretStore
from budgetapp.services import app_settings
from budgetapp.web.deps import FormDep, NotifierDep, SecretStoreDep, SessionDep, SettingsDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, layout_env, render

router = APIRouter()
NAV = "settings"
SAVED = "Settings saved"
TEST_TITLE = "Budget Tracker"
TEST_MESSAGE = "Test notification: budget and sync alerts will look like this."


def _form_context(
    view: app_settings.SettingsView,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "view": view,
        "values": values if values is not None else app_settings.form_values(view),
        "errors": errors or {},
        "period_modes": app_settings.PERIOD_MODES,
    }


def _page_context(
    request: Request, settings: Settings, store: SecretStore, form: dict[str, Any]
) -> dict[str, Any]:
    demo = layout_env(request) == "demo"
    return {
        **form,
        "about": app_settings.about(settings, store, demo=demo),
        "demo": demo,
        "notify_sent": False,
    }


@router.get("/settings")
def settings_page(
    request: Request, session: SessionDep, settings: SettingsDep, store: SecretStoreDep
) -> Response:
    form = _form_context(app_settings.load(session))
    return render(
        request,
        "settings/index.html",
        _page_context(request, settings, store, form),
        active_nav=NAV,
    )


@router.post("/settings")
def save_settings(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    store: SecretStoreDep,
    form: FormDep,
) -> Response:
    try:
        view = app_settings.save(session, form)
    except app_settings.SettingsInvalid as err:
        session.rollback()
        view = app_settings.load(session)
        context = _form_context(view, app_settings.posted_values(view, form), err.errors)
        if is_htmx(request):
            return render(request, "settings/_form.html", context, active_nav=NAV, status_code=422)
        return render(
            request,
            "settings/index.html",
            _page_context(request, settings, store, context),
            active_nav=NAV,
            status_code=422,
        )
    session.commit()
    if is_htmx(request):
        return render(
            request,
            "settings/_form.html",
            _form_context(view),
            active_nav=NAV,
            oob=flash_oob("success", SAVED),
        )
    return redirect(request, "/settings", "success", SAVED)


@router.post("/settings/test-notification")
def test_notification(request: Request, notifier: NotifierDep, settings: SettingsDep) -> Response:
    demo = layout_env(request) == "demo"
    if demo:
        level, text = "info", "Demo data: no notification is sent."
    else:
        result = notifier.notify(TEST_TITLE, TEST_MESSAGE, subtitle=f"{settings.env} environment")
        if result.ok:
            level = "success"
            text = "Test notification sent. If none appeared, follow the steps on this page."
        else:
            level, text = "error", "macOS didn't accept the notification. Try ./run notify-test."
    if is_htmx(request):
        context = {"demo": demo, "notify_sent": level == "success", "sent_at": clock.now()}
        return render(
            request, "settings/_notify.html", context, active_nav=NAV, oob=flash_oob(level, text)
        )
    return redirect(request, "/settings", level, text)
