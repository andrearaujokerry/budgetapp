"""The Plaid gateway ``./run serve --demo NAME`` uses: nothing ever leaves the Mac.

:class:`DemoPlaidGateway` implements every method of the
:class:`~budgetapp.plaid_gateway.PlaidGateway` protocol, and each one raises
:class:`~budgetapp.plaid_gateway.PlaidError` with the plain message "Not available with demo data",
so a page that would link, sync or refresh shows a friendly error instead of crashing. The error is
classified like any other Plaid error the app shows (not "reconnect needed", not retried).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from budgetapp.plaid_gateway import ExchangeResult, PlaidError

DEMO_MESSAGE = "Not available with demo data"
DEMO_ERROR_TYPE = "INVALID_REQUEST"
DEMO_ERROR_CODE = "DEMO_MODE"


def demo_error() -> PlaidError:
    """The error every demo gateway call raises (HTTP 400, ``INVALID_REQUEST``/``DEMO_MODE``)."""
    return PlaidError(
        400,
        DEMO_ERROR_TYPE,
        DEMO_ERROR_CODE,
        f"{DEMO_MESSAGE}: this server shows a demo database, so it never calls Plaid.",
        DEMO_MESSAGE,
        None,
    )


class DemoPlaidGateway:
    """A ``PlaidGateway`` whose every call raises :func:`demo_error` (nothing leaves the Mac)."""

    plaid_env = "sandbox"

    def _refuse(self) -> NoReturn:
        raise demo_error()

    def create_link_token(
        self, *, access_token: str | None = None, additional_consented_products: Sequence[str] = ()
    ) -> str:
        self._refuse()

    def exchange_public_token(self, public_token: str) -> ExchangeResult:
        self._refuse()

    def transactions_sync(self, access_token: str, cursor: str | None) -> dict[str, Any]:
        self._refuse()

    def transactions_refresh(self, access_token: str) -> None:
        self._refuse()

    def get_item(self, access_token: str) -> dict[str, Any]:
        self._refuse()

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        self._refuse()

    def get_liabilities(self, access_token: str) -> dict[str, Any]:
        self._refuse()

    def search_institutions(self, query: str, products: Sequence[str]) -> dict[str, Any]:
        self._refuse()

    def sandbox_create_public_token(
        self,
        institution_id: str,
        *,
        override_username: str | None = None,
        override_password: str | None = None,
        initial_products: Sequence[str] = ("transactions",),
    ) -> str:
        self._refuse()

    def sandbox_reset_login(self, access_token: str) -> None:
        self._refuse()

    def sandbox_create_transactions(
        self, access_token: str, account_id: str, transactions: Sequence[Mapping[str, Any]]
    ) -> None:
        self._refuse()
