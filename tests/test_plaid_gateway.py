import ast
import datetime as dt
import json
import typing
from decimal import Decimal
from pathlib import Path

import plaid
import pytest
import urllib3

from budgetapp import plaid_gateway as pg
from budgetapp.config import load_settings
from budgetapp.plaid_gateway import (
    ALLOWED_METHODS,
    MUTATION_CODE,
    PlaidEnvironmentError,
    PlaidError,
    PlaidGateway,
    PlaidGatewayError,
    PlaidTransportError,
    RealPlaidGateway,
)
from tests.fake_plaid import FakePlaidGateway

SANDBOX_TOKEN = "access-sandbox-11111111-2222-3333-4444-555555555555"
PROD_TOKEN = "access-production-11111111-2222-3333-4444-555555555555"
EXPECTED_ENDPOINTS = {
    "link_token_create",
    "item_public_token_exchange",
    "transactions_sync",
    "transactions_refresh",
    "item_get",
    "accounts_get",
    "liabilities_get",
    "institutions_search",
    "sandbox_public_token_create",
    "sandbox_item_reset_login",
    "sandbox_transactions_create",
}


class FakeResponse:
    def __init__(self, body: bytes | Exception):
        self._body = body
        self.released = False

    @property
    def data(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def release_conn(self):
        self.released = True


class BodyError:
    """The response headers arrive, then reading the body fails (a stall or reset mid-body)."""

    def __init__(self, exc: Exception):
        self.exc = exc


class FakeClient:
    """Stands in for plaid_api.PlaidApi: every endpoint returns or raises what the test set."""

    def __init__(self):
        self.calls: list[tuple[str, object, dict]] = []
        self.responses: dict[str, object] = {}
        self.last_response: FakeResponse | None = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def endpoint(request, **kwargs):
            self.calls.append((name, request, kwargs))
            outcome = self.responses.get(name, {"request_id": "r"})
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, BodyError):
                body = outcome.exc
            elif isinstance(outcome, bytes):
                body = outcome
            else:
                body = json.dumps(outcome).encode()
            self.last_response = FakeResponse(body)
            return self.last_response

        return endpoint


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def gateway(client):
    return RealPlaidGateway("sandbox", client=client)


def api_exception(status, body):
    exc = plaid.ApiException(status=status, reason="Bad Request")
    exc.body = body if isinstance(body, str) else json.dumps(body)
    return exc


class TestAllowlist:
    def test_public_methods_are_exactly_the_allowlist(self):
        builders = {"from_settings", "link_token_request", "sync_request"}
        assert pg.gateway_methods(RealPlaidGateway) - builders == ALLOWED_METHODS

    def test_protocol_declares_exactly_the_allowlist(self):
        members = {
            name
            for name in vars(PlaidGateway)
            if not name.startswith("_") and callable(getattr(PlaidGateway, name))
        }
        assert members == ALLOWED_METHODS
        assert "plaid_env" in typing.get_type_hints(PlaidGateway)

    def test_only_allowed_plaid_endpoints_are_called(self):
        source = Path(pg.__file__).read_text()
        used = {
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_client"
        }
        assert used == EXPECTED_ENDPOINTS
        for forbidden in ("item_remove", "transfer", "payment", "processor", "bank_transfer"):
            assert not any(forbidden in name for name in used)

    def test_the_fake_satisfies_the_protocol(self):
        assert isinstance(FakePlaidGateway(), PlaidGateway)
        assert pg.gateway_methods(FakePlaidGateway) >= ALLOWED_METHODS

    def test_no_other_module_imports_plaid_or_reaches_the_client(self):
        """SEC-3: plaid_gateway is the only module that may talk to Plaid."""
        package = Path(pg.__file__).parent
        offenders = []
        for path in sorted(package.rglob("*.py")):
            if path == Path(pg.__file__):
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                if any(n == "plaid" or n.startswith("plaid.") for n in names):
                    offenders.append(f"{path.relative_to(package)} imports {names}")
                if isinstance(node, ast.Attribute) and node.attr == "_client":
                    offenders.append(f"{path.relative_to(package)} uses ._client")
        assert offenders == []


