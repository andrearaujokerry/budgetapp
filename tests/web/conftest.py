"""Fixtures for web tests (``budgetapp/web/README.md`` § Tests).

* ``app``: ``create_app(settings, session_factory=<the test session factory>,
  gateway_factory=lambda s: fake_plaid, notifier=<recording notifier>,
  secret_store=<in-memory store>)``. It shares the test's connection, so a route sees what the
  test wrote with ``db`` (flushed or committed) and the test sees what the route committed;
  everything is rolled back after the test.
* ``client``: a TestClient at http://127.0.0.1:8766 that sends the app's own ``Origin`` on every
  request, like the app's pages do, so POSTs pass the security checks. It follows redirects;
  pass ``follow_redirects=False`` to check a 303. HTMX requests: ``headers={"HX-Request":
  "true"}``.
* ``raw_client``: the same client without default headers (security tests).
* ``make_app(settings=None, **overrides)`` and ``make_client(app, *, origin=True, **kwargs)``:
  other settings or wiring (e.g. ``make_app(load_settings("sandbox"))`` to see the Sandbox bar;
  nothing connects anywhere but the test database), or ``raise_server_exceptions=False``.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from budgetapp.config import Settings
from budgetapp.secrets import MemoryStore
from budgetapp.services.notify import RecordingNotifier
from budgetapp.web.app import create_app
from tests.fake_plaid import FakePlaidGateway

with warnings.catch_warnings():
    # Starlette 1.10 prefers the httpx2 package; httpx (requirements-dev.txt) works the same.
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

HTMX = {"HX-Request": "true"}


@pytest.fixture
def make_app(
    settings: Settings,
    session_factory: sessionmaker[Session],
    fake_plaid: FakePlaidGateway,
    notifier: RecordingNotifier,
    secrets: MemoryStore,
) -> Callable[..., FastAPI]:
    def make(app_settings: Settings | None = None, **overrides: Any) -> FastAPI:
        options: dict[str, Any] = {
            "session_factory": session_factory,
            "gateway_factory": lambda _settings: fake_plaid,
            "notifier": notifier,
            "secret_store": secrets,
        }
        options.update(overrides)
        return create_app(settings if app_settings is None else app_settings, **options)

    return make


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(app: FastAPI, *, origin: bool = True, **kwargs: Any) -> TestClient:
        url = app.state.settings.app_url
        headers = {"Origin": url} if origin else {}
        headers.update(kwargs.pop("headers", None) or {})
        client = TestClient(app, base_url=url, headers=headers, **kwargs)
        client.__enter__()  # runs the app's lifespan (startup schema check)
        clients.append(client)
        return client

    yield make
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def app(make_app: Callable[..., FastAPI]) -> FastAPI:
    return make_app()


@pytest.fixture
def client(app: FastAPI, make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(app)


@pytest.fixture
def raw_client(app: FastAPI, make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(app, origin=False)
