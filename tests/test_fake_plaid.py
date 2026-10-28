import datetime as dt
from decimal import Decimal

import pytest

from budgetapp.plaid_gateway import MUTATION_CODE, PlaidError, PlaidTransportError
from tests.factories import plaid_credit_liability, plaid_txn
from tests.fake_plaid import Event, FakePlaidGateway, plaid_number


@pytest.fixture
def fake():
    return FakePlaidGateway()


@pytest.fixture
def item(fake):
    return fake.add_item()


def sync_all(fake, item, cursor=None):
    """Fetch every page from ``cursor``; returns (pages, final cursor)."""
    pages = []
    while True:
        page = fake.transactions_sync(item.access_token, cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if not page["has_more"]:
            return pages, cursor


def ids(entries):
    return [e["transaction_id"] for e in entries]


class TestEventLog:
    def test_same_id_in_added_modified_and_removed_on_different_pages(self, fake, item):
        fake.page_size = 1
        fake.add_pending(item, transaction_id="p1", amount="50.00")
        fake.modify_transaction(item, "p1", amount="62.40")
        fake.remove_transaction(item, "p1")
        pages, cursor = sync_all(fake, item)
        assert [(ids(p["added"]), ids(p["modified"]), ids(p["removed"])) for p in pages] == [
            (["p1"], [], []),
            ([], ["p1"], []),
            ([], [], ["p1"]),
        ]
        assert [p["has_more"] for p in pages] == [True, True, False]
        assert pages[1]["modified"][0]["amount"] == Decimal("62.4")
        # Caught up: a new sync from the final cursor brings nothing.
        again = fake.transactions_sync(item.access_token, cursor)
        assert again["added"] == again["modified"] == again["removed"] == []
        assert again["has_more"] is False and again["next_cursor"] == cursor

    def test_one_page_carries_the_last_occurrence_per_list(self, fake, item):
        fake.add_transaction(item, transaction_id="a", amount="1.00")
        fake.modify_transaction(item, "a", amount="2.00")
        fake.modify_transaction(item, "a", amount="3.00")
        fake.remove_transaction(item, "a")
        page = fake.transactions_sync(item.access_token, None)
        assert ids(page["added"]) == ["a"] and ids(page["removed"]) == ["a"]
        assert [m["amount"] for m in page["modified"]] == [3]
        assert fake.current_transactions(item) == {}

    def test_restarting_from_an_old_cursor_replays_the_same_events(self, fake, item):
        fake.page_size = 2
        for n in range(5):
            fake.add_transaction(item, transaction_id=f"t{n}")
        first_page = fake.transactions_sync(item.access_token, None)
        replay = fake.transactions_sync(item.access_token, None)
        assert ids(first_page["added"]) == ids(replay["added"]) == ["t0", "t1"]

    def test_count_limits_the_page(self, fake, item):
        for n in range(3):
            fake.add_transaction(item, transaction_id=f"t{n}")
        page = fake.transactions_sync(item.access_token, None, count=2)
        assert len(page["added"]) == 2 and page["has_more"]

    def test_invalid_cursor(self, fake, item):
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, "garbage!")
        assert info.value.error_code == "INVALID_FIELD"