class TestRequests:
    def test_new_link_token_request(self, gateway):
        body = gateway.link_token_request().to_dict()
        assert body["products"] == ["transactions"]
        assert body["additional_consented_products"] == ["liabilities"]
        assert body["transactions"] == {"days_requested": 730}
        assert body["client_name"] == "Amex Budget Tracker"
        assert body["user"] == {"client_user_id": "owner-1"}
        assert body["country_codes"] == ["US"] and body["language"] == "en"
        for absent in ("redirect_uri", "webhook", "access_token", "optional_products", "update"):
            assert absent not in body

    def test_update_mode_link_token_request(self, gateway):
        body = gateway.link_token_request(access_token=SANDBOX_TOKEN).to_dict()
        assert body["access_token"] == SANDBOX_TOKEN
        for absent in ("products", "transactions", "additional_consented_products", "update"):
            assert absent not in body
        add = gateway.link_token_request(
            access_token=SANDBOX_TOKEN, additional_consented_products=["liabilities"]
        ).to_dict()
        assert add["additional_consented_products"] == ["liabilities"]
        assert "products" not in add

    def test_sync_request(self, gateway):
        first = gateway.sync_request(SANDBOX_TOKEN, None).to_dict()
        assert first["count"] == 500 and "cursor" not in first
        assert first["options"] == {
            "include_original_description": True,
            "personal_finance_category_version": "v2",
        }
        assert "cursor" not in gateway.sync_request(SANDBOX_TOKEN, "").to_dict()
        assert gateway.sync_request(SANDBOX_TOKEN, "abc").to_dict()["cursor"] == "abc"

    def test_calls_use_raw_content_and_timeouts(self, gateway, client):
        client.responses["transactions_sync"] = {"added": [], "has_more": False}
        gateway.transactions_sync(SANDBOX_TOKEN, None)
        name, _, kwargs = client.calls[-1]
        assert name == "transactions_sync"
        assert kwargs == {"_preload_content": False, "_request_timeout": (10, 180)}
        assert client.last_response.released
        client.responses["item_public_token_exchange"] = {
            "access_token": SANDBOX_TOKEN,
            "item_id": "item-1",
        }
        result = gateway.exchange_public_token("public-sandbox-abc")
        assert client.calls[-1][2]["_request_timeout"] == (10, 120)
        assert result.item_id == "item-1" and SANDBOX_TOKEN not in repr(result)
        gateway.get_item(SANDBOX_TOKEN)
        assert client.calls[-1][2]["_request_timeout"] == (10, 60)

    def test_search_institutions_request(self, gateway, client):
        gateway.search_institutions("American Express", ["liabilities"])
        request = client.calls[-1][1].to_dict()
        assert request == {
            "query": "American Express",
            "products": ["liabilities"],
            "country_codes": ["US"],
        }


class TestDecimalParsing:
    def test_amounts_are_decimal_and_ints_stay_int(self, gateway, client):
        client.responses["transactions_sync"] = (
            b'{"added": [{"amount": 12.34}, {"amount": -0.1}], '
            b'"accounts": [{"balances": {"current": 410.5, "limit": 5000}}], "has_more": false}'
        )
        body = gateway.transactions_sync(SANDBOX_TOKEN, None)
        assert body["added"][0]["amount"] == Decimal("12.34")
        assert isinstance(body["added"][1]["amount"], Decimal)
        assert body["accounts"][0]["balances"]["limit"] == 5000
        assert isinstance(body["accounts"][0]["balances"]["limit"], int)


