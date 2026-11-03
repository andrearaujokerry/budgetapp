"""FastAPI dependencies: settings, database sessions, the Plaid gateway, notifier, secret store
and posted forms.

Routes get everything through these (never from ``app.state`` directly), usually via the
``Annotated`` aliases::

    from budgetapp.web.deps import SessionDep

    @router.post("/rules/{rule_id}/delete")
    def delete_rule(request: Request, rule_id: int, session: SessionDep) -> Response:
        rules.delete_rule(session, rule_id)
        session.commit()  # routes own the transaction
        ...

``get_session`` yields one Session per request: it rolls back when the route raises and always
closes. The route calls ``session.commit()`` after a successful change.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import FormData

from budgetapp import db
from budgetapp.config import Settings
from budgetapp.plaid_gateway import PlaidGateway
from budgetapp.secrets import SecretStore
from budgetapp.services.notify import Notifier

#: ``create_app(gateway_factory=…)``: builds the gateway for the app's settings, on first use.
GatewayFactory = Callable[[Settings], PlaidGateway]

_gateway_lock = threading.Lock()


class Database:
    """The app's session factory (built on first use) and its schema check (``app.state.database``).

    Building the engine reads the database password from Keychain, which can need the user's
    approval (``KeychainApprovalNeeded``) when the production web agent runs unattended. Building
    it on first use lets the app start anyway, show a page with the fix, and try again on the next
    request.
    """

    def __init__(
        self, settings: Settings, session_factory: sessionmaker[Session] | None = None
    ) -> None:
        self.settings = settings
        self._factory = session_factory
        self._engine: Engine | None = None
        self._lock = threading.Lock()
        self.schema_ok = False

    @property
    def session_factory(self) -> sessionmaker[Session]:
        factory = self._factory
        if factory is None:
            with self._lock:
                if self._factory is None:
                    engine = db.make_engine(self.settings)
                    self._engine = engine
                    self._factory = db.make_session_factory(engine)
                factory = self._factory
        return factory

    def check_schema(self) -> None:
        """Raise ``db.SchemaMismatch`` / ``db.DatabaseUnavailable`` unless the schema is at head.

        A success is remembered for the life of the app; a failure is checked again on the next
        request (the database may come back, or be upgraded).
        """
        if self.schema_ok:
            return
        db.schema_guard(self.session_factory.kw["bind"])
        self.schema_ok = True

    def dispose(self) -> None:
        """Close the engine's connections (only when this object built the engine)."""
        if self._engine is not None:
            self._engine.dispose()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """The ``sessionmaker`` (for work that needs several transactions, e.g. background syncs)."""
    return request.app.state.database.session_factory


def get_session(request: Request) -> Iterator[Session]:
    """One Session per request: rolled back if the route raises, always closed."""
    session = get_session_factory(request)()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_gateway(request: Request) -> PlaidGateway:
    """The Plaid gateway, built once on first use (it reads the Plaid keys from Keychain).

    Raises ``secrets.MissingSecretError`` while the keys are not saved; the app shows a page that
    says to run ``./run setup --keys-only``.
    """
    state = request.app.state
    gateway = state.gateway
    if gateway is None:
        with _gateway_lock:
            if state.gateway is None:
                state.gateway = state.gateway_factory(state.settings)
            gateway = state.gateway
    return gateway


def get_notifier(request: Request) -> Notifier:
    return request.app.state.notifier


def get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store


async def get_form(request: Request) -> AsyncIterator[FormData]:
    """The posted form (urlencoded or multipart), parsed before a ``def`` route runs.

    Every field arrives as text (uploads as ``UploadFile``), so the route validates it and
    re-renders the form with messages (status 422) instead of FastAPI's generic 400 page for a
    malformed typed ``Form()`` parameter. Read values with ``form.get(name, "")`` and lists
    with ``form.getlist(name)``. Uploaded files are closed after the request.
    """
    form = await request.form()
    try:
        yield form
    finally:
        await form.close()


def check_schema(request: Request) -> None:
    """Router-level dependency on every page: the schema guard (ARCHITECTURE §2)."""
    request.app.state.database.check_schema()


SettingsDep = Annotated[Settings, Depends(get_settings)]
FormDep = Annotated[FormData, Depends(get_form)]
SessionDep = Annotated[Session, Depends(get_session)]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
GatewayDep = Annotated[PlaidGateway, Depends(get_gateway)]
NotifierDep = Annotated[Notifier, Depends(get_notifier)]
SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
