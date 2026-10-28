"""FakePlaidGateway: an in-memory Plaid that replays an EVENT LOG (ARCHITECTURE §12).

Each Item keeps an ordered log of ``added`` / ``modified`` / ``removed`` events.
``transactions_sync`` pages through that log (not through a net state), exactly as Plaid may: one
sync can carry the same transaction id in ``added`` on page 1, ``modified`` on page 2 and
``removed`` on page 3. The cursor is a position in the log, so a sync restarted from an older
cursor replays the same events.

Helpers model what Plaid really does: pending → posted (removed + added with
``pending_transaction_id``, optional amount change), churn (removed + added, unlinked),
``NOT_READY`` (empty arrays and ``next_cursor == ''``),
``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION``
(new events appear and the call fails), queued ``PlaidError``/``PlaidTransportError`` per method,
Item errors (``ITEM_LOGIN_REQUIRED`` on data calls while ``/item/get`` still answers), balances,
liabilities, ``/transactions/refresh`` callbacks, institution search and a call log.

Amounts are served the way ``json.loads(parse_float=Decimal)`` would see Plaid's JSON: ``Decimal``
for fractional numbers (``12.5``) and ``int`` for whole ones (``5000``).
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import itertools
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from budgetapp.money import to_money
from budgetapp.plaid_gateway import (
    MUTATION_CODE,
    ExchangeResult,
    PlaidEnvironmentError,
    PlaidError,
    PlaidTransportError,
)
from tests.factories import plaid_account, plaid_credit_liability, plaid_txn

ADDED, MODIFIED, REMOVED = "added", "modified", "removed"

DEFAULT_INSTITUTIONS: list[dict[str, Any]] = [
    {
        "institution_id": "ins_10",
        "name": "American Express",
        "products": ["assets", "balance", "transactions", "liabilities"],
        "country_codes": ["US"],
        "oauth": True,
        "routing_numbers": [],
        "dtc_numbers": [],
    },
    {
        "institution_id": "ins_109508",
        "name": "First Platypus Bank",
        "products": ["assets", "auth", "balance", "transactions", "liabilities"],
        "country_codes": ["US"],
        "oauth": False,
        "routing_numbers": [],
        "dtc_numbers": [],
    },
]


def plaid_number(value: Any) -> Any:
    """Plaid JSON as ``parse_float=Decimal`` parses it: ``int`` if whole, else ``Decimal``."""
    if value is None or isinstance(value, bool):
        return value
    number = to_money(value)
    if number == number.to_integral_value():
        return int(number)
    return Decimal(format(number.normalize(), "f"))


def _iso(value: dt.date | str | None) -> str | None:
    return value.isoformat() if isinstance(value, dt.date) else value


@dataclass(frozen=True)
class Event:
    kind: str
    transaction: dict[str, Any]

    @property
    def transaction_id(self) -> str:
        return self.transaction["transaction_id"]


@dataclass
class FakeItem:
    """One fake Plaid Item and everything Plaid would know about it."""

    item_id: str
    access_token: str
    institution_id: str
    institution_name: str
    accounts: list[dict[str, Any]]
    events: list[Event] = field(default_factory=list)
    update_status: str = "HISTORICAL_UPDATE_COMPLETE"
    error: dict[str, Any] | None = None
    consent_expiration_time: str | None = None
    consented_products: list[str] = field(default_factory=lambda: ["transactions", "liabilities"])
    products: list[str] = field(default_factory=lambda: ["transactions"])
    liabilities: list[dict[str, Any]] | None = None
    last_successful_update: str | None = "2026-10-15T06:00:00Z"
    last_failed_update: str | None = None
    sync_calls: int = 0
    mutations: dict[int, list[Event]] = field(default_factory=dict)
    on_refresh: Callable[[FakeItem], None] | None = None
    refresh_count: int = 0

    @property
    def default_account_id(self) -> str:
        return self.accounts[0]["account_id"]


class FakePlaidGateway:
    """Implements ``plaid_gateway.PlaidGateway`` in memory (see module docstring)."""

    def __init__(self, *, plaid_env: str = "sandbox", page_size: int = 500) -> None:
        self.plaid_env = plaid_env
        self.page_size = page_size
        self.items: dict[str, FakeItem] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.link_tokens: list[dict[str, Any]] = []
        self.institutions = copy.deepcopy(DEFAULT_INSTITUTIONS)
        self._errors: dict[str, deque[BaseException]] = {}
        self._public_tokens: dict[str, str] = {}
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ setup helpers

    def add_item(
        self,
        *,
        item_id: str | None = None,
        institution_id: str = "ins_10",
        institution_name: str = "American Express",
        accounts: Sequence[Mapping[str, Any]] | None = None,
        update_status: str = "HISTORICAL_UPDATE_COMPLETE",
        consented_products: Sequence[str] = ("transactions", "liabilities"),
        consent_expiration_time: str | None = None,
    ) -> FakeItem:
        """Create an Item (with one credit-card account unless ``accounts`` is given)."""
        item_id = item_id or f"fake-item-{next(self._ids)}"
        if accounts is None:
            accounts = [
                plaid_account(account_id=f"{item_id}-card", mask=f"{1000 + len(self.items)}")
            ]
        item = FakeItem(
            item_id=item_id,
            access_token=f"access-{self.plaid_env}-{uuid.uuid4()}",
            institution_id=institution_id,
            institution_name=institution_name,
            accounts=[copy.deepcopy(dict(a)) for a in accounts],
            update_status=update_status,
            consented_products=list(consented_products),
            consent_expiration_time=consent_expiration_time,
        )
        self.items[item_id] = item
        return item

    def public_token_for(self, item: FakeItem) -> str:
        """A public token that :meth:`exchange_public_token` turns into ``item``'s access token."""
        token = f"public-{self.plaid_env}-{uuid.uuid4()}"
        self._public_tokens[token] = item.item_id
        return token

    def item_for_token(self, access_token: str) -> FakeItem:
        for item in self.items.values():
            if item.access_token == access_token:
                return item
        raise self.plaid_error(
            "INVALID_ACCESS_TOKEN", error_type="INVALID_INPUT", message="invalid access token"
        )

    def add_account(self, item: FakeItem, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("account_id", f"{item.item_id}-acc-{next(self._ids)}")
        account = plaid_account(**kwargs)
        item.accounts.append(account)
        return copy.deepcopy(account)

    def set_balances(self, item: FakeItem, account_id: str, **balances: Any) -> None:
        for account in item.accounts:
            if account["account_id"] == account_id:
                for key, value in balances.items():
                    account["balances"][key] = to_money(value) if value is not None else None

    def add_transaction(self, item: FakeItem, **fields: Any) -> dict[str, Any]:
        """Append an ``added`` event; returns the transaction dict (see ``factories.plaid_txn``)."""
        fields.setdefault("account_id", item.default_account_id)
        fields.setdefault("transaction_id", f"{item.item_id}-txn-{next(self._ids)}")
        txn = plaid_txn(**fields)
        item.events.append(Event(ADDED, txn))
        return copy.deepcopy(txn)

    def add_pending(self, item: FakeItem, **fields: Any) -> dict[str, Any]:
        fields["pending"] = True
        return self.add_transaction(item, **fields)

    def current_transactions(self, item: FakeItem) -> dict[str, dict[str, Any]]:
        """Net state after replaying every event (for assertions)."""
        state: dict[str, dict[str, Any]] = {}
        for event in item.events:
            if event.kind == REMOVED:
                state.pop(event.transaction_id, None)
            else:
                state[event.transaction_id] = event.transaction
        return copy.deepcopy(state)

    def modify_transaction(
        self, item: FakeItem, transaction_id: str, **changes: Any
    ) -> dict[str, Any]:
        """Append a ``modified`` event with ``changes`` applied to the current version."""
        current = self.current_transactions(item)[transaction_id]
        updated = {**current, **self._normalize(changes)}
        item.events.append(Event(MODIFIED, updated))
        return copy.deepcopy(updated)

    def remove_transaction(self, item: FakeItem, transaction_id: str) -> None:
        """Append a ``removed`` event (removing an unknown id is allowed, like Plaid)."""
        current = self.current_transactions(item).get(transaction_id)
        account_id = current["account_id"] if current else item.default_account_id
        item.events.append(
            Event(REMOVED, {"transaction_id": transaction_id, "account_id": account_id})
        )

    def post_pending(
        self,
        item: FakeItem,
        pending_id: str,
        *,
        amount: Any = None,
        new_id: str | None = None,
        date: dt.date | str | None = None,
        link: bool = True,
        added_first: bool = False,
        **changes: Any,
    ) -> dict[str, Any]:
        """Pending → posted: ``removed`` (pending id) + ``added`` (new id, linked by default).

        ``amount`` changes the posted amount (e.g. a tip); ``link=False`` leaves
        ``pending_transaction_id`` null, as Plaid does when it cannot match them.
        """
        pending = self.current_transactions(item)[pending_id]
        posted = {
            **pending,
            "transaction_id": new_id or f"{pending_id}-posted",
            "pending": False,
            "pending_transaction_id": pending_id if link else None,
            "authorized_date": pending.get("authorized_date") or pending["date"],
        }
        if amount is not None:
            posted["amount"] = to_money(amount)
        if date is not None:
            posted["date"] = _iso(date)
        posted.update(self._normalize(changes))
        events = [
            Event(REMOVED, {"transaction_id": pending_id, "account_id": pending["account_id"]}),
            Event(ADDED, posted),
        ]
        if added_first:
            events.reverse()
        item.events.extend(events)
        return copy.deepcopy(posted)

    def churn(
        self, item: FakeItem, transaction_id: str, *, new_id: str | None = None, **changes: Any
    ) -> dict[str, Any]:
        """Transaction churn: the old id is removed and an unlinked new id is added."""
        old = self.current_transactions(item)[transaction_id]
        new = {
            **old,
            "transaction_id": new_id or f"{transaction_id}-churned",
            "pending_transaction_id": None,
            **self._normalize(changes),
        }
        item.events.append(
            Event(REMOVED, {"transaction_id": transaction_id, "account_id": old["account_id"]})
        )
        item.events.append(Event(ADDED, new))
        return copy.deepcopy(new)

    def set_update_status(self, item: FakeItem, status: str) -> None:
        """``NOT_READY`` | ``INITIAL_UPDATE_COMPLETE`` | ``HISTORICAL_UPDATE_COMPLETE`` | …"""
        item.update_status = status

    def mutate_during_pagination(
        self, item: FakeItem, *, on_call: int, events: Sequence[Event] = ()
    ) -> None:
        """On the item's ``on_call``-th sync call, append ``events`` and fail with
        ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`` (callers restart from their cursor)."""
        item.mutations[on_call] = list(events)

    def queue_error(self, method: str, error: BaseException) -> None:
        """The next call of ``method`` raises ``error`` (FIFO; one error per queued entry)."""
        self._errors.setdefault(method, deque()).append(error)

    @staticmethod
    def plaid_error(
        code: str,
        *,
        error_type: str | None = None,
        status: int = 400,
        message: str | None = None,
        display_message: str | None = None,
        error_code_reason: str | None = None,
    ) -> PlaidError:
        """Build a :class:`PlaidError` as the real gateway would raise it."""
        if error_type is None:
            if code in ("TRANSACTIONS_SYNC_LIMIT", "TRIAL_CONNECTION_LIMIT", "RATE_LIMIT"):
                error_type = "RATE_LIMIT_EXCEEDED"
            elif code.startswith("INSTITUTION_"):
                error_type = "INSTITUTION_ERROR"
            elif code in ("INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE"):
                error_type = "API_ERROR"
            elif code.startswith("TRANSACTIONS_"):
                error_type = "TRANSACTIONS_ERROR"
            else:
                error_type = "ITEM_ERROR"
        if error_type == "RATE_LIMIT_EXCEEDED":
            status = 429
        return PlaidError(
            status,
            error_type,
            code,
            message or f"fake {code}",
            display_message,
            f"req-{uuid.uuid4().hex[:8]}",
            error_code_reason=error_code_reason,
        )

    @staticmethod
    def transport_error(message: str = "ReadTimeoutError: read timed out") -> PlaidTransportError:
        return PlaidTransportError(message)

    def set_item_error(
        self, item: FakeItem, code: str | None, *, error_code_reason: str | None = None
    ) -> None:
        """Put the Item in an error state (data calls fail with ``code``); ``None`` clears it."""
        if code is None:
            item.error = None
            return
        error = self.plaid_error(code, error_code_reason=error_code_reason)
        item.error = {
            "error_type": error.error_type,
            "error_code": code,
            "error_code_reason": error_code_reason,
            "error_message": error.error_message,
            "display_message": None,
        }

    def set_liabilities(self, item: FakeItem, credit: list[dict[str, Any]] | None) -> None:
        """``None`` = one default credit liability per credit account; ``[]`` = none."""
        item.liabilities = credit

    # ------------------------------------------------------------------ internals

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append((method, args))

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == method]

    def _maybe_fail(self, method: str) -> None:
        queue = self._errors.get(method)
        if queue:
            raise queue.popleft()

    def _sandbox_only(self, method: str) -> None:
        if self.plaid_env != "sandbox":
            raise PlaidEnvironmentError(f"{method} is a Sandbox-only call")

    def _raise_item_error(self, item: FakeItem) -> None:
        if item.error is not None:
            raise PlaidError.from_body(400, {**item.error, "request_id": "req-item-error"})

    def _request_id(self) -> str:
        return f"req-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _normalize(changes: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(changes)
        if "amount" in out:
            out["amount"] = to_money(out["amount"])
        for key in ("date", "authorized_date"):
            if key in out:
                out[key] = _iso(out[key])
        return out

    @staticmethod
    def _wire_transaction(txn: Mapping[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(dict(txn))
        out["amount"] = plaid_number(out["amount"])
        return out

    @staticmethod
    def _wire_account(account: Mapping[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(dict(account))
        balances = out.get("balances") or {}
        for key in ("available", "current", "limit"):
            balances[key] = plaid_number(balances.get(key))
        return out

    def _item_json(self, item: FakeItem) -> dict[str, Any]:
        return {
            "item_id": item.item_id,
            "institution_id": item.institution_id,
            "institution_name": item.institution_name,
            "webhook": "",
            "auth_method": None,
            "error": copy.deepcopy(item.error),
            "available_products": ["balance", "identity", "investments"],
            "billed_products": list(item.products),
            "products": list(item.products),
            "consented_products": list(item.consented_products),
            "consent_expiration_time": item.consent_expiration_time,
            "update_type": "background",
        }

    def _cursor(self, item: FakeItem, position: int) -> str:
        return base64.urlsafe_b64encode(f"{item.item_id}:{position}".encode()).decode()

    def _position(self, item: FakeItem, cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            item_id, position = base64.urlsafe_b64decode(cursor.encode()).decode().rsplit(":", 1)
            value = int(position)
        except (ValueError, UnicodeDecodeError) as exc:
            raise self.plaid_error("INVALID_FIELD", error_type="INVALID_INPUT") from exc
        if item_id != item.item_id or not 0 <= value <= len(item.events):
            raise self.plaid_error("INVALID_FIELD", error_type="INVALID_INPUT")
        return value

    # ------------------------------------------------------------------ the PlaidGateway protocol

    def create_link_token(
        self, *, access_token: str | None = None, additional_consented_products: Sequence[str] = ()
    ) -> str:
        self._record(
            "create_link_token",
            access_token=access_token,
            additional_consented_products=tuple(additional_consented_products),
        )
        self._maybe_fail("create_link_token")
        if access_token is not None:
            self.item_for_token(access_token)
        token = f"link-{self.plaid_env}-{uuid.uuid4()}"
        self.link_tokens.append(
            {
                "link_token": token,
                "access_token": access_token,
                "additional_consented_products": tuple(additional_consented_products),
            }
        )
        return token

    def exchange_public_token(self, public_token: str) -> ExchangeResult:
        self._record("exchange_public_token", public_token=public_token)
        self._maybe_fail("exchange_public_token")
        item_id = self._public_tokens.pop(public_token, None)
        if item_id is None:
            raise self.plaid_error("INVALID_PUBLIC_TOKEN", error_type="INVALID_INPUT")
        item = self.items[item_id]
        return ExchangeResult(access_token=item.access_token, item_id=item.item_id)

    def transactions_sync(
        self, access_token: str, cursor: str | None, *, count: int | None = None
    ) -> dict[str, Any]:
        self._record("transactions_sync", access_token=access_token, cursor=cursor, count=count)
        self._maybe_fail("transactions_sync")
        item = self.item_for_token(access_token)
        self._raise_item_error(item)
        item.sync_calls += 1
        if item.sync_calls in item.mutations:
            item.events.extend(item.mutations.pop(item.sync_calls))
            raise self.plaid_error(
                MUTATION_CODE,
                error_type="TRANSACTIONS_ERROR",
                message="Underlying transaction data changed since last page was fetched. "
                "Please restart pagination from last update.",
            )
        if item.update_status == "NOT_READY":
            return {
                "accounts": [],
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "",
                "has_more": False,
                "transactions_update_status": "NOT_READY",
                "request_id": self._request_id(),
            }
        position = self._position(item, cursor)
        size = min(count or self.page_size, self.page_size)
        page = item.events[position : position + size]
        end = position + len(page)
        buckets: dict[str, dict[str, dict[str, Any]]] = {ADDED: {}, MODIFIED: {}, REMOVED: {}}
        for event in page:
            bucket = buckets[event.kind]
            bucket.pop(event.transaction_id, None)
            bucket[event.transaction_id] = event.transaction
        touched = {t["account_id"] for bucket in buckets.values() for t in bucket.values()}
        return {
            "accounts": [
                self._wire_account(a) for a in item.accounts if a["account_id"] in touched
            ],
            "added": [self._wire_transaction(t) for t in buckets[ADDED].values()],
            "modified": [self._wire_transaction(t) for t in buckets[MODIFIED].values()],
            "removed": [
                {"transaction_id": t["transaction_id"], "account_id": t["account_id"]}
                for t in buckets[REMOVED].values()
            ],
            "next_cursor": self._cursor(item, end),
            "has_more": end < len(item.events),
            "transactions_update_status": item.update_status,
            "request_id": self._request_id(),
        }

    def transactions_refresh(self, access_token: str) -> None:
        self._record("transactions_refresh", access_token=access_token)
        self._maybe_fail("transactions_refresh")
        item = self.item_for_token(access_token)
        self._raise_item_error(item)
        item.refresh_count += 1
        if item.on_refresh is not None:
            item.on_refresh(item)

    def get_item(self, access_token: str) -> dict[str, Any]:
        """``/item/get`` answers 200 even when the Item is in an error state."""
        self._record("get_item", access_token=access_token)
        self._maybe_fail("get_item")
        item = self.item_for_token(access_token)
        return {
            "item": self._item_json(item),
            "status": {
                "transactions": {
                    "last_successful_update": item.last_successful_update,
                    "last_failed_update": item.last_failed_update,
                },
                "last_webhook": None,
            },
            "request_id": self._request_id(),
        }

    def get_accounts(self, access_token: str) -> dict[str, Any]:
        self._record("get_accounts", access_token=access_token)
        self._maybe_fail("get_accounts")
        item = self.item_for_token(access_token)
        self._raise_item_error(item)
        return {
            "accounts": [self._wire_account(a) for a in item.accounts],
            "item": self._item_json(item),
            "request_id": self._request_id(),
        }

    def get_liabilities(self, access_token: str) -> dict[str, Any]:
        self._record("get_liabilities", access_token=access_token)
        self._maybe_fail("get_liabilities")
        item = self.item_for_token(access_token)
        self._raise_item_error(item)
        if "liabilities" not in item.consented_products:
            raise self.plaid_error("ADDITIONAL_CONSENT_REQUIRED", error_type="INVALID_INPUT")
        credit_accounts = [a for a in item.accounts if a.get("type") == "credit"]
        if item.liabilities is None:
            credit = [plaid_credit_liability(account_id=a["account_id"]) for a in credit_accounts]
        else:
            credit = copy.deepcopy(item.liabilities)
        if not credit:
            raise self.plaid_error("NO_LIABILITY_ACCOUNTS", error_type="ITEM_ERROR")
        wired = []
        for entry in credit:
            entry = dict(entry)
            for key in (
                "last_payment_amount",
                "last_statement_balance",
                "minimum_payment_amount",
            ):
                entry[key] = plaid_number(entry.get(key))
            wired.append(entry)
        return {
            "accounts": [self._wire_account(a) for a in credit_accounts],
            "item": self._item_json(item),
            "liabilities": {"credit": wired, "mortgage": None, "student": None},
            "request_id": self._request_id(),
        }

    def search_institutions(self, query: str, products: Sequence[str]) -> dict[str, Any]:
        self._record("search_institutions", query=query, products=tuple(products))
        self._maybe_fail("search_institutions")
        wanted = set(products)
        found = [
            copy.deepcopy(inst)
            for inst in self.institutions
            if query.casefold() in inst["name"].casefold() and wanted <= set(inst["products"])
        ]
        return {"institutions": found, "request_id": self._request_id()}

    def sandbox_create_public_token(
        self,
        institution_id: str,
        *,
        override_username: str | None = None,
        override_password: str | None = None,
    ) -> str:
        self._record(
            "sandbox_create_public_token",
            institution_id=institution_id,
            override_username=override_username,
        )
        self._sandbox_only("sandbox_create_public_token")
        self._maybe_fail("sandbox_create_public_token")
        name = next(
            (i["name"] for i in self.institutions if i["institution_id"] == institution_id),
            "Sandbox Bank",
        )
        item = self.add_item(institution_id=institution_id, institution_name=name)
        return self.public_token_for(item)

    def sandbox_reset_login(self, access_token: str) -> None:
        self._record("sandbox_reset_login", access_token=access_token)
        self._sandbox_only("sandbox_reset_login")
        self._maybe_fail("sandbox_reset_login")
        self.set_item_error(self.item_for_token(access_token), "ITEM_LOGIN_REQUIRED")

    def sandbox_create_transactions(
        self, access_token: str, account_id: str, transactions: Sequence[Mapping[str, Any]]
    ) -> None:
        self._record("sandbox_create_transactions", account_id=account_id, count=len(transactions))
        self._sandbox_only("sandbox_create_transactions")
        self._maybe_fail("sandbox_create_transactions")
        if not 1 <= len(transactions) <= 10:
            raise ValueError("send between 1 and 10 Sandbox transactions per call")
        item = self.item_for_token(access_token)
        for t in transactions:
            self.add_transaction(
                item,
                account_id=account_id,
                amount=t["amount"],
                date=t["date_posted"],
                authorized_date=t["date_transacted"],
                name=t["description"],
                merchant_name=None,
                pfc_primary=None,
                pfc_detailed=None,
            )