class TestErrors:
    def test_plaid_error_fields_and_classification(self, gateway, client):
        client.responses["transactions_sync"] = api_exception(
            400,
            {
                "error_type": "ITEM_ERROR",
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_code_reason": "OAUTH_CONSENT_EXPIRED",
                "error_message": "the login details of this item have changed",
                "display_message": "Please reconnect.",
                "request_id": "req-1",
            },
        )
        with pytest.raises(PlaidError) as info:
            gateway.transactions_sync(SANDBOX_TOKEN, "cur")
        err = info.value
        assert (err.status, err.error_type, err.error_code) == (
            400,
            "ITEM_ERROR",
            "ITEM_LOGIN_REQUIRED",
        )
        assert err.display_message == "Please reconnect." and err.request_id == "req-1"
        assert err.error_code_reason == "OAUTH_CONSENT_EXPIRED"
        assert err.classification == "needs_reconnect" and err.needs_reconnect
        assert not err.retryable
        assert pg.item_status_for(err) == "needs_reconnect"

    @pytest.mark.parametrize(
        ("error_type", "code", "classification", "retryable"),
        [
            ("RATE_LIMIT_EXCEEDED", "TRANSACTIONS_SYNC_LIMIT", "error", True),
            ("RATE_LIMIT_EXCEEDED", "TRIAL_CONNECTION_LIMIT", "trial_limit", False),
            ("INSTITUTION_ERROR", "INSTITUTION_DOWN", "error", True),
            ("API_ERROR", "INTERNAL_SERVER_ERROR", "error", True),
            ("ITEM_ERROR", "PENDING_EXPIRATION", "needs_reconnect", False),
            ("ITEM_ERROR", "USER_PERMISSION_REVOKED", "needs_reconnect", False),
            ("INVALID_INPUT", "INVALID_ACCESS_TOKEN", "error", False),
            ("TRANSACTIONS_ERROR", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "error", False),
        ],
    )
    def test_classification(self, error_type, code, classification, retryable):
        err = PlaidError(400, error_type, code, "message")
        assert err.classification == classification
        assert err.retryable is retryable
        assert pg.item_status_for(err) == (
            "needs_reconnect" if classification == "needs_reconnect" else "error"
        )
        assert err.is_trial_limit is (code == "TRIAL_CONNECTION_LIMIT")
        assert err.is_mutation_during_pagination is (code.endswith("MUTATION_DURING_PAGINATION"))

    def test_html_error_body_is_a_transport_error(self, gateway, client):
        client.responses["item_get"] = api_exception(502, "<html>Bad gateway</html>")
        with pytest.raises(PlaidTransportError):
            gateway.get_item(SANDBOX_TOKEN)

    def test_timeout_before_headers(self, gateway, client):
        client.responses["accounts_get"] = urllib3.exceptions.ReadTimeoutError(
            None, "/", "timed out"
        )
        with pytest.raises(PlaidTransportError):
            gateway.get_accounts(SANDBOX_TOKEN)

    def test_connection_refused(self, gateway, client):
        client.responses["accounts_get"] = urllib3.exceptions.MaxRetryError(None, "/", "refused")
        with pytest.raises(PlaidTransportError) as info:
            gateway.get_accounts(SANDBOX_TOKEN)
        assert info.value.classification == "error" and info.value.retryable
        assert pg.item_status_for(info.value) == "error"

    def test_body_stall_after_headers_is_translated(self, gateway, client):
        client.responses["transactions_sync"] = BodyError(
            urllib3.exceptions.ProtocolError("connection reset")
        )
        with pytest.raises(PlaidTransportError):
            gateway.transactions_sync(SANDBOX_TOKEN, None)
        assert client.last_response.released  # the body read failed inside the handler

    def test_non_json_success_body(self, gateway, client):
        client.responses["liabilities_get"] = b"<html>captive portal</html>"
        with pytest.raises(PlaidTransportError, match="non-JSON"):
            gateway.get_liabilities(SANDBOX_TOKEN)

    def test_plaid_tls_bug_is_a_transport_error(self, gateway, client):
        client.responses["item_get"] = AttributeError("'NoneType' object has no attribute 'decode'")
        with pytest.raises(PlaidTransportError):
            gateway.get_item(SANDBOX_TOKEN)

    def test_other_attribute_errors_are_real_bugs(self, gateway, client):
        client.responses["item_get"] = AttributeError("'Foo' object has no attribute 'bar'")
        with pytest.raises(AttributeError):
            gateway.get_item(SANDBOX_TOKEN)

    def test_an_error_page_that_is_not_utf8_is_a_transport_error(self, gateway, client):
        """plaid-python decodes error bodies as UTF-8 before raising ApiException."""
        client.responses["transactions_sync"] = UnicodeDecodeError(
            "utf-8", b"\xe9", 0, 1, "invalid continuation byte"
        )
        with pytest.raises(PlaidTransportError, match="not UTF-8"):
            gateway.transactions_sync(SANDBOX_TOKEN, None)