class TestPlaidBehaviours:
    def test_not_ready(self, fake, item):
        fake.set_update_status(item, "NOT_READY")
        fake.add_transaction(item)
        page = fake.transactions_sync(item.access_token, None)
        assert page["transactions_update_status"] == "NOT_READY"
        assert page["next_cursor"] == "" and page["added"] == [] and not page["has_more"]

    def test_update_status_is_reported(self, fake, item):
        fake.set_update_status(item, "INITIAL_UPDATE_COMPLETE")
        assert fake.transactions_sync(item.access_token, None)["transactions_update_status"] == (
            "INITIAL_UPDATE_COMPLETE"
        )

    def test_mutation_during_pagination(self, fake, item):
        fake.page_size = 1
        fake.add_transaction(item, transaction_id="a")
        fake.add_transaction(item, transaction_id="b")
        late = Event("added", plaid_txn(transaction_id="late", account_id=item.default_account_id))
        fake.mutate_during_pagination(item, on_call=2, events=[late])
        first = fake.transactions_sync(item.access_token, None)
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, first["next_cursor"])
        assert info.value.error_code == MUTATION_CODE
        pages, _ = sync_all(fake, item)  # restart from the original (null) cursor
        assert [i for p in pages for i in ids(p["added"])] == ["a", "b", "late"]

    def test_pending_to_posted_with_a_changed_amount(self, fake, item):
        fake.add_pending(item, transaction_id="p", amount="50.00", date="2026-10-10")
        posted = fake.post_pending(item, "p", amount="62.40", date="2026-10-12")
        assert posted["pending_transaction_id"] == "p" and posted["pending"] is False
        assert posted["authorized_date"] == "2026-10-10" and posted["date"] == "2026-10-12"
        page = fake.transactions_sync(item.access_token, None)
        assert ids(page["removed"]) == ["p"] and set(ids(page["added"])) == {"p", "p-posted"}
        assert fake.current_transactions(item)["p-posted"]["amount"] == Decimal("62.40")

    def test_unlinked_posting_and_churn(self, fake, item):
        fake.add_pending(item, transaction_id="p")
        unlinked = fake.post_pending(item, "p", link=False, added_first=True)
        assert unlinked["pending_transaction_id"] is None
        assert item.events[-2].kind == "added" and item.events[-1].kind == "removed"
        churned = fake.churn(item, unlinked["transaction_id"], name="NEW NAME", amount="9.99")
        assert churned["pending_transaction_id"] is None and churned["name"] == "NEW NAME"
        assert list(fake.current_transactions(item)) == [churned["transaction_id"]]

    def test_amounts_look_like_parsed_plaid_json(self, fake, item):
        fake.add_transaction(item, amount="12.50")
        fake.add_transaction(item, amount="5000.00")
        page = fake.transactions_sync(item.access_token, None)
        amounts = [t["amount"] for t in page["added"]]
        assert amounts == [Decimal("12.5"), 5000]
        assert isinstance(amounts[0], Decimal) and isinstance(amounts[1], int)
        assert plaid_number(Decimal("-0.10")) == Decimal("-0.1")
        assert page["accounts"][0]["account_id"] == item.default_account_id
        assert "balances" in page["accounts"][0]

    def test_transaction_shape(self, fake, item):
        txn = fake.add_transaction(item, authorized_date=dt.date(2026, 10, 1))
        for key in (
            "transaction_id",
            "account_id",
            "amount",
            "iso_currency_code",
            "date",
            "pending",
            "pending_transaction_id",
            "name",
            "merchant_name",
            "original_description",
            "payment_channel",
            "location",
            "authorized_date",
            "account_owner",
            "transaction_code",
            "personal_finance_category",
        ):
            assert key in txn
        assert txn["authorized_date"] == "2026-10-01"
        assert txn["personal_finance_category"]["version"] == "v2"


