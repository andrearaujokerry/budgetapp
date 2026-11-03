"""The Plaid gateway (ARCHITECTURE §7): the ONLY module that talks to Plaid.

:class:`PlaidGateway` is an allowlist — nothing that can move money (or remove an Item) exists
(SEC-3). :class:`RealPlaidGateway` uses plaid-python only for request models, auth and transport:
every call uses ``_preload_content=False`` with an explicit ``_request_timeout``; the body is read
inside the error handling (it arrives lazily) and parsed with ``json.loads(parse_float=Decimal)``,
so amounts are exact ``Decimal`` (JSON integers such as ``"limit": 5000`` stay ``int``; normalize
with ``money.to_money``). plaid-python's typed response models (floats) are never used.

Errors: ``plaid.ApiException`` → :class:`PlaidError`; urllib3 errors, non-JSON bodies, error
bodies that are not UTF-8 and the plaid-python TLS bug (``AttributeError``) →
:class:`PlaidTransportError`. Both subclass :class:`PlaidGatewayError`, so one ``except`` clause
and one set of attributes (``error_code``, ``error_message``, ``needs_reconnect``,
``restart_pagination`` …) handle every failed call.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import plaid
import urllib3
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.custom_sandbox_transaction import CustomSandboxTransaction
from plaid.model.institutions_search_request import InstitutionsSearchRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.personal_finance_category_version import PersonalFinanceCategoryVersion
from plaid.model.products import Products
from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_public_token_create_request_options import (
    SandboxPublicTokenCreateRequestOptions,
)
from plaid.model.sandbox_public_token_create_request_options_transactions import (
    SandboxPublicTokenCreateRequestOptionsTransactions,
)
from plaid.model.sandbox_transactions_create_request import SandboxTransactionsCreateRequest
from plaid.model.transactions_refresh_request import TransactionsRefreshRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

from budgetapp import secrets
from budgetapp.logsetup import register_secret
from budgetapp.money import to_money

if TYPE_CHECKING:
    from budgetapp.config import Settings

log = logging.getLogger(__name__)

CLIENT_NAME = "Amex Budget Tracker"
CLIENT_USER_ID = "owner-1"
DAYS_REQUESTED = 730
SYNC_PAGE_SIZE = 500
HOSTS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}

TIMEOUT_SYNC = (10, 180)
TIMEOUT_EXCHANGE = (10, 120)
TIMEOUT_DEFAULT = (10, 60)

#: Methods that exist on the gateway; nothing else may call Plaid (SEC-3).
ALLOWED_METHODS = frozenset(
    {
        "create_link_token",
        "exchange_public_token",
        "transactions_sync",
        "transactions_refresh",
        "get_item",
        "get_accounts",
        "get_liabilities",
        "search_institutions",
        "sandbox_create_public_token",
        "sandbox_reset_login",
        "sandbox_create_transactions",
    }
)
SANDBOX_ONLY_METHODS = frozenset(
    {"sandbox_create_public_token", "sandbox_reset_login", "sandbox_create_transactions"}
)

NEEDS_RECONNECT_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "PENDING_EXPIRATION",
        "PENDING_DISCONNECT",
        "ACCESS_NOT_GRANTED",
        "INVALID_CREDENTIALS",
        "ITEM_LOCKED",
        "USER_SETUP_REQUIRED",
        "USER_PERMISSION_REVOKED",
    }
)
TRIAL_LIMIT_CODE = "TRIAL_CONNECTION_LIMIT"
MUTATION_CODE = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
RETRYABLE_TYPES = frozenset({"RATE_LIMIT_EXCEEDED", "INSTITUTION_ERROR", "API_ERROR"})
RETRYABLE_CODES = frozenset({"INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE"})


class PlaidGatewayError(Exception):
    """Base of the errors a failed Plaid call raises: :class:`PlaidError` (Plaid answered with an
    error) and :class:`PlaidTransportError` (no usable answer). Every attribute exists on both.

    * ``classification``: ``needs_reconnect`` (fix with update mode), ``trial_limit`` (shown
      plainly, never retried) or ``error`` (retried on the next run).
    * ``retryable``: transient, worth retrying with backoff (rate limits, Plaid or institution
      outages, transport failures); never the Trial limit or a reconnect.
    * ``restart_pagination``: a ``/transactions/sync`` loop must discard the pages it fetched and
      restart from its original cursor (§8): a transport failure or
      ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION``.
    """

    status: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    display_message: str | None = None
    request_id: str | None = None
    error_code_reason: str | None = None

    @property
    def classification(self) -> str:
        return classify_error(self.error_code, self.error_type)

    @property
    def needs_reconnect(self) -> bool:
        return self.classification == "needs_reconnect"

    @property
    def is_trial_limit(self) -> bool:
        return self.error_code == TRIAL_LIMIT_CODE

    @property
    def is_mutation_during_pagination(self) -> bool:
        return self.error_code == MUTATION_CODE

    @property
    def retryable(self) -> bool:
        """Worth retrying within a run (with backoff); never for the Trial connection limit."""
        if self.is_trial_limit or self.needs_reconnect:
            return False
        return self.error_type in RETRYABLE_TYPES or self.error_code in RETRYABLE_CODES

    @property
    def restart_pagination(self) -> bool:
        """Restart the sync loop from its original cursor (transport failure or mutation)."""
        return self.is_mutation_during_pagination


class PlaidError(PlaidGatewayError):
    """An error response from Plaid (``plaid.ApiException`` translated); see the base class."""

    def __init__(
        self,
        status: int | None,
        error_type: str | None,
        error_code: str | None,
        error_message: str | None,
        display_message: str | None = None,
        request_id: str | None = None,
        *,
        error_code_reason: str | None = None,
    ) -> None:
        self.status = status
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = error_message
        self.display_message = display_message
        self.request_id = request_id
        self.error_code_reason = error_code_reason
        super().__init__(
            f"Plaid error {error_type}/{error_code} (HTTP {status}): {error_message}"
            + (f" [request {request_id}]" if request_id else "")
        )

    @classmethod
    def from_body(cls, status: int | None, body: Mapping[str, Any]) -> PlaidError:
        return cls(
            status,
            body.get("error_type"),
            body.get("error_code"),
            body.get("error_message"),
            body.get("display_message"),
            body.get("request_id"),
            error_code_reason=body.get("error_code_reason"),
        )


class PlaidTransportError(PlaidGatewayError):
    """Network failure, timeout or an unusable response body; transient (retried next run).

    ``error_type``/``error_code`` are ``TRANSPORT_ERROR``, ``error_message`` is the message,
    ``status``/``display_message``/``request_id`` are ``None``.
    """

    error_type = TRANSPORT_ERROR
    error_code = TRANSPORT_ERROR

    def __init__(self, message: str = "Plaid could not be reached") -> None:
        self.error_message = message
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        return True

    @property
    def restart_pagination(self) -> bool:
        return True


def classify_error(error_code: str | None, error_type: str | None = None) -> str:
    """``needs_reconnect`` | ``trial_limit`` | ``error`` for a Plaid error (ARCHITECTURE §7)."""
    if error_code in NEEDS_RECONNECT_CODES:
        return "needs_reconnect"
    if error_code == TRIAL_LIMIT_CODE:
        return "trial_limit"
    return "error"


def item_status_for(error: PlaidGatewayError) -> str:
    """The ``plaid_items.status`` an error leaves the Item in: ``needs_reconnect`` or ``error``."""
    return "needs_reconnect" if error.classification == "needs_reconnect" else "error"


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """Result of ``/item/public_token/exchange``. The token is hidden from ``repr``."""

    access_token: str = field(repr=False)
    item_id: str


@runtime_checkable
class PlaidGateway(Protocol):
    """Every Plaid call the app may make (an allowlist; read-only by construction, SEC-3).

    Responses are raw JSON dicts with ``Decimal`` amounts. Sandbox-only methods raise
    :class:`PlaidEnvironmentError` in production.
    """

    plaid_env: str

    def create_link_token(
        self, *, access_token: str | None = None, additional_consented_products: Sequence[str] = ()
    ) -> str: ...

    def exchange_public_token(self, public_token: str) -> ExchangeResult: ...

    def transactions_sync(self, access_token: str, cursor: str | None) -> dict[str, Any]: ...

    def transactions_refresh(self, access_token: str) -> None: ...

    def get_item(self, access_token: str) -> dict[str, Any]: ...

    def get_accounts(self, access_token: str) -> dict[str, Any]: ...

    def get_liabilities(self, access_token: str) -> dict[str, Any]: ...

    def search_institutions(self, query: str, products: Sequence[str]) -> dict[str, Any]: ...

    def sandbox_create_public_token(
        self,
        institution_id: str,
        *,
        override_username: str | None = None,
        override_password: str | None = None,
        initial_products: Sequence[str] = ("transactions",),
    ) -> str: ...

    def sandbox_reset_login(self, access_token: str) -> None: ...

    def sandbox_create_transactions(
        self, access_token: str, account_id: str, transactions: Sequence[Mapping[str, Any]]
    ) -> None: ...


class PlaidEnvironmentError(RuntimeError):
    """A call was refused because it does not belong to this Plaid environment."""


def check_token_environment(token: str, plaid_env: str) -> None:
    """Secondary guard: refuse a token whose prefix names the other environment.

    Prefixes (``access-sandbox-…``) are not a documented contract, so only a clear mismatch is
    refused; the stored environment is the primary guard.
    """
    other = "production" if plaid_env == "sandbox" else "sandbox"
    for kind in ("access", "public", "link"):
        if token.startswith(f"{kind}-{other}-"):
            raise PlaidEnvironmentError(
                f"A {other} {kind} token was used with the {plaid_env} Plaid environment."
            )


def parse_json(raw: bytes | str) -> dict[str, Any]:
    """Parse a Plaid body with exact decimals; non-JSON → :class:`PlaidTransportError`."""
    try:
        data = json.loads(raw, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        preview = raw[:200] if isinstance(raw, (bytes, str)) else b""
        raise PlaidTransportError(f"Plaid returned a non-JSON body: {preview!r}") from exc
    if not isinstance(data, dict):
        raise PlaidTransportError("Plaid returned JSON that is not an object")
    return data


def error_from_api_exception(exc: plaid.ApiException) -> PlaidError | PlaidTransportError:
    """Translate an ``ApiException`` (its ``.body`` is a str in both content modes)."""
    body = exc.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or "error_code" not in data:
        return PlaidTransportError(
            f"Plaid returned HTTP {exc.status} without a JSON error body ({exc.reason})"
        )
    return PlaidError.from_body(exc.status, data)


def call_raw(
    method: Callable[..., Any], request: Any, *, timeout: tuple[float, float] = TIMEOUT_DEFAULT
) -> dict[str, Any]:
    """Call a plaid-python endpoint and return its JSON body with ``Decimal`` amounts.

    RESEARCH T2.9c (corrected): with ``_preload_content=False`` the endpoint returns once the
    headers arrive and the body is read lazily at ``resp.data``, so the read happens inside the
    error handling too. plaid-python decodes an error body as UTF-8 before raising
    ``ApiException``, so a non-UTF-8 error page (a proxy's Latin-1 HTML) surfaces as
    ``UnicodeDecodeError``; that, too, is a transport error.
    """
    try:
        resp = method(request, _preload_content=False, _request_timeout=timeout)
        try:
            raw = resp.data
        finally:
            resp.release_conn()
    except plaid.ApiException as exc:
        raise error_from_api_exception(exc) from None
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise PlaidTransportError(f"{type(exc).__name__}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PlaidTransportError(
            f"Plaid returned an HTTP error with a body that is not UTF-8: {exc}"
        ) from exc
    except AttributeError as exc:
        # plaid-python bug: a TLS failure with retries disabled surfaces as
        # "'NoneType' object has no attribute 'decode'". Anything else is a real bug.
        if "decode" not in str(exc):
            raise
        raise PlaidTransportError(f"TLS/connection failure: {exc}") from exc
    return parse_json(raw)


def _refuse_real_client_in_tests(plaid_env: str) -> None:
    """Test processes use FakePlaidGateway; only ``./run test --sandbox`` may reach the Sandbox."""
    if os.environ.get("BUDGETAPP_ENV") != "test":
        return
    if os.environ.get("BUDGETAPP_LIVE_SANDBOX") and plaid_env == "sandbox":
        return
    raise PlaidEnvironmentError(
        "Tests must use FakePlaidGateway; only the live Sandbox tests (./run test --sandbox) may "
        "build a real Plaid client, and only for the Sandbox."
    )


class RealPlaidGateway:
    """The real Plaid API for one environment (``sandbox`` or ``production``).

    Building a real client (no ``client`` injected) is refused in a test process, except for the
    Sandbox under ``BUDGETAPP_LIVE_SANDBOX=1`` (``./run test --sandbox``).
    """

    def __init__(
        self,
        plaid_env: str,
        client_id: str | None = None,
        secret: str | None = None,
        *,
        client: Any = None,
    ) -> None:
        if plaid_env not in HOSTS:
            raise ValueError(f"unknown Plaid environment {plaid_env!r}")
        self.plaid_env = plaid_env
        if client is None:
            _refuse_real_client_in_tests(plaid_env)
            if not client_id or not secret:
                raise ValueError("client_id and secret are required")
            register_secret(client_id)
            register_secret(secret)
            configuration = plaid.Configuration(
                host=HOSTS[plaid_env], api_key={"clientId": client_id, "secret": secret}
            )
            client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
        self._client = client

    @classmethod
    def from_settings(
        cls, settings: Settings, store: secrets.SecretStore | None = None
    ) -> RealPlaidGateway:
        """Build from Keychain credentials for ``settings.plaid_env``. Never in the test env."""
        if settings.env == "test":
            raise PlaidEnvironmentError(
                "Tests must use FakePlaidGateway, never the real Plaid API."
            )
        client_id, secret = secrets.plaid_credentials(settings.plaid_env, store=store)
        return cls(settings.plaid_env, client_id, secret)

    # -- helpers ---------------------------------------------------------------------------

    def _token(self, token: str) -> str:
        if not token:
            raise ValueError("a token is required")
        check_token_environment(token, self.plaid_env)
        register_secret(token)
        return token

    def _require_sandbox(self, name: str) -> None:
        if self.plaid_env != "sandbox":
            raise PlaidEnvironmentError(
                f"{name} is a Sandbox-only call; refused in {self.plaid_env}."
            )

    def _base_link_fields(self) -> dict[str, Any]:
        return {
            "user": LinkTokenCreateRequestUser(client_user_id=CLIENT_USER_ID),
            "client_name": CLIENT_NAME,
            "language": "en",
            "country_codes": [CountryCode("US")],
        }

    # -- link ------------------------------------------------------------------------------

    def link_token_request(
        self, *, access_token: str | None = None, additional_consented_products: Sequence[str] = ()
    ) -> LinkTokenCreateRequest:
        """The ``/link/token/create`` request (exposed so tests can check its exact shape).

        New link: ``products=['transactions']`` only, ``additional_consented_products`` always
        includes ``liabilities``, ``transactions.days_requested=730``. Update mode
        (``access_token`` given): the basic fields only, plus ``additional_consented_products`` when
        asked (to add Liabilities consent). Never a ``redirect_uri`` or ``webhook``.
        """
        fields = self._base_link_fields()
        if access_token is None:
            extra = list(dict.fromkeys(["liabilities", *additional_consented_products]))
            return LinkTokenCreateRequest(
                **fields,
                products=[Products("transactions")],
                additional_consented_products=[Products(p) for p in extra],
                transactions=LinkTokenTransactions(days_requested=DAYS_REQUESTED),
            )
        fields["access_token"] = self._token(access_token)
        if additional_consented_products:
            fields["additional_consented_products"] = [
                Products(p) for p in dict.fromkeys(additional_consented_products)
            ]
        return LinkTokenCreateRequest(**fields)

    def create_link_token(
        self, *, access_token: str | None = None, additional_consented_products: Sequence[str] = ()
    ) -> str:
        """A Link token for a new Item (no ``access_token``) or update mode (with one)."""
        request = self.link_token_request(
            access_token=access_token, additional_consented_products=additional_consented_products
        )
        body = call_raw(self._client.link_token_create, request)
        token = body["link_token"]
        register_secret(token)
        return token

    def exchange_public_token(self, public_token: str) -> ExchangeResult:
        """``/item/public_token/exchange`` (timeout ``(10, 120)``)."""
        request = ItemPublicTokenExchangeRequest(public_token=self._token(public_token))
        body = call_raw(self._client.item_public_token_exchange, request, timeout=TIMEOUT_EXCHANGE)
        register_secret(body["access_token"])
        return ExchangeResult(access_token=body["access_token"], item_id=body["item_id"])

    # -- transactions ----------------------------------------------------------------------

    def sync_request(self, access_token: str, cursor: str | None, count: int = SYNC_PAGE_SIZE):
        """The ``/transactions/sync`` request: count 500, cursor omitted when none is stored."""
        kwargs: dict[str, Any] = {}
        if cursor:
            kwargs["cursor"] = cursor
        return TransactionsSyncRequest(
            access_token=self._token(access_token),
            count=count,
            options=TransactionsSyncRequestOptions(
                include_original_description=True,
                personal_finance_category_version=PersonalFinanceCategoryVersion("v2"),
            ),
            **kwargs,
        )

    def transactions_sync(
        self, access_token: str, cursor: str | None, *, count: int = SYNC_PAGE_SIZE
    ) -> dict[str, Any]:
        """One ``/transactions/sync`` page (timeout ``(10, 180)``); JSON with Decimal amounts."""
        request = self.sync_request(access_token, cursor, count)
        return call_raw(self._client.transactions_sync, request, timeout=TIMEOUT_SYNC)

    def transactions_refresh(self, access_token: str) -> None:
        """Ask Plaid to check the institution now (included in the Trial plan; 2/min per Item)."""
        request = TransactionsRefreshRequest(access_token=self._token(access_token))
        call_raw(self._client.transactions_refresh, request)

    # -- items, accounts, liabilities ------------------------------------------------------

    def get_item(self, access_token: str) -> dict[str, Any]:
        """``/item/get``: ``{item, status, request_id}`` (consent expiry, products, errors)."""
        return call_raw(
            self._client.item_get, ItemGetRequest(access_token=self._token(access_token))
        )

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        """``/accounts/get`` (free, cached balances)."""
        return call_raw(
            self._client.accounts_get, AccountsGetRequest(access_token=self._token(access_token))
        )

    def get_liabilities(self, access_token: str) -> dict[str, Any]:
        """``/liabilities/get`` (only when Liabilities was consented)."""
        return call_raw(
            self._client.liabilities_get,
            LiabilitiesGetRequest(access_token=self._token(access_token)),
        )

    def search_institutions(self, query: str, products: Sequence[str]) -> dict[str, Any]:
        """``/institutions/search`` in the US (pre-link check; creates no Item)."""
        request = InstitutionsSearchRequest(
            query=query,
            products=[Products(p) for p in products],
            country_codes=[CountryCode("US")],
        )
        return call_raw(self._client.institutions_search, request)

    # -- sandbox only ----------------------------------------------------------------------

    def sandbox_create_public_token(
        self,
        institution_id: str,
        *,
        override_username: str | None = None,
        override_password: str | None = None,
        initial_products: Sequence[str] = ("transactions",),
    ) -> str:
        """Create a Sandbox Item without Link (730 days of history).

        ``initial_products=("transactions", "liabilities")`` (RESEARCH T3.10) lets Sandbox Items
        exercise ``/liabilities/get`` and statement snapshots before the one real link.
        """
        self._require_sandbox("sandbox_create_public_token")
        products = list(dict.fromkeys(initial_products))
        if not products:
            raise ValueError("initial_products must name at least one product")
        options: dict[str, Any] = {
            "transactions": SandboxPublicTokenCreateRequestOptionsTransactions(
                days_requested=DAYS_REQUESTED
            )
        }
        if override_username is not None:
            options["override_username"] = override_username
        if override_password is not None:
            options["override_password"] = override_password
        request = SandboxPublicTokenCreateRequest(
            institution_id=institution_id,
            initial_products=[Products(p) for p in products],
            options=SandboxPublicTokenCreateRequestOptions(**options),
        )
        body = call_raw(self._client.sandbox_public_token_create, request)
        register_secret(body["public_token"])
        return body["public_token"]

    def sandbox_reset_login(self, access_token: str) -> None:
        """Force ``ITEM_LOGIN_REQUIRED`` on a Sandbox Item."""
        self._require_sandbox("sandbox_reset_login")
        call_raw(
            self._client.sandbox_item_reset_login,
            SandboxItemResetLoginRequest(access_token=self._token(access_token)),
        )

    def sandbox_create_transactions(
        self, access_token: str, account_id: str, transactions: Sequence[Mapping[str, Any]]
    ) -> None:
        """Add posted transactions to a Sandbox account (at most 10 per call).

        Each item: ``date_transacted`` and ``date_posted`` (``datetime.date``), ``amount``
        (``Decimal``), ``description`` and optional ``iso_currency_code``. Amounts are converted to
        ``float`` only here, at the SDK boundary (plaid-python rejects ``Decimal``).
        """
        self._require_sandbox("sandbox_create_transactions")
        if not 1 <= len(transactions) <= 10:
            raise ValueError("send between 1 and 10 Sandbox transactions per call")
        custom = [
            CustomSandboxTransaction(
                date_transacted=t["date_transacted"],
                date_posted=t["date_posted"],
                amount=float(to_money(t["amount"])),
                description=t["description"],
                iso_currency_code=t.get("iso_currency_code", "USD"),
                account_id=account_id,
            )
            for t in transactions
        ]
        request = SandboxTransactionsCreateRequest(
            access_token=self._token(access_token), transactions=custom
        )
        call_raw(self._client.sandbox_transactions_create, request)


def gateway_methods(obj: object) -> set[str]:
    """Public callable attributes of a gateway (used by the allowlist test)."""
    return {
        name for name in dir(obj) if not name.startswith("_") and callable(getattr(obj, name, None))
    }