class TestErrorInterface:
    """SYNC handles both errors with one ``except PlaidGatewayError`` and one set of fields."""

    FIELDS = (
        "status",
        "error_type",
        "error_code",
        "error_message",
        "display_message",
        "request_id",
        "error_code_reason",
        "classification",
        "needs_reconnect",
        "is_trial_limit",
        "is_mutation_during_pagination",
        "retryable",
        "restart_pagination",
    )

    def test_both_errors_share_the_interface(self):
        transport = PlaidTransportError("ReadTimeoutError: read timed out")
        plaid_error = PlaidError(400, "ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "login", "Log in again")
        for error in (transport, plaid_error):
            assert isinstance(error, PlaidGatewayError)
            for field in self.FIELDS:
                getattr(error, field)
        assert (transport.error_code, transport.error_message) == (
            "TRANSPORT_ERROR",
            "ReadTimeoutError: read timed out",
        )
        assert transport.status is None and transport.display_message is None
        assert transport.classification == "error" and not transport.needs_reconnect
        assert pg.item_status_for(transport) == "error"
        assert pg.item_status_for(plaid_error) == "needs_reconnect"

    def test_restart_pagination_for_transport_errors_and_mutations_only(self):
        mutation = PlaidError(400, "TRANSACTIONS_ERROR", MUTATION_CODE, "changed")
        assert mutation.restart_pagination and not mutation.retryable
        assert PlaidTransportError("reset").restart_pagination
        limit = PlaidError(429, "RATE_LIMIT_EXCEEDED", "TRANSACTIONS_SYNC_LIMIT", "slow down")
        assert limit.retryable and not limit.restart_pagination


class TestEnvironmentGuards:
    def test_sandbox_only_methods_refused_in_production(self, client):
        gateway = RealPlaidGateway("production", client=client)
        with pytest.raises(PlaidEnvironmentError):
            gateway.sandbox_create_public_token("ins_109508")
        with pytest.raises(PlaidEnvironmentError):
            gateway.sandbox_reset_login(PROD_TOKEN)
        with pytest.raises(PlaidEnvironmentError):
            gateway.sandbox_create_transactions(PROD_TOKEN, "acc", [{}])
        assert client.calls == []

    def test_token_from_the_other_environment_is_refused(self, gateway, client):
        with pytest.raises(PlaidEnvironmentError):
            gateway.get_item(PROD_TOKEN)
        production = RealPlaidGateway("production", client=client)
        with pytest.raises(PlaidEnvironmentError):
            production.transactions_sync(SANDBOX_TOKEN, None)
        assert client.calls == []

    def test_from_settings_refuses_the_test_environment(self, settings):
        with pytest.raises(PlaidEnvironmentError):
            RealPlaidGateway.from_settings(settings)

    def test_from_settings_reads_keychain(self, secrets, monkeypatch):
        secrets.values["plaid.client_id"] = "0123456789abcdef01234567"
        secrets.values["plaid.secret.sandbox"] = "abcdef0123456789abcdef01234567"
        monkeypatch.setenv("BUDGETAPP_LIVE_SANDBOX", "1")  # as ./run test --sandbox does
        gateway = RealPlaidGateway.from_settings(load_settings("sandbox"))
        assert gateway.plaid_env == "sandbox"

    def test_tests_never_build_a_real_client(self, secrets, monkeypatch):
        secrets.values["plaid.client_id"] = "0123456789abcdef01234567"
        secrets.values["plaid.secret.sandbox"] = "abcdef0123456789abcdef01234567"
        secrets.values["plaid.secret.production"] = "fedcba9876543210fedcba98765432"
        for env in ("sandbox", "production"):
            with pytest.raises(PlaidEnvironmentError, match="FakePlaidGateway"):
                RealPlaidGateway.from_settings(load_settings(env))
        monkeypatch.setenv("BUDGETAPP_LIVE_SANDBOX", "1")
        with pytest.raises(PlaidEnvironmentError):
            RealPlaidGateway.from_settings(load_settings("production"))
        assert RealPlaidGateway("production", client=FakeClient()).plaid_env == "production"

    def test_unknown_environment(self):
        with pytest.raises(ValueError):
            RealPlaidGateway("development", "id", "secret")


class TestSandboxHelpers:
    def test_public_token_request(self, gateway, client):
        client.responses["sandbox_public_token_create"] = {"public_token": "public-sandbox-x"}
        token = gateway.sandbox_create_public_token(
            "ins_109508", override_username="user_transactions_dynamic", override_password="pw"
        )
        assert token == "public-sandbox-x"
        body = client.calls[-1][1].to_dict()
        assert body["initial_products"] == ["transactions"]
        assert body["options"]["transactions"] == {"days_requested": 730}
        assert body["options"]["override_username"] == "user_transactions_dynamic"

    def test_public_token_with_liabilities(self, gateway, client):
        """RESEARCH T3.10: Sandbox Items with Liabilities exercise statement snapshots."""
        client.responses["sandbox_public_token_create"] = {"public_token": "public-sandbox-y"}
        gateway.sandbox_create_public_token(
            "ins_109508", initial_products=("transactions", "liabilities")
        )
        assert client.calls[-1][1].to_dict()["initial_products"] == ["transactions", "liabilities"]
        with pytest.raises(ValueError):
            gateway.sandbox_create_public_token("ins_109508", initial_products=())

    def test_create_transactions_converts_decimal_only_at_the_boundary(self, gateway, client):
        gateway.sandbox_create_transactions(
            SANDBOX_TOKEN,
            "acc-card",
            [
                {
                    "date_transacted": dt.date(2026, 10, 1),
                    "date_posted": dt.date(2026, 10, 2),
                    "amount": Decimal("12.34"),
                    "description": "TEST COFFEE",
                }
            ],
        )
        custom = client.calls[-1][1].transactions[0]
        assert custom.amount == 12.34 and isinstance(custom.amount, float)
        assert custom.account_id == "acc-card"
        with pytest.raises(ValueError):
            gateway.sandbox_create_transactions(SANDBOX_TOKEN, "acc", [{}] * 11)