class TestOtherEndpoints:
    def test_errors_are_queued_per_method(self, fake, item):
        fake.queue_error("transactions_sync", fake.plaid_error("INTERNAL_SERVER_ERROR"))
        fake.queue_error("transactions_sync", fake.transport_error())
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, None)
        assert info.value.error_type == "API_ERROR" and info.value.retryable
        with pytest.raises(PlaidTransportError):
            fake.transactions_sync(item.access_token, None)
        fake.transactions_sync(item.access_token, None)  # queue drained
        assert fake.plaid_error("TRIAL_CONNECTION_LIMIT").status == 429

    def test_item_error_breaks_data_calls_but_not_item_get(self, fake, item):
        fake.sandbox_reset_login(item.access_token)
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, None)
        assert info.value.error_code == "ITEM_LOGIN_REQUIRED" and info.value.needs_reconnect
        for call in (fake.get_accounts, fake.get_liabilities, fake.transactions_refresh):
            with pytest.raises(PlaidError):
                call(item.access_token)
        body = fake.get_item(item.access_token)
        assert body["item"]["error"]["error_code"] == "ITEM_LOGIN_REQUIRED"
        fake.set_item_error(item, None)
        fake.transactions_sync(item.access_token, None)

    def test_item_get_fields(self, fake):
        item = fake.add_item(
            consent_expiration_time="2027-10-01T00:00:00Z", consented_products=["transactions"]
        )
        body = fake.get_item(item.access_token)
        assert body["item"]["consent_expiration_time"] == "2027-10-01T00:00:00Z"
        assert body["item"]["consented_products"] == ["transactions"]
        assert body["item"]["institution_id"] == "ins_10"
        assert "last_successful_update" in body["status"]["transactions"]

    def test_accounts_with_balances(self, fake, item):
        fake.set_balances(item, item.default_account_id, current="1500.25", limit=None)
        account = fake.get_accounts(item.access_token)["accounts"][0]
        assert account["balances"]["current"] == Decimal("1500.25")
        assert account["balances"]["limit"] is None
        assert account["type"] == "credit" and account["subtype"] == "credit card"

    def test_liabilities(self, fake, item):
        body = fake.get_liabilities(item.access_token)
        credit = body["liabilities"]["credit"][0]
        assert credit["account_id"] == item.default_account_id
        assert credit["last_statement_balance"] == 1500
        fake.set_liabilities(
            item,
            [
                plaid_credit_liability(
                    account_id=item.default_account_id, last_statement_balance=None
                )
            ],
        )
        assert (
            fake.get_liabilities(item.access_token)["liabilities"]["credit"][0][
                "last_statement_balance"
            ]
            is None
        )
        fake.set_liabilities(item, [])
        with pytest.raises(PlaidError, match="NO_LIABILITY_ACCOUNTS"):
            fake.get_liabilities(item.access_token)
        no_consent = fake.add_item(consented_products=["transactions"])
        with pytest.raises(PlaidError) as info:
            fake.get_liabilities(no_consent.access_token)
        assert info.value.error_code == "ADDITIONAL_CONSENT_REQUIRED"

    def test_refresh_callback(self, fake, item):
        fake.add_pending(item, transaction_id="p")
        item.on_refresh = lambda it: fake.post_pending(it, "p")
        fake.transactions_refresh(item.access_token)
        assert item.refresh_count == 1
        assert "p-posted" in fake.current_transactions(item)

    def test_link_and_exchange(self, fake):
        public = fake.sandbox_create_public_token("ins_109508", override_username="user_good")
        result = fake.exchange_public_token(public)
        assert result.access_token.startswith("access-sandbox-")
        assert fake.items[result.item_id].institution_name == "First Platypus Bank"
        with pytest.raises(PlaidError, match="INVALID_PUBLIC_TOKEN"):
            fake.exchange_public_token(public)  # single use
        token = fake.create_link_token(
            access_token=result.access_token, additional_consented_products=["liabilities"]
        )
        assert token.startswith("link-sandbox-")
        assert fake.link_tokens[-1]["additional_consented_products"] == ("liabilities",)
        assert [name for name, _ in fake.calls][:2] == [
            "sandbox_create_public_token",
            "exchange_public_token",
        ]

    def test_search_institutions(self, fake):
        found = fake.search_institutions("american express", ["liabilities"])["institutions"]
        assert [i["institution_id"] for i in found] == ["ins_10"]
        assert found[0]["oauth"] is True
        assert fake.search_institutions("american express", ["auth"])["institutions"] == []

    def test_sandbox_create_transactions_and_production_guard(self, fake, item):
        fake.sandbox_create_transactions(
            item.access_token,
            item.default_account_id,
            [
                {
                    "date_transacted": dt.date(2026, 10, 1),
                    "date_posted": dt.date(2026, 10, 2),
                    "amount": Decimal("7.25"),
                    "description": "TEST",
                },
            ],
        )
        posted = list(fake.current_transactions(item).values())[-1]
        assert posted["date"] == "2026-10-02" and posted["authorized_date"] == "2026-10-01"
        production = FakePlaidGateway(plaid_env="production")
        with pytest.raises(Exception, match="Sandbox-only"):
            production.sandbox_create_public_token("ins_109508")


