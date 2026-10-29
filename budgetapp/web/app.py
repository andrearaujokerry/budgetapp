"""The FastAPI application (ARCHITECTURE §9): ``create_app()``.

What it wires up:

* ``app.state``: ``settings``, ``database`` (session factory built on first use + schema guard),
  ``gateway_factory`` / ``gateway`` (the Plaid gateway, built on first use), ``notifier``,
  ``secret_store`` and ``demo_name`` (set by ``serve --demo``). Routes reach them through
  ``budgetapp.web.deps``.
* ``/static`` and every router in ``routes.ALL_ROUTERS``. Each router runs the schema guard first
  (``deps.check_schema``): a database at another revision shows "Upgrade needed" (503) on every
  page; nothing ever migrates from the web app.
* The security middlewares (``security.py``) and the error pages. Every error page renders
  through ``templating.render()`` and never shows exception text: 404, 400 (bad parameters),
  "Upgrade needed", "Keychain approval needed", "Setup isn't finished" (a key missing from
  Keychain), "The database isn't responding" (503), a Plaid error (502) and 500. The process
  keeps running whatever happens, so the unattended production web agent recovers by itself
  once the cause is fixed.
* No OpenAPI docs routes (``/docs``, ``/redoc``, ``/openapi.json``), in every environment.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import anyio.to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse, Response

from budgetapp import __version__, secrets
from budgetapp.config import Settings, load_settings
from budgetapp.db import DatabaseUnavailable, SchemaMismatch
from budgetapp.plaid_gateway import PlaidGatewayError, PlaidTransportError, RealPlaidGateway
from budgetapp.secrets import KeychainApprovalNeeded, MissingSecretError, SecretStore
from budgetapp.services.notify import Notifier, get_notifier
from budgetapp.web.deps import Database, GatewayFactory, check_schema
from budgetapp.web.flash import flash_oob
from budgetapp.web.routes import ALL_ROUTERS
from budgetapp.web.security import (
    HostOriginMiddleware,
    SecurityHeadersMiddleware,
    apply_security_headers,
    csp_nonce,
)
from budgetapp.web.templating import error_page, is_htmx, render

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    gateway_factory: GatewayFactory | None = None,
    notifier: Notifier | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    """Build the web app for ``settings`` (default: ``load_settings()``).

    Defaults: an engine and session factory for the environment's database (built on the first
    request), ``RealPlaidGateway.from_settings`` (built on first use), ``get_notifier(settings)``
    and the process-wide secret store (the Keychain). Tests and ``serve --demo`` pass their own.
    """
    settings = settings if settings is not None else load_settings()
    store = secret_store if secret_store is not None else secrets.get_store()
    if gateway_factory is None:

        def gateway_factory(s: Settings) -> RealPlaidGateway:
            return RealPlaidGateway.from_settings(s, store=store)

    app = FastAPI(
        title="Budget Tracker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.database = Database(settings, session_factory)
    app.state.gateway_factory = gateway_factory
    app.state.gateway = None
    app.state.notifier = notifier if notifier is not None else get_notifier(settings)
    app.state.secret_store = store
    app.state.demo_name = None

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in ALL_ROUTERS:
        app.include_router(router, dependencies=[Depends(check_schema)])
    _install_error_handlers(app)
    # The last one added runs first: security headers wrap everything, refusals included.
    app.add_middleware(HostOriginMiddleware, web_port=settings.web_port)
    app.add_middleware(SecurityHeadersMiddleware, plaid_env=settings.plaid_env)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    database: Database = app.state.database
    try:
        await anyio.to_thread.run_sync(database.check_schema)
    except Exception as exc:  # the pages explain it; the app always starts
        log.warning("Startup schema check failed: %s", _summary(exc))
    yield
    database.dispose()


def _summary(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text.splitlines()[0] if text else 'no detail'}"


# --------------------------------------------------------------------------- error pages


_HTTP_TEXT = {
    400: ("Bad request", "The app couldn't use the values that were sent."),
    403: ("Not allowed", "That action was blocked."),
    405: ("Not allowed here", "That address doesn't accept this kind of request."),
    409: ("Something changed", "Something changed in the meantime. Reload the page and try again."),
    413: ("Too large", "That upload is too large."),
}


def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    headers = dict(exc.headers or {})
    if request.url.path.startswith("/static/"):
        return PlainTextResponse(
            HTTPStatus(exc.status_code).phrase, status_code=exc.status_code, headers=headers
        )
    if exc.status_code == 404:
        return render(request, "errors/404.html", active_nav=None, status_code=404, headers=headers)
    phrase = HTTPStatus(exc.status_code).phrase
    title, message = _HTTP_TEXT.get(exc.status_code, (phrase, None))
    if isinstance(exc.detail, str) and exc.detail and exc.detail != phrase:
        message = exc.detail  # written for the user by the route that raised it
    return error_page(request, exc.status_code, title, message, headers=headers)


def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    # 400, not FastAPI's JSON 422: htmx would swap a 422 body into the page.
    where = [".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()]
    log.info("Invalid request %s %s: %s", request.method, request.url.path, ", ".join(where))
    return error_page(
        request, 400, "Bad request", "Some of the values sent were missing or not valid."
    )


def _schema_mismatch(request: Request, exc: SchemaMismatch) -> Response:
    settings: Settings = request.app.state.settings
    demo_name = request.app.state.demo_name
    if settings.is_production:
        message = (
            "This version of the app needs a newer database layout. Run this in Terminal (it "
            "takes a backup first), then reload this page:"
        )
        command = "./run live"
    elif demo_name:
        message = "This demo database was made by another version of the app. Recreate it:"
        command = f"./run demo create {demo_name}"
    else:
        message = "The Sandbox database is behind this code. Run this in Terminal, then reload:"
        command = "./run migrate"
    log.warning("Schema check: %s", _summary(exc))
    return error_page(
        request, 503, "Upgrade needed", message, command=command, icon="refresh", refresh=True
    )


def _keychain_approval(request: Request, exc: KeychainApprovalNeeded) -> Response:
    log.warning("Keychain approval needed to read %r.", exc.account)
    if request.app.state.settings.is_production:
        title, message = secrets.keychain_approval_message()
        command = secrets.KEYCHAIN_FIX_COMMAND
    else:
        title = "Keychain approval needed"
        message = (
            "macOS didn't let the app read its passwords from your Keychain. Run this in "
            "Terminal and click Always Allow when asked, then reload:"
        )
        command = "./run doctor"
    return error_page(request, 503, title, message, command=command, icon="lock", refresh=True)


def _missing_secret(request: Request, exc: MissingSecretError) -> Response:
    log.warning("Keychain has no value for %r.", exc.account)
    account = exc.account
    if account == secrets.PLAID_CLIENT_ID or account.startswith("plaid.secret."):
        message = (
            "Your Plaid keys aren't saved yet, so the app can't reach Plaid. Save them in "
            "Terminal (typing is hidden), then reload:"
        )
        command = "./run setup --keys-only"
    elif account.startswith("db.password."):
        message = "The database password isn't in your Keychain. Run setup in Terminal:"
        command = "./run setup"
    else:
        message = "A key the app needs is missing from your Keychain. Check with:"
        command = "./run doctor"
    return error_page(
        request, 503, "Setup isn't finished", message, command=command, icon="lock", refresh=True
    )


def _database_unavailable(request: Request, exc: Exception) -> Response:
    log.warning("Database unavailable: %s", _summary(exc))
    return error_page(
        request,
        503,
        "The database isn't responding",
        "Nothing was changed. If this doesn't clear up in a minute, start the database from "
        "Terminal, then reload:",
        command="./run db start",
        icon="alert",
        refresh=True,
    )


def _plaid_error(request: Request, exc: PlaidGatewayError) -> Response:
    """A Plaid failure a route did not handle itself: a friendly message, never a crash."""
    log.warning("Plaid error: %s %s", exc.error_code, exc.error_message)
    if isinstance(exc, PlaidTransportError):
        text = "Plaid couldn't be reached. Check your internet connection and try again."
    else:
        text = exc.display_message or exc.error_message or "Plaid couldn't do that just now."
    if is_htmx(request):
        # Swap nothing, show the message as an error toast (out-of-band swaps still apply).
        body: Markup = flash_oob("error", text)
        return Response(str(body), media_type="text/html", headers={"HX-Reswap": "none"})
    return error_page(request, 502, "Plaid couldn't do that", text, icon="link")


def _server_error(request: Request, exc: Exception) -> Response:
    """500 page. Runs outside the middlewares (Starlette's ServerErrorMiddleware), so it sets the
    security headers itself; the exception is re-raised afterwards and logged (redacted) by the
    server."""
    try:
        response: Response = render(
            request, "errors/500.html", {"conn": None}, active_nav=None, status_code=500
        )
    except Exception:
        log.exception("The 500 page failed to render")
        response = PlainTextResponse(
            "Something went wrong. The details are in the app's log.", status_code=500
        )
    apply_security_headers(
        response.headers,
        nonce=csp_nonce(request),
        plaid_env=request.app.state.settings.plaid_env,
    )
    return response


def _install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(SchemaMismatch, _schema_mismatch)
    app.add_exception_handler(KeychainApprovalNeeded, _keychain_approval)
    app.add_exception_handler(MissingSecretError, _missing_secret)
    app.add_exception_handler(DatabaseUnavailable, _database_unavailable)
    app.add_exception_handler(OperationalError, _database_unavailable)
    app.add_exception_handler(PlaidGatewayError, _plaid_error)
    app.add_exception_handler(Exception, _server_error)