class TestScriptedScenarios:
    """What the SYNC package needs without patching the shared fake."""

    def test_a_transport_error_on_page_two(self, fake, item):
        fake.page_size = 1
        fake.add_transaction(item, transaction_id="a")
        fake.add_transaction(item, transaction_id="b")
        fake.script_sync(item, 2, error=fake.transport_error())
        first = fake.transactions_sync(item.access_token, None)
        assert ids(first["added"]) == ["a"] and first["has_more"]
        with pytest.raises(PlaidTransportError) as info:
            fake.transactions_sync(item.access_token, first["next_cursor"])
        assert info.value.restart_pagination
        pages, _ = sync_all(fake, item)  # the restart from the original cursor sees everything
        assert [i for p in pages for i in ids(p["added"])] == ["a", "b"]

    def test_an_item_error_on_page_two(self, fake, item):
        fake.page_size = 1
        fake.add_transaction(item, transaction_id="a")
        fake.add_transaction(item, transaction_id="b")
        fake.script_sync(item, 2, error=fake.plaid_error("ITEM_LOGIN_REQUIRED"))
        first = fake.transactions_sync(item.access_token, None)
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, first["next_cursor"])
        assert info.value.needs_reconnect

    def test_history_becomes_ready_after_some_calls(self, fake):
        item = fake.add_item(update_status="NOT_READY")
        fake.add_transaction(item, transaction_id="old")
        fake.script_sync(item, 2, update_status="INITIAL_UPDATE_COMPLETE")
        fake.script_sync(item, 3, update_status="HISTORICAL_UPDATE_COMPLETE")
        statuses = [
            fake.transactions_sync(item.access_token, None)["transactions_update_status"]
            for _ in range(4)
        ]
        assert statuses == [
            "NOT_READY",
            "INITIAL_UPDATE_COMPLETE",
            "HISTORICAL_UPDATE_COMPLETE",
            "HISTORICAL_UPDATE_COMPLETE",
        ]

    def test_events_without_an_error_mean_a_mutation(self, fake, item):
        late = Event("added", plaid_txn(transaction_id="late", account_id=item.default_account_id))
        fake.script_sync(item, 1, events=[late])
        with pytest.raises(PlaidError) as info:
            fake.transactions_sync(item.access_token, None)
        assert info.value.is_mutation_during_pagination and info.value.restart_pagination

    def test_account_churn_redelivers_history_under_new_ids(self, fake, item):
        old_account = item.default_account_id
        fake.add_transaction(item, transaction_id="t1", amount="10.00")
        _, cursor = sync_all(fake, item)
        new_account = fake.churn_account(item, old_account)
        assert item.default_account_id == new_account != old_account
        churned = fake.get_accounts(item.access_token)["accounts"][0]
        assert churned["account_id"] == new_account and churned["mask"] is not None
        page = fake.transactions_sync(item.access_token, cursor)
        assert page["removed"] == [{"transaction_id": "t1", "account_id": old_account}]
        [again] = page["added"]
        assert again["account_id"] == new_account and again["transaction_id"] != "t1"
        assert again["amount"] == Decimal("10")


class TestPlaidErrorLabels:
    @pytest.mark.parametrize(
        "code",
        [
            "TRANSACTIONS_SYNC_LIMIT",
            "TRANSACTIONS_REFRESH_LIMIT",
            "TRANSACTIONS_LIMIT",
            "ITEM_GET_LIMIT",
            "ACCOUNTS_LIMIT",
            "INSTITUTION_RATE_LIMIT",
            "TRIAL_CONNECTION_LIMIT",
        ],
    )
    def test_rate_limits_are_429(self, code):
        error = FakePlaidGateway.plaid_error(code)
        assert (error.error_type, error.status) == ("RATE_LIMIT_EXCEEDED", 429)
        assert error.retryable is (code != "TRIAL_CONNECTION_LIMIT")

    @pytest.mark.parametrize(
        "code", ["INVALID_ACCESS_TOKEN", "ADDITIONAL_CONSENT_REQUIRED", "INVALID_PUBLIC_TOKEN"]
    )
    def test_invalid_input(self, code):
        error = FakePlaidGateway.plaid_error(code)
        assert error.error_type == "INVALID_INPUT" and error.status == 400 and not error.retryable


def test_sandbox_items_can_include_liabilities(fake):
    token = fake.sandbox_create_public_token(
        "ins_109508", initial_products=("transactions", "liabilities")
    )
    item = fake.items[fake.exchange_public_token(token).item_id]
    assert item.products == ["transactions", "liabilities"]
    assert fake.get_liabilities(item.access_token)["liabilities"]["credit"]


def test_default_locations_carry_decimal_coordinates(fake, item):
    fake.add_transaction(item)
    location = fake.transactions_sync(item.access_token, None)["added"][0]["location"]
    assert isinstance(location["lat"], Decimal) and isinstance(location["lon"], Decimal)
