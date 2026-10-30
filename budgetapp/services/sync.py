"""The sync engine: ``/transactions/sync`` into the ledger, then balances, consent and statements.

ARCHITECTURE §6 ("Sync apply algorithm", "Locks") and §8; SYNC-2, SYNC-4 … SYNC-7, REL-1.

:func:`sync_item` is the one way Plaid data reaches the ledger::

    wait for the database, schema guard
    sync lock (busy → run recorded 'skipped'); older 'running' runs → 'interrupted'
    sync run 'running'                                   (own short transaction)
    fetch EVERY page from the stored cursor; a mutation or transport error discards the pages
        and restarts from the same cursor (bounded); NOT_READY → success, nothing changes
    ONE transaction: ledger lock → fold → accounts → upserts → deletes (tombstones) →
        unlinked replacements → history/cutover → reconcile → cursor compare-and-set
    sync run 'success' with counts                       (own short transaction)
    best effort, each in its own transaction: /accounts/get, /item/get, /liabilities/get,
        alerts.evaluate_after_sync

A Plaid error leaves the ledger and the cursor untouched, records the run and the Item's status
(``needs_reconnect`` or ``error``) and calls the alerts hook. Any other exception is recorded as
``INTERNAL`` (Item status ``error``), the alerts hook runs, and the exception is re-raised.

Several transactions are needed (bookkeeping must survive a rolled-back apply), so this module takes
a ``sessionmaker`` instead of a session — the documented exception to "services take a session".

Multi-step work for the web app ("Sync now", the backfill after linking or update mode) is written
as re-scheduling steps (:class:`RefreshThenSync`, :class:`Backfill`): each call does one sync and
returns the seconds until the next, so ``services.background`` can run them without blocking its
worker, and commands run them in a loop with :func:`background.run_steps`.
"""

from __future__ import annotations

import datetime as dt
import difflib
import logging
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import clock, db, secrets
from budgetapp.config import Settings
from budgetapp.logsetup import redact
from budgetapp.models import (
    SYNC_TRIGGERS,
    Account,
    PlaidItem,
    Statement,
    SyncRun,
    Transaction,
    TransactionTombstone,
)
from budgetapp.money import is_cents, to_money, to_money_or_none
from budgetapp.plaid_gateway import (
    TRANSPORT_ERROR,
    PlaidGateway,
    PlaidGatewayError,
    item_status_for,
)
from budgetapp.services import alerts, reconcile, userstate
from budgetapp.services.background import run_steps
from budgetapp.services.categorize import Categorizer, apply_carried_state, apply_decision
from budgetapp.services.events import SyncOutcome
from budgetapp.services.notify import Notifier

log = logging.getLogger(__name__)

NOT_READY = "NOT_READY"
INITIAL_UPDATE_COMPLETE = "INITIAL_UPDATE_COMPLETE"
HISTORICAL_UPDATE_COMPLETE = "HISTORICAL_UPDATE_COMPLETE"
INTERNAL = "INTERNAL"
KEYCHAIN_APPROVAL = "KEYCHAIN_APPROVAL_NEEDED"
ACCESS_TOKEN_MISSING = "ACCESS_TOKEN_MISSING"

#: Restarts of one sync's page loop after a mutation or transport error (RESEARCH T1.3: ~5).
MAX_PAGINATION_RESTARTS = 5
#: Refetches after the cursor compare-and-set found another cursor (should never happen: the
#: sync lock is held).
MAX_CURSOR_RETRIES = 2
#: A guard against a ``has_more`` loop that never ends (500 rows a page).
MAX_PAGES = 5000
TOMBSTONE_RETENTION = dt.timedelta(days=90)
REPLACEMENT_WINDOW = dt.timedelta(days=30)
REPLACEMENT_MAX_DAYS = 7
REPLACEMENT_MIN_SIMILARITY = 0.5
#: ``--if-due`` / ``sync_all(if_due=True)``: skip Items whose last success is newer than this.
DUE_AFTER = dt.timedelta(hours=5)
#: "Sync now": a sync right after ``/transactions/refresh``, then after ~15 s, 45 s and 2 min.
FOLLOW_UP_DELAYS: tuple[float, ...] = (15.0, 30.0, 75.0)
#: After linking: re-sync until HISTORICAL_UPDATE_COMPLETE (RESEARCH T1.2), at most ~1 hour.
BACKFILL_DELAYS: tuple[float, ...] = (15.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)
BACKFILL_CAP = 3600.0
#: After update mode: a sync now and once more shortly after (Plaid fetches new data right away
#: when the Item was broken for over a day), then the backfill rhythm if history is incomplete.
UPDATE_DELAYS: tuple[float, ...] = (60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)

_RATE_LIMIT_TYPE = "RATE_LIMIT_EXCEEDED"
_LIABILITIES_STATUS = {
    "ADDITIONAL_CONSENT_REQUIRED": "not_consented",
    "ACCESS_NOT_GRANTED": "not_consented",
    "NO_LIABILITY_ACCOUNTS": "unsupported",
    "PRODUCTS_NOT_SUPPORTED": "unsupported",
}


class MissingAccessToken(LookupError):
    """No access token for an Item in the secret store (the message never holds a token)."""


class _CursorMoved(Exception):
    """The stored cursor is not the one this sync started from (compare-and-set failed)."""

    def __init__(self, current: str | None) -> None:
        super().__init__("the stored cursor changed during the sync")
        self.current = current


# --------------------------------------------------------------------------- small helpers


def _date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def parse_timestamp(value: Any) -> dt.datetime | None:
    """An aware datetime from Plaid's ISO 8601 text (``None`` when absent or unreadable)."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            log.warning("Unreadable timestamp from Plaid: %r", value)
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clip(text: str | None, limit: int = 500) -> str | None:
    if text is None:
        return None
    text = redact(" ".join(str(text).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunks(values: Sequence[Any], size: int = 1000) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _set(obj: Any, name: str, value: Any) -> bool:
    """Set an attribute only when it differs; True when it changed."""
    if getattr(obj, name) == value:
        return False
    setattr(obj, name, value)
    return True


def access_token_for(settings: Settings, plaid_item_id: str, store: secrets.SecretStore) -> str:
    """The Item's access token from the secret store, or :class:`MissingAccessToken`."""
    token = secrets.get_access_token(settings.plaid_env, plaid_item_id, store=store)
    if not token:
        raise MissingAccessToken(
            f"No access token for Item {plaid_item_id} in Keychain. Restore it with ./run setup "
            f"--env {settings.plaid_env} --access-token (never link a new connection)."
        )
    return token


def ensure_database(settings: Settings, bind: Any) -> None:
    """Every entry point: wait for the database (up to ~120 s), then the schema guard.

    Raises ``db.DatabaseUnavailable`` ("try later", never an alert) or ``db.SchemaMismatch``.
    """
    if not db.ping(bind) and not db.wait_for_db(settings):
        raise db.DatabaseUnavailable("The database is not ready; the next sync will try again.")
    db.schema_guard(bind)


def describe_error(error_code: str | None, error_type: str | None = None) -> str | None:
    """A plain sentence about a stored sync error, for the Connection page."""
    if not error_code:
        return None
    if error_code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"):
        return (
            "Amex needs you to sign in again through Plaid (the consent expired or was revoked). "
            "Reconnect fixes it and keeps this connection."
        )
    if error_code in ("ACCESS_NOT_GRANTED", "USER_PERMISSION_REVOKED", "USER_SETUP_REQUIRED"):
        return "Amex stopped sharing data with Plaid. Reconnect and allow access to your card."
    if error_code in ("INVALID_CREDENTIALS", "ITEM_LOCKED"):
        return "Amex didn't accept the sign-in. Reconnect to sign in again."
    if error_code == "ITEM_NOT_FOUND":
        return (
            "Plaid no longer has this connection (it was removed). Syncing can't continue; "
            "the data already here stays."
        )
    if error_code == "INVALID_ACCESS_TOKEN":
        return "Plaid didn't accept the saved access token. Run ./run doctor to check the keys."
    if error_code == "TRIAL_CONNECTION_LIMIT":
        return "Your Plaid Trial plan has used all 10 connections; Plaid refuses new ones."
    if error_code == TRANSPORT_ERROR:
        return "Plaid couldn't be reached. The next sync will try again."
    if error_code == INTERNAL:
        return "Something went wrong in the app during the sync. The details are in the sync log."
    if error_code == KEYCHAIN_APPROVAL:
        return (
            "macOS Keychain needs your approval before the app can read the access token. Run "
            "./run doctor --env production in Terminal and click Always Allow."
        )
    if error_code == ACCESS_TOKEN_MISSING:
        return (
            "The access token is missing from Keychain. Restore it with ./run setup --env "
            "production --access-token; never link a new connection."
        )
    if error_type == _RATE_LIMIT_TYPE or error_code.endswith("_LIMIT"):
        return "Plaid asked the app to slow down. The next sync will try again."
    if error_code.startswith("INSTITUTION_") or error_type == "INSTITUTION_ERROR":
        return "Amex isn't answering Plaid right now. The next sync will try again."
    if error_code in ("INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE"):
        return "Plaid had a problem on its side. The next sync will try again."
    return None


# --------------------------------------------------------------------------- folding pages


@dataclass(slots=True)
class Batch:
    """All pages of one sync folded into one final state per transaction id (§6 step 1)."""

    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: removed transaction id → its Plaid account id
    removed: dict[str, str | None] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_cursor: str = ""
    update_status: str | None = None


def fold_pages(pages: Sequence[Mapping[str, Any]]) -> Batch:
    """Fold ``/transactions/sync`` pages, in order: the last occurrence of an id wins; within one
    page ``removed`` beats ``modified``, which beats ``added``."""
    batch = Batch()
    for page in pages:
        for account in page.get("accounts") or ():
            batch.accounts[account["account_id"]] = account
        for kind in ("added", "modified"):
            for txn in page.get(kind) or ():
                tid = txn["transaction_id"]
                batch.removed.pop(tid, None)
                batch.payloads.pop(tid, None)  # re-insert so the order follows the last occurrence
                batch.payloads[tid] = txn
        for gone in page.get("removed") or ():
            tid = gone["transaction_id"]
            batch.payloads.pop(tid, None)
            batch.removed[tid] = gone.get("account_id")
        batch.next_cursor = page.get("next_cursor") or ""
        batch.update_status = page.get("transactions_update_status")
    return batch


def _fetch_pages(
    gateway: PlaidGateway,
    token: str,
    start: str | None,
    *,
    sleep: Callable[[float], None],
) -> tuple[list[dict[str, Any]], bool]:
    """Every page from ``start`` until ``has_more`` is false: ``(pages, ready)``.

    ``ready`` is False for Plaid's NOT_READY (empty ``next_cursor``). A mutation during pagination
    or a transport failure discards the pages and restarts from ``start`` (bounded, with
    jittered backoff); any other error propagates.
    """
    restarts = 0
    while True:
        pages: list[dict[str, Any]] = []
        cursor = start
        try:
            while True:
                page = gateway.transactions_sync(token, cursor)
                pages.append(page)
                next_cursor = page.get("next_cursor") or ""
                if not next_cursor:
                    return pages, False
                if not page.get("has_more"):
                    return pages, True
                if len(pages) >= MAX_PAGES:
                    raise RuntimeError(f"/transactions/sync returned more than {MAX_PAGES} pages")
                cursor = next_cursor
        except PlaidGatewayError as err:
            if not err.restart_pagination or restarts >= MAX_PAGINATION_RESTARTS:
                raise
            restarts += 1
            log.info(
                "Restarting /transactions/sync from the stored cursor (%s, attempt %d)",
                err.error_code,
                restarts,
            )
            sleep(min(30.0, 0.5 * 2 ** (restarts - 1)) * (0.75 + random.random() / 2))


# --------------------------------------------------------------------------- description match

_NON_WORD = re.compile(r"[^0-9a-z]+")


def _normalize(text: str | None) -> str:
    words = _NON_WORD.sub(" ", (text or "").casefold()).split()
    return " ".join(w for w in words if not w.isdigit())


def description_similarity(
    a_merchant: str | None, a_name: str | None, b_merchant: str | None, b_name: str | None
) -> float:
    """0–1: how alike two charges' descriptions are (merchant names first, then raw names)."""
    best = 0.0
    for x, y in (
        (a_merchant, b_merchant),
        (a_name, b_name),
        (a_merchant, b_name),
        (a_name, b_merchant),
    ):
        nx, ny = _normalize(x), _normalize(y)
        if not nx or not ny:
            continue
        if nx == ny:
            return 1.0
        tx, ty = set(nx.split()), set(ny.split())
        if tx <= ty or ty <= tx:
            best = max(best, 0.9)
        best = max(best, difflib.SequenceMatcher(None, nx, ny).ratio())
    return best


def replacement_score(tomb: TransactionTombstone, txn: Transaction) -> Decimal | None:
    """How well ``txn`` (inserted, inheriting nothing) replaces ``tomb`` (§6 step 5), or None.

    Requires the same account, the same sign, an amount within ``max($1, 20%)``, spend dates
    within 7 days and similar descriptions. Higher is better; a posted row scores higher for a
    pending tombstone.
    """
    if tomb.account_id != txn.account_id:
        return None
    old, new = to_money(tomb.amount), to_money(txn.amount)
    if (old < 0) != (new < 0):
        return None
    limit = max(Decimal("1.00"), abs(old) * Decimal("0.20"))
    diff = abs(old - new)
    if diff > limit:
        return None
    days = abs(((tomb.authorized_date or tomb.date) - (txn.authorized_date or txn.date)).days)
    if days > REPLACEMENT_MAX_DAYS:
        return None
    similarity = description_similarity(tomb.merchant_name, tomb.name, txn.merchant_name, txn.name)
    if similarity < REPLACEMENT_MIN_SIMILARITY:
        return None
    score = Decimal(str(round(similarity, 4))) * 2
    score += 1 - diff / limit
    score += Decimal(REPLACEMENT_MAX_DAYS - days) / REPLACEMENT_MAX_DAYS
    if diff == 0:
        score += Decimal("0.25")
    if tomb.pending and not txn.pending:
        score += Decimal("0.5")
    return score


def match_replacements(
    tombs: Iterable[TransactionTombstone], candidates: Iterable[Transaction]
) -> list[tuple[TransactionTombstone, Transaction]]:
    """One-to-one ``(tombstone, row)`` pairs by :func:`replacement_score`, best first (ties: the
    newest tombstone, then the lowest row id). Rows are looked up only among the same account,
    sign and spend dates within 7 days, so an account churn of thousands of rows stays fast."""
    window = range(-REPLACEMENT_MAX_DAYS, REPLACEMENT_MAX_DAYS + 1)
    buckets: dict[tuple[int, bool, dt.date], list[Transaction]] = {}
    for txn in candidates:
        key = (txn.account_id, txn.amount < 0, txn.authorized_date or txn.date)
        buckets.setdefault(key, []).append(txn)
    pairs: list[tuple[Decimal, int, int, TransactionTombstone, Transaction]] = []
    for tomb in tombs:
        day = tomb.authorized_date or tomb.date
        negative = tomb.amount < 0
        for offset in window:
            key = (tomb.account_id, negative, day + dt.timedelta(days=offset))
            for txn in buckets.get(key, ()):
                score = replacement_score(tomb, txn)
                if score is not None:
                    pairs.append((score, -int(tomb.removed_at.timestamp()), txn.id, tomb, txn))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used_tombs: set[str] = set()
    used_rows: set[int] = set()
    matched: list[tuple[TransactionTombstone, Transaction]] = []
    for _, _, _, tomb, txn in pairs:
        if tomb.plaid_transaction_id in used_tombs or id(txn) in used_rows:
            continue
        used_tombs.add(tomb.plaid_transaction_id)
        used_rows.add(id(txn))
        matched.append((tomb, txn))
    return matched


# --------------------------------------------------------------------------- the apply step


@dataclass(frozen=True, slots=True)
class _Applied:
    added: int
    modified: int
    removed: int
    #: the inserted rows that are new charges (not a replacement of a row the app already had)
    new_charge_ids: tuple[int, ...]
    update_status: str | None
    history_completed_now: bool


def _plaid_fields(payload: Mapping[str, Any], account_id: int) -> dict[str, Any]:
    """The Plaid-owned columns of a transaction from a ``/transactions/sync`` entry."""
    pfc = payload.get("personal_finance_category") or {}
    day = _date(payload.get("date"))
    if day is None:
        raise ValueError(f"Plaid transaction {payload.get('transaction_id')} has no date")
    pending = bool(payload.get("pending"))
    location = payload.get("location")
    if not isinstance(location, Mapping) or all(v is None for v in location.values()):
        location = None
    name = (
        _text(payload.get("name"))
        or _text(payload.get("merchant_name"))
        or _text(payload.get("original_description"))
        or "Unknown transaction"
    )
    return {
        "account_id": account_id,
        "pending_transaction_id": _text(payload.get("pending_transaction_id")),
        "pending": pending,
        "date": day,
        "authorized_date": _date(payload.get("authorized_date")),
        "posted_date": None if pending else day,
        "name": name,
        "merchant_name": _text(payload.get("merchant_name")),
        "original_description": _text(payload.get("original_description")),
        "amount": to_money(payload["amount"]),
        "iso_currency_code": _text(payload.get("iso_currency_code")),
        "pfc_primary": _text(pfc.get("primary")),
        "pfc_detailed": _text(pfc.get("detailed")),
        "pfc_confidence": _text(pfc.get("confidence_level")),
        "pfc_version": _text(pfc.get("version")),
        "transaction_code": _text(payload.get("transaction_code")),
        "merchant_category_code": _text(payload.get("merchant_category_code")),
        "merchant_entity_id": _text(payload.get("merchant_entity_id")),
        "payment_channel": _text(payload.get("payment_channel")),
        "logo_url": _text(payload.get("logo_url")),
        "website": _text(payload.get("website")),
        "location": dict(location) if location is not None else None,
        "account_owner": _text(payload.get("account_owner")),
    }


def _money_problem(payload: Mapping[str, Any]) -> str | None:
    """A review note when Plaid's amount or currency can't be trusted as-is (REL-4)."""
    raw = payload.get("amount")
    if isinstance(raw, Decimal) and not is_cents(raw):
        return (
            f"Plaid sent the amount {raw}, with more than two decimals; it was rounded to "
            f"{to_money(raw)}. Check it against your statement."
        )
    if not _text(payload.get("iso_currency_code")):
        other = _text(payload.get("unofficial_currency_code"))
        detail = f" (it named {other})" if other else ""
        return (
            f"Plaid sent this charge without a currency{detail}; it is counted as US dollars. "
            "Check it against your statement."
        )
    return None


def has_user_state() -> Any:
    """SQL: the tombstone carries user edits.

    A Python ``None`` in a JSONB column is stored as JSON ``null``, not SQL NULL, so "no edits"
    can be either; only a JSON object holds edits.
    """
    return func.jsonb_typeof(TransactionTombstone.user_state) == "object"


def _flag(txn: Transaction, reason: str) -> None:
    if txn.needs_review and txn.review_reason:
        return  # keep the earlier reason (e.g. rescaled splits)
    txn.needs_review = True
    txn.review_reason = reason


class _Applier:
    """Applies one folded batch inside the caller's transaction (§6 steps 2–5).

    It also tells new charges from replacements: an inserted row *continues* a charge the app
    already had when it names a known pending row or tombstone (step 3), inherits a tombstone's
    edits (step 5), or matches an unconsumed tombstone without edits by the same rules (a posted
    charge Plaid didn't link to its pending one, or a re-delivery after Plaid re-issued an
    account). Only new charges reach ``SyncOutcome.inserted_transaction_ids`` (ALR-3 alerts once
    per charge).
    """

    def __init__(self, session: Session, item: PlaidItem, batch: Batch, now: dt.datetime) -> None:
        self.s = session
        self.item = item
        self.batch = batch
        self.now = now
        self.categorizer = Categorizer.load(session)
        self.accounts: dict[str, Account] = {}
        self.rows: dict[str, Transaction] = {}
        self.tombstones: dict[str, TransactionTombstone] = {}
        self.inserted: list[Transaction] = []
        self.continued: set[int] = set()  # id() of inserted rows that replace a known charge
        self.deleted: set[str] = set()
        self.consumed: set[str] = set()  # tombstones a row of this batch has taken over
        self.consumptions: list[tuple[TransactionTombstone, Transaction]] = []
        self.stored_links: set[str] = set()
        self.batch_links: set[str] = set()
        self.modified = 0
        self.removed = 0

    # ---------------------------------------------------------------- step 2: accounts

    def apply_accounts(self) -> None:
        known = {
            a.plaid_account_id: a
            for a in self.s.scalars(select(Account).where(Account.item_id == self.item.id))
        }
        seen = set(self.batch.accounts)
        for pid, data in self.batch.accounts.items():
            account = (
                known.get(pid)
                or self.s.scalars(
                    select(Account).where(Account.plaid_account_id == pid)
                ).one_or_none()
            )
            if account is None:
                account = churned_account(known, seen, data)
                if account is not None:
                    known.pop(account.plaid_account_id, None)
                    reissue_account(account, pid, self.now)
                    log.warning("Account churn on Item %s: an account was re-issued", self.item.id)
                else:
                    account = Account(item_id=self.item.id, plaid_account_id=pid)
                    self.s.add(account)
            known[pid] = account
            update_account(account, data, self.now)
        for payload in self.batch.payloads.values():
            pid = payload["account_id"]
            if pid not in known:  # an account Plaid didn't list: /accounts/get fills it in
                account = Account(item_id=self.item.id, plaid_account_id=pid)
                self.s.add(account)
                known[pid] = account
        self.s.flush()
        self.accounts = known

    # ---------------------------------------------------------------- preload

    def preload(self) -> None:
        payload_ids = list(self.batch.payloads)
        links = {
            p["pending_transaction_id"]
            for p in self.batch.payloads.values()
            if p.get("pending_transaction_id")
        }
        wanted = list(dict.fromkeys([*payload_ids, *links, *self.batch.removed]))
        for chunk in _chunks(wanted):
            for txn in self.s.scalars(
                select(Transaction).where(Transaction.plaid_transaction_id.in_(chunk))
            ):
                self.rows[txn.plaid_transaction_id] = txn
            for tomb in self.s.scalars(
                select(TransactionTombstone).where(
                    TransactionTombstone.plaid_transaction_id.in_(chunk)
                )
            ):
                self.tombstones[tomb.plaid_transaction_id] = tomb
        for chunk in _chunks(payload_ids):
            self.stored_links.update(
                self.s.scalars(
                    select(Transaction.pending_transaction_id).where(
                        Transaction.pending_transaction_id.in_(chunk)
                    )
                )
            )
        self.batch_links = links

    # ---------------------------------------------------------------- step 3: upserts

    def apply_upserts(self) -> None:
        for tid, payload in self.batch.payloads.items():
            account = self.accounts[payload["account_id"]]
            existing = self.rows.get(tid)
            if existing is not None and tid not in self.deleted:
                if tid in self.batch_links:
                    continue  # a posted row in this batch replaces it (consumed below)
                if self._update(existing, payload, account.id):
                    self.modified += 1
                continue
            if tid in self.deleted or tid in self.stored_links or tid in self.batch_links:
                continue  # stale: a stored or incoming posted row already replaced it
            if tid in self.tombstones:
                continue  # removed earlier: never resurrect it
            self._insert(tid, payload, account.id)
        self.s.flush()
        for tomb, txn in self.consumptions:
            tomb.consumed_by_transaction_id = txn.id

    def _update(self, txn: Transaction, payload: Mapping[str, Any], account_id: int) -> bool:
        old_amount, old_pending = txn.amount, txn.pending
        changed = False
        for name, value in _plaid_fields(payload, account_id).items():
            changed |= _set(txn, name, value)
        if not changed:
            return False
        if txn.category_source != "user":
            apply_decision(txn, self.categorizer.decide(txn))
        if txn.splits and (to_money(old_amount) != txn.amount or old_pending != txn.pending):
            userstate.fit_splits_to_amount(txn)
        problem = _money_problem(payload)
        if problem:
            _flag(txn, problem)
        return True

    def _insert(self, tid: str, payload: Mapping[str, Any], account_id: int) -> None:
        fields = _plaid_fields(payload, account_id)
        state: userstate.UserState | None = None
        predecessor: TransactionTombstone | None = None
        link = fields["pending_transaction_id"]
        if link:
            previous = self.rows.get(link)
            if previous is not None and link not in self.deleted:
                # Pending → posted in one step: the posted row takes the pending row's place.
                state = userstate.capture(previous)
                predecessor = self._tombstone(previous, state)
                self._delete([previous])
            else:
                tomb = self.tombstones.get(link)
                if (
                    tomb is not None
                    and tomb.consumed_by_transaction_id is None
                    and link not in self.consumed
                ):
                    state = userstate.from_json(tomb.user_state)  # removed in an earlier sync
                    predecessor = tomb
        txn = Transaction(source="plaid", plaid_transaction_id=tid, **fields)
        with self.s.no_autoflush:  # the row is complete only once it has a category
            self.s.add(txn)
            apply_carried_state(self.categorizer, txn, state)
            problem = _money_problem(payload)
            if problem:
                _flag(txn, problem)
        if predecessor is not None:
            self.consumptions.append((predecessor, txn))
            self.consumed.add(predecessor.plaid_transaction_id)
        if predecessor is not None or (link and (link in self.rows or link in self.tombstones)):
            self.continued.add(id(txn))  # the pending charge it names was already here
        self.inserted.append(txn)

    # ---------------------------------------------------------------- step 4: deletes

    def _tombstone(
        self, txn: Transaction, state: userstate.UserState | None
    ) -> TransactionTombstone:
        tid = txn.plaid_transaction_id
        assert tid is not None
        values = {
            "account_id": txn.account_id,
            "amount": to_money(txn.amount),
            "date": txn.date,
            "authorized_date": txn.authorized_date,
            "name": txn.name,
            "merchant_name": txn.merchant_name,
            "pending": txn.pending,
            "user_state": userstate.to_json(state),
            "removed_at": self.now,
            "consumed_by_transaction_id": None,
        }
        tomb = self.tombstones.get(tid)
        if tomb is None:
            tomb = TransactionTombstone(plaid_transaction_id=tid, **values)
            self.s.add(tomb)
            self.tombstones[tid] = tomb
        else:
            for name, value in values.items():
                setattr(tomb, name, value)
        return tomb

    def _delete(self, rows: Sequence[Transaction]) -> None:
        """Delete rows whose state was captured into their tombstones.

        Tombstones these rows had consumed hand their edits over to the new tombstones: their
        ``consumed_by`` link is about to become NULL (ON DELETE SET NULL), and they must never be
        offered as "unconsumed edits" again.
        """
        ids = [t.id for t in rows if t.id is not None]
        for chunk in _chunks(ids):
            self.s.execute(
                update(TransactionTombstone)
                .where(TransactionTombstone.consumed_by_transaction_id.in_(chunk))
                .values(user_state=None)
                .execution_options(synchronize_session="fetch")
            )
        for txn in rows:
            self.s.delete(txn)
            self.deleted.add(txn.plaid_transaction_id or "")
            self.removed += 1

    def apply_deletes(self) -> None:
        doomed: list[Transaction] = []
        for tid in self.batch.removed:
            txn = self.rows.get(tid)
            if txn is None or tid in self.deleted:
                continue
            self._tombstone(txn, userstate.capture(txn))
            doomed.append(txn)
        self._delete(doomed)
        self.s.flush()

    # ---------------------------------------------------------------- step 5: replacements

    def apply_replacements(self) -> int:
        """Step 5: unconsumed tombstones from the last 30 days (this batch's too) and the rows
        this batch inserted that replace nothing yet, matched one-to-one. Tombstones with edits go
        first and hand their edits over; then those without edits are matched only to record the
        replacement (``consumed_by_transaction_id``) — so the row is known as the same charge.
        Returns how many rows inherited edits."""
        candidates = [t for t in self.inserted if id(t) not in self.continued]
        if not candidates:
            return 0
        edited: list[TransactionTombstone] = []
        plain: list[TransactionTombstone] = []
        for tomb in self.s.scalars(
            select(TransactionTombstone).where(
                TransactionTombstone.consumed_by_transaction_id.is_(None),
                TransactionTombstone.removed_at >= self.now - REPLACEMENT_WINDOW,
            )
        ):
            if tomb.plaid_transaction_id in self.consumed:
                continue  # a row of this batch took it over in step 3 (not flushed yet)
            (plain if userstate.from_json(tomb.user_state) is None else edited).append(tomb)
        carried = 0
        for tomb, txn in match_replacements(edited, candidates):
            apply_carried_state(self.categorizer, txn, userstate.from_json(tomb.user_state))
            tomb.consumed_by_transaction_id = txn.id
            self.continued.add(id(txn))
            carried += 1
        rest = [t for t in candidates if id(t) not in self.continued]
        for tomb, txn in match_replacements(plain, rest) if rest else ():
            tomb.consumed_by_transaction_id = txn.id
            self.continued.add(id(txn))
        self.s.flush()
        return carried

    # ---------------------------------------------------------------- history (CSV cutover)

    def update_history(self, update_status: str | None) -> bool:
        """``history_complete_at`` the first time HISTORICAL_UPDATE_COMPLETE is seen, then each
        account's ``plaid_history_start`` (earliest Plaid posted date). True when completed now."""
        completed_now = False
        if update_status == HISTORICAL_UPDATE_COMPLETE and self.item.history_complete_at is None:
            self.item.history_complete_at = self.now
            completed_now = True
        if self.item.history_complete_at is None:
            return completed_now
        starts = dict(
            self.s.execute(
                select(Transaction.account_id, func.min(Transaction.posted_date))
                .join(Account, Account.id == Transaction.account_id)
                .where(
                    Account.item_id == self.item.id,
                    Transaction.source == "plaid",
                    Transaction.posted_date.is_not(None),
                )
                .group_by(Transaction.account_id)
            ).all()
        )
        for account in self.s.scalars(select(Account).where(Account.item_id == self.item.id)):
            first = starts.get(account.id)
            if first is not None and (
                account.plaid_history_start is None or first < account.plaid_history_start
            ):
                account.plaid_history_start = first
        return completed_now


def churned_account(
    known: Mapping[str, Account], listed: set[str], data: Mapping[str, Any]
) -> Account | None:
    """The existing account that a new Plaid account id re-issues (ARCHITECTURE §6 step 2): the
    one account of the Item with the same type, subtype and mask whose own id Plaid no longer
    lists in ``listed``. ``None`` when there is no such account or more than one."""
    mask = _text(data.get("mask"))
    if mask is None:
        return None
    matches = [
        a
        for pid, a in known.items()
        if pid not in listed
        and a.mask == mask
        and a.type == _text(data.get("type"))
        and a.subtype == _text(data.get("subtype"))
    ]
    return matches[0] if len(matches) == 1 else None


def reissue_account(account: Account, new_id: str, now: dt.datetime) -> None:
    """Point ``account`` at the id Plaid re-issued it under, and tell the user (``notice``)."""
    account.plaid_account_id = new_id
    account.notice = (
        f"Plaid re-issued this card's account on {now.astimezone():%b %-d, %Y} (American "
        "Express can do this after a card replacement). Its history was matched to this card; "
        "check that nothing is missing or doubled."
    )


def update_account(account: Account, data: Mapping[str, Any], now: dt.datetime) -> bool:
    """Copy an AccountBase (``/accounts/get`` or a sync page) onto ``account``; True if changed."""
    changed = False
    for name in ("name", "official_name", "mask", "type", "subtype"):
        value = _text(data.get(name))
        if value is not None:
            changed |= _set(account, name, value)
    balances = data.get("balances")
    if isinstance(balances, Mapping):
        currency = _text(balances.get("iso_currency_code")) or _text(
            balances.get("unofficial_currency_code")
        )
        if currency:
            changed |= _set(account, "iso_currency_code", currency)
        changed |= _set(account, "current_balance", to_money_or_none(balances.get("current")))
        changed |= _set(account, "available_balance", to_money_or_none(balances.get("available")))
        changed |= _set(account, "credit_limit", to_money_or_none(balances.get("limit")))
        account.balances_as_of = now
    return changed


# --------------------------------------------------------------------------- bookkeeping


def _start_run(
    session_factory: sessionmaker[Session], item_id: int, trigger: str, run_id: int | None
) -> tuple[int, str | None, str, bool]:
    """Mark older 'running' runs interrupted and open this run (own transaction).

    Returns ``(run_id, start cursor, plaid_item_id, history was complete)``.
    """
    now = clock.now()
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        if item is None:
            raise LookupError(f"no Plaid Item with id {item_id}")
        run = s.get(SyncRun, run_id) if run_id is not None else None
        if run is None or run.item_id != item_id or run.status != "running":
            run = SyncRun(item_id=item_id, trigger=trigger, status="running", started_at=now)
            s.add(run)
            s.flush()
        s.execute(
            update(SyncRun)
            .where(SyncRun.item_id == item_id, SyncRun.status == "running", SyncRun.id != run.id)
            .values(status="interrupted", finished_at=now)
            .execution_options(synchronize_session=False)
        )
        item.last_sync_attempt_at = now
        return (
            run.id,
            item.transactions_cursor,
            item.plaid_item_id,
            item.history_complete_at is not None,
        )


def prepare_run(session: Session, item_id: int, trigger: str) -> SyncRun:
    """A ``running`` sync run for a sync that is about to start in the background.

    The web app marks the run before answering, so the page says "Syncing…" at once; pass its id
    as ``run_id`` to :func:`sync_item` (or :class:`RefreshThenSync` / :class:`Backfill`), which
    reuses it. The caller commits. A run that never starts (the app stopped) is marked
    ``interrupted`` by the next sync of the Item.
    """
    if trigger not in SYNC_TRIGGERS:
        raise ValueError(f"unknown sync trigger {trigger!r}")
    run = SyncRun(item_id=item_id, trigger=trigger, status="running", started_at=clock.now())
    session.add(run)
    session.flush()
    return run


def _record_skipped(
    session_factory: sessionmaker[Session], item_id: int, trigger: str, run_id: int | None
) -> SyncOutcome:
    now = clock.now()
    with db.transaction(session_factory) as s:
        run = s.get(SyncRun, run_id) if run_id is not None else None
        if run is None or run.item_id != item_id:
            run = SyncRun(item_id=item_id, trigger=trigger, status="skipped", started_at=now)
            s.add(run)
        run.status = "skipped"
        run.finished_at = now
        run.error_message = "Another sync of this connection was running."
        s.flush()
        run_id = run.id
    log.info("Sync of Item %s skipped: another sync holds the lock", item_id)
    return SyncOutcome(
        item_id, trigger, "skipped", sync_run_id=run_id, started_at=now, finished_at=now
    )


def _finish_run(
    session_factory: sessionmaker[Session],
    run_id: int,
    *,
    status: str,
    added: int = 0,
    modified: int = 0,
    removed: int = 0,
    update_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dt.datetime:
    now = clock.now()
    with db.transaction(session_factory) as s:
        run = s.get(SyncRun, run_id)
        if run is not None:
            run.status = status
            run.finished_at = now
            run.added_count = added
            run.modified_count = modified
            run.removed_count = removed
            run.update_status = update_status
            run.error_code = error_code
            run.error_message = _clip(error_message)
    return now


def _record_item_error(
    session_factory: sessionmaker[Session],
    item_id: int,
    *,
    status: str,
    code: str | None,
    message: str | None,
    display: str | None,
) -> None:
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        if item is not None:
            item.status = status
            item.error_code = code
            item.error_message = _clip(message)
            item.error_display_message = _clip(display)


def _run_alerts(
    session_factory: sessionmaker[Session], notifier: Notifier, outcome: SyncOutcome
) -> None:
    """``alerts.evaluate_after_sync`` in its own transaction (notifications go out on commit)."""
    try:
        with db.transaction(session_factory) as s:
            alerts.evaluate_after_sync(s, notifier, outcome)
    except Exception:
        log.warning("Alerts after the sync of Item %s failed", outcome.item_id, exc_info=True)


def _item_status(session_factory: sessionmaker[Session], item_id: int) -> str | None:
    with session_factory() as s:
        return s.scalar(select(PlaidItem.status).where(PlaidItem.id == item_id))


# --------------------------------------------------------------------------- best-effort steps


def _side_error(
    session_factory: sessionmaker[Session], item_id: int, err: PlaidGatewayError, what: str
) -> None:
    """A best-effort call failed: a reconnect-type error marks the Item; others are only logged."""
    log.warning("%s for Item %s failed: %s %s", what, item_id, err.error_code, err.error_message)
    if err.needs_reconnect:
        _record_item_error(
            session_factory,
            item_id,
            status="needs_reconnect",
            code=err.error_code,
            message=err.error_message,
            display=err.display_message,
        )


def refresh_accounts(
    session_factory: sessionmaker[Session], gateway: PlaidGateway, token: str, item_id: int
) -> bool:
    """``/accounts/get`` → names, masks and balances of every account (SYNC-7)."""
    try:
        body = gateway.get_accounts(token)
    except PlaidGatewayError as err:
        _side_error(session_factory, item_id, err, "/accounts/get")
        return False
    now = clock.now()
    entries = list(body.get("accounts") or ())
    listed = {data["account_id"] for data in entries}
    with db.transaction(session_factory) as s:
        known = {
            a.plaid_account_id: a
            for a in s.scalars(select(Account).where(Account.item_id == item_id))
        }
        for data in entries:
            pid = data["account_id"]
            account = known.get(pid)
            if account is None:
                account = s.scalars(
                    select(Account).where(Account.plaid_account_id == pid)
                ).one_or_none()
            if account is None:
                account = churned_account(known, listed, data)
                if account is not None:  # re-issued with no new transactions to show it
                    known.pop(account.plaid_account_id, None)
                    reissue_account(account, pid, now)
                    log.warning("Account churn on Item %s: an account was re-issued", item_id)
            if account is None:
                account = Account(item_id=item_id, plaid_account_id=pid)
                s.add(account)
            known[pid] = account
            update_account(account, data, now)
    return True


def apply_item_snapshot(item: PlaidItem, body: Mapping[str, Any]) -> None:
    """Copy an ``/item/get`` answer onto the Item: consent expiry, consented products, Plaid's
    freshness timestamps, the Item error (if any) and the snapshot itself."""
    data = body.get("item") or {}
    item.item_snapshot = dict(body)
    if "consent_expiration_time" in data:
        item.consent_expires_at = parse_timestamp(data.get("consent_expiration_time"))
    products = data.get("consented_products")
    if isinstance(products, list):
        item.consented_products = sorted({str(p) for p in products})
    if data.get("institution_id") and not item.institution_id:
        item.institution_id = data["institution_id"]
    if data.get("institution_name") and not item.institution_name:
        item.institution_name = data["institution_name"]
    transactions = ((body.get("status") or {}).get("transactions")) or {}
    if "last_successful_update" in transactions:
        item.plaid_last_update_at = parse_timestamp(transactions.get("last_successful_update"))
    if "last_failed_update" in transactions:
        item.plaid_last_failed_update_at = parse_timestamp(transactions.get("last_failed_update"))
    error = data.get("error")
    if isinstance(error, Mapping) and error.get("error_code"):
        code = str(error["error_code"])
        from budgetapp.plaid_gateway import classify_error

        item.status = "needs_reconnect" if classify_error(code) == "needs_reconnect" else "error"
        item.error_code = code
        item.error_message = _clip(error.get("error_message"))
        item.error_display_message = _clip(error.get("display_message"))


def refresh_item(
    session_factory: sessionmaker[Session], gateway: PlaidGateway, token: str, item_id: int
) -> bool:
    """``/item/get`` → consent (SYNC-6), consented products, Item error, Plaid freshness."""
    try:
        body = gateway.get_item(token)
    except PlaidGatewayError as err:
        _side_error(session_factory, item_id, err, "/item/get")
        return False
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        if item is not None:
            apply_item_snapshot(item, body)
    return True


def snapshot_statement(
    session: Session, account: Account, issue_date: dt.date, balance: Decimal | None
) -> Statement:
    """Record a Liabilities statement snapshot (ARCHITECTURE §5 statements).

    Upsert on ``(account_id, plaid_issue_date)``; else attach to the row whose ``closing_date`` is
    within 7 days (rows without an issue date first, then the closest); else insert. A ``user``
    closing date or a ``user`` reported balance is never changed, nor anything typed from the PDF.
    """
    rows = list(session.scalars(select(Statement).where(Statement.account_id == account.id)))
    row = next((r for r in rows if r.plaid_issue_date == issue_date), None)
    if row is None:
        near = [r for r in rows if abs((r.closing_date - issue_date).days) <= 7]
        near.sort(
            key=lambda r: (r.plaid_issue_date is not None, abs((r.closing_date - issue_date).days))
        )
        row = near[0] if near else None
    if row is None:
        row = Statement(
            account_id=account.id,
            closing_date=issue_date,
            closing_date_source="liabilities",
            plaid_issue_date=issue_date,
        )
        session.add(row)
    else:
        row.plaid_issue_date = issue_date
        taken = any(r is not row and r.closing_date == issue_date for r in rows)
        if row.closing_date_source != "user" and row.closing_date != issue_date and not taken:
            row.closing_date = issue_date
            row.closing_date_source = "liabilities"
    if balance is not None and row.reported_new_balance_source != "user":
        row.reported_new_balance = balance
        row.reported_new_balance_source = "liabilities"
    session.flush()
    return row


def refresh_liabilities(
    session_factory: sessionmaker[Session], gateway: PlaidGateway, token: str, item_id: int
) -> str | None:
    """``/liabilities/get`` when consented → statement balance, due date, minimum payment and a
    statement snapshot per card (SYNC-7); records ``liabilities_status``. Returns the status."""
    with session_factory() as s:
        consented = s.scalar(select(PlaidItem.consented_products).where(PlaidItem.id == item_id))
    if consented is None:
        return None
    if "liabilities" not in consented:
        status = "not_consented"
        _set_liabilities_status(session_factory, item_id, status)
        return status
    try:
        body = gateway.get_liabilities(token)
    except PlaidGatewayError as err:
        status = _LIABILITIES_STATUS.get(err.error_code or "")
        if status is None:
            _side_error(session_factory, item_id, err, "/liabilities/get")
            if err.needs_reconnect:
                return None
            status = "error"
        else:
            log.info("Liabilities for Item %s: %s (%s)", item_id, status, err.error_code)
        _set_liabilities_status(session_factory, item_id, status)
        return status
    now = clock.now()
    with db.transaction(session_factory) as s:
        accounts = {
            a.plaid_account_id: a
            for a in s.scalars(select(Account).where(Account.item_id == item_id))
        }
        for entry in ((body.get("liabilities") or {}).get("credit")) or ():
            account = accounts.get(entry.get("account_id"))
            if account is None:
                continue
            account.statement_balance = to_money_or_none(entry.get("last_statement_balance"))
            account.minimum_payment = to_money_or_none(entry.get("minimum_payment_amount"))
            account.last_payment_amount = to_money_or_none(entry.get("last_payment_amount"))
            account.statement_issue_date = _date(entry.get("last_statement_issue_date"))
            account.payment_due_date = _date(entry.get("next_payment_due_date"))
            account.last_payment_date = _date(entry.get("last_payment_date"))
            overdue = entry.get("is_overdue")
            account.is_overdue = overdue if isinstance(overdue, bool) else None
            account.liabilities_as_of = now
            if account.statement_issue_date is not None:
                snapshot_statement(
                    s, account, account.statement_issue_date, account.statement_balance
                )
        item = s.get(PlaidItem, item_id)
        if item is not None:
            item.liabilities_status = "ok"
    return "ok"


def _set_liabilities_status(
    session_factory: sessionmaker[Session], item_id: int, status: str
) -> None:
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        if item is not None:
            item.liabilities_status = status


def _best_effort(what: str, item_id: int, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        log.warning("%s for Item %s failed", what, item_id, exc_info=True)


# --------------------------------------------------------------------------- sync_item


def _apply_batch(
    session_factory: sessionmaker[Session],
    item_id: int,
    start: str | None,
    batch: Batch,
) -> _Applied:
    """The ONE apply transaction (§6, §8): ledger lock first, cursor compare-and-set last."""
    now = clock.now()
    with db.transaction(session_factory) as s:
        db.ledger_lock(s)
        item = s.get(PlaidItem, item_id)
        if item is None:
            raise LookupError(f"no Plaid Item with id {item_id}")
        if item.transactions_cursor != start:
            raise _CursorMoved(item.transactions_cursor)
        applier = _Applier(s, item, batch, now)
        applier.apply_accounts()
        applier.preload()
        applier.apply_upserts()
        applier.apply_deletes()
        applier.apply_replacements()
        completed_now = applier.update_history(batch.update_status)
        if applier.inserted or completed_now:
            result = reconcile.reconcile_item(s, item.id)
            if result.paired or result.suspects_flagged:
                log.info(
                    "Reconciled Item %s: %d CSV rows paired, %d marked suspect",
                    item.id,
                    result.paired,
                    result.suspects_flagged,
                )
        s.execute(
            delete(TransactionTombstone)
            .where(TransactionTombstone.removed_at < now - TOMBSTONE_RETENTION)
            .execution_options(synchronize_session=False)
        )
        item.status = "healthy"
        item.error_code = None
        item.error_message = None
        item.error_display_message = None
        item.transactions_update_status = batch.update_status
        item.last_sync_success_at = now
        s.flush()
        moved = s.execute(
            update(PlaidItem)
            .where(
                PlaidItem.id == item_id,
                PlaidItem.transactions_cursor.is_not_distinct_from(start),
            )
            .values(transactions_cursor=batch.next_cursor)
            .execution_options(synchronize_session=False)
        ).rowcount
        if moved != 1:
            raise _CursorMoved(None)
        return _Applied(
            added=len(applier.inserted),
            modified=applier.modified,
            removed=applier.removed,
            new_charge_ids=tuple(t.id for t in applier.inserted if id(t) not in applier.continued),
            update_status=batch.update_status,
            history_completed_now=completed_now,
        )


def _record_not_ready(
    session_factory: sessionmaker[Session], item_id: int, update_status: str | None
) -> None:
    """NOT_READY is recorded as a success with no changes (§8), but no data has arrived: while the
    Item has no cursor yet its ``last_sync_success_at`` stays as it was, so ``--if-due`` keeps
    polling and the 36-hour stale check counts from linking."""
    now = clock.now()
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        if item is not None:
            item.status = "healthy"
            item.error_code = None
            item.error_message = None
            item.error_display_message = None
            item.transactions_update_status = update_status or NOT_READY
            if item.transactions_cursor is not None:
                item.last_sync_success_at = now


def sync_item(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    item_id: int,
    *,
    trigger: str,
    notifier: Notifier,
    run_id: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncOutcome:
    """Sync one Item (ARCHITECTURE §8); see the module docstring for the steps.

    ``run_id``: a ``running`` sync run created beforehand (the web app's "Sync now" marks the run
    before answering); it is reused instead of a new one. Returns the outcome for every Plaid
    failure; re-raises any other exception after recording it.
    """
    if trigger not in SYNC_TRIGGERS:
        raise ValueError(f"unknown sync trigger {trigger!r}")
    bind = session_factory.kw["bind"]
    ensure_database(settings, bind)
    with db.sync_lock(bind, item_id) as acquired:
        if not acquired:
            return _record_skipped(session_factory, item_id, trigger, run_id)
        return _sync_locked(
            session_factory,
            gateway,
            store,
            settings,
            item_id,
            trigger=trigger,
            notifier=notifier,
            run_id=run_id,
            sleep=sleep,
        )


def _sync_locked(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    item_id: int,
    *,
    trigger: str,
    notifier: Notifier,
    run_id: int | None,
    sleep: Callable[[float], None],
) -> SyncOutcome:
    started = clock.now()
    run_id, start, plaid_item_id, history_was_complete = _start_run(
        session_factory, item_id, trigger, run_id
    )
    base = {"sync_run_id": run_id, "started_at": started}
    try:
        token = access_token_for(settings, plaid_item_id, store)
        applied: _Applied | None = None
        update_status: str | None = None
        for attempt in range(MAX_CURSOR_RETRIES + 1):
            pages, ready = _fetch_pages(gateway, token, start, sleep=sleep)
            if not ready:
                update_status = (
                    pages[-1].get("transactions_update_status") if pages else None
                ) or NOT_READY
                _record_not_ready(session_factory, item_id, update_status)
                break
            try:
                applied = _apply_batch(session_factory, item_id, start, fold_pages(pages))
                update_status = applied.update_status
                break
            except _CursorMoved as moved:
                if attempt >= MAX_CURSOR_RETRIES:
                    raise RuntimeError("the stored cursor kept changing during the sync") from None
                with session_factory() as s:
                    start = s.scalar(
                        select(PlaidItem.transactions_cursor).where(PlaidItem.id == item_id)
                    )
                log.warning(
                    "Item %s: cursor changed during the sync (%s); refetching", item_id, moved
                )
        finished = _finish_run(
            session_factory,
            run_id,
            status="success",
            added=applied.added if applied else 0,
            modified=applied.modified if applied else 0,
            removed=applied.removed if applied else 0,
            update_status=update_status,
        )
    except PlaidGatewayError as err:
        return _plaid_failure(
            session_factory, notifier, item_id, trigger, run_id, started, err, history_was_complete
        )
    except MissingAccessToken as exc:
        outcome = _internal_failure(
            session_factory, item_id, trigger, run_id, started, ACCESS_TOKEN_MISSING, str(exc)
        )
        _run_alerts(session_factory, notifier, outcome)
        return outcome
    except secrets.KeychainApprovalNeeded as exc:
        _internal_failure(
            session_factory, item_id, trigger, run_id, started, KEYCHAIN_APPROVAL, str(exc)
        )
        raise
    except Exception as exc:
        outcome = _internal_failure(
            session_factory,
            item_id,
            trigger,
            run_id,
            started,
            INTERNAL,
            f"{type(exc).__name__}: {exc}",
        )
        _run_alerts(session_factory, notifier, outcome)
        raise

    _best_effort(
        "/accounts/get", item_id, lambda: refresh_accounts(session_factory, gateway, token, item_id)
    )
    _best_effort(
        "/item/get", item_id, lambda: refresh_item(session_factory, gateway, token, item_id)
    )
    _best_effort(
        "/liabilities/get",
        item_id,
        lambda: refresh_liabilities(session_factory, gateway, token, item_id),
    )
    outcome = SyncOutcome(
        item_id,
        trigger,
        "success" if applied is not None else "not_ready",
        added=applied.added if applied else 0,
        modified=applied.modified if applied else 0,
        removed=applied.removed if applied else 0,
        inserted_transaction_ids=applied.new_charge_ids if applied else (),
        initial_history_load=not history_was_complete,
        history_completed_now=applied.history_completed_now if applied else False,
        update_status=update_status,
        item_status=_item_status(session_factory, item_id),
        finished_at=finished,
        **base,
    )
    log.info(
        "Synced Item %s (%s): %d added, %d changed, %d removed, %s",
        item_id,
        trigger,
        outcome.added,
        outcome.modified,
        outcome.removed,
        update_status,
    )
    _run_alerts(session_factory, notifier, outcome)
    return outcome


def _plaid_failure(
    session_factory: sessionmaker[Session],
    notifier: Notifier,
    item_id: int,
    trigger: str,
    run_id: int,
    started: dt.datetime,
    err: PlaidGatewayError,
    history_was_complete: bool,
) -> SyncOutcome:
    """A Plaid error: nothing was applied; record the Item status, the run and alert."""
    status = item_status_for(err)
    log.warning("Sync of Item %s failed: %s %s", item_id, err.error_code, err.error_message)
    _record_item_error(
        session_factory,
        item_id,
        status=status,
        code=err.error_code,
        message=err.error_message,
        display=err.display_message,
    )
    finished = _finish_run(
        session_factory,
        run_id,
        status="error",
        error_code=err.error_code,
        error_message=err.error_message,
    )
    outcome = SyncOutcome(
        item_id,
        trigger,
        "error",
        sync_run_id=run_id,
        initial_history_load=not history_was_complete,
        item_status=status,
        error_type=err.error_type,
        error_code=err.error_code,
        error_message=_clip(err.error_message),
        display_message=_clip(err.display_message),
        started_at=started,
        finished_at=finished,
    )
    _run_alerts(session_factory, notifier, outcome)
    return outcome


def _internal_failure(
    session_factory: sessionmaker[Session],
    item_id: int,
    trigger: str,
    run_id: int,
    started: dt.datetime,
    code: str,
    message: str,
) -> SyncOutcome:
    """A failure that is not Plaid's: the Item shows ``error`` with ``code`` until a success."""
    log.error("Sync of Item %s failed (%s): %s", item_id, code, redact(message))
    finished = started
    try:
        _record_item_error(
            session_factory, item_id, status="error", code=code, message=message, display=None
        )
        finished = _finish_run(
            session_factory, run_id, status="error", error_code=code, error_message=message
        )
    except Exception:
        log.warning("Could not record the failed sync of Item %s", item_id, exc_info=True)
    return SyncOutcome(
        item_id,
        trigger,
        "error",
        sync_run_id=run_id,
        item_status="error",
        error_code=code,
        error_message=_clip(message),
        internal_error=True,
        started_at=started,
        finished_at=finished,
    )


# --------------------------------------------------------------------------- several Items


@dataclass(frozen=True, slots=True)
class SyncAllResult:
    """``outcomes`` of the Items synced; ``not_due``: Items skipped by ``if_due``."""

    outcomes: tuple[SyncOutcome, ...] = ()
    not_due: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return all(o.ok or o.status == "skipped" for o in self.outcomes)


def sync_all(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    *,
    trigger: str,
    notifier: Notifier,
    if_due: bool = False,
    min_age: dt.timedelta = DUE_AFTER,
    item_ids: Iterable[int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncAllResult:
    """Sync every Item (or ``item_ids``). ``if_due`` skips Items whose last success is newer
    than ``min_age`` (5 h: the launchd schedule). An unexpected exception in one Item does not
    stop the others; the first one is re-raised at the end."""
    wanted = set(item_ids) if item_ids is not None else None
    with session_factory() as s:
        rows = s.execute(
            select(PlaidItem.id, PlaidItem.last_sync_success_at).order_by(PlaidItem.id)
        ).all()
    if wanted is not None:
        missing = wanted - {row.id for row in rows}
        if missing:
            raise LookupError(f"no Plaid Item with id {', '.join(map(str, sorted(missing)))}")
    now = clock.now()
    outcomes: list[SyncOutcome] = []
    not_due: list[int] = []
    first_error: BaseException | None = None
    for row in rows:
        if wanted is not None and row.id not in wanted:
            continue
        if (
            if_due
            and row.last_sync_success_at is not None
            and now - row.last_sync_success_at < min_age
        ):
            not_due.append(row.id)
            continue
        try:
            outcomes.append(
                sync_item(
                    session_factory,
                    gateway,
                    store,
                    settings,
                    row.id,
                    trigger=trigger,
                    notifier=notifier,
                    sleep=sleep,
                )
            )
        except db.DatabaseUnavailable, db.SchemaMismatch, secrets.KeychainApprovalNeeded:
            raise
        except Exception as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
    return SyncAllResult(tuple(outcomes), tuple(not_due))


# --------------------------------------------------------------------------- multi-step syncs


def request_refresh(
    gateway: PlaidGateway, store: secrets.SecretStore, settings: Settings, plaid_item_id: str
) -> PlaidGatewayError | None:
    """``/transactions/refresh`` for "Sync now"; a failure (e.g. the 2-per-minute limit) is
    returned, never raised: the sync runs anyway and records any real problem."""
    try:
        token = access_token_for(settings, plaid_item_id, store)
    except MissingAccessToken, secrets.SecretStoreError:
        return None
    try:
        gateway.transactions_refresh(token)
    except PlaidGatewayError as err:
        if err.error_type == _RATE_LIMIT_TYPE:
            log.info(
                "Refresh for Item %s rate-limited (%s); syncing anyway",
                plaid_item_id,
                err.error_code,
            )
        else:
            log.warning("Refresh for Item %s failed: %s", plaid_item_id, err.error_code)
        return err
    return None


class RefreshThenSync:
    """ "Sync now" (SYNC-2): ``/transactions/refresh``, a sync now, then syncs after ~15 s, 45 s and
    2 min (Plaid's refresh is asynchronous and announces nothing without webhooks).

    Each call runs one step and returns the seconds until the next (``None`` when done); a failed
    sync ends the series (the error is recorded and shown).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: PlaidGateway,
        store: secrets.SecretStore,
        settings: Settings,
        item_id: int,
        *,
        notifier: Notifier,
        run_id: int | None = None,
        delays: Sequence[float] = FOLLOW_UP_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.args = (session_factory, gateway, store, settings, item_id)
        self.notifier = notifier
        self.run_id = run_id
        self.delays = tuple(delays)
        self.sleep = sleep
        self.step = 0
        self.refresh_error: PlaidGatewayError | None = None
        self.outcomes: list[SyncOutcome] = []

    def __call__(self) -> float | None:
        session_factory, gateway, store, settings, item_id = self.args
        if self.step == 0:
            with session_factory() as s:
                plaid_item_id = s.scalar(
                    select(PlaidItem.plaid_item_id).where(PlaidItem.id == item_id)
                )
            if plaid_item_id is not None:
                self.refresh_error = request_refresh(gateway, store, settings, plaid_item_id)
        outcome = sync_item(
            session_factory,
            gateway,
            store,
            settings,
            item_id,
            trigger="manual",
            notifier=self.notifier,
            run_id=self.run_id if self.step == 0 else None,
            sleep=self.sleep,
        )
        self.outcomes.append(outcome)
        if outcome.status == "error" or self.step >= len(self.delays):
            return None
        delay = self.delays[self.step]
        self.step += 1
        return delay


def refresh_then_sync(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    item_id: int,
    *,
    notifier: Notifier,
    run_id: int | None = None,
    delays: Sequence[float] = FOLLOW_UP_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SyncOutcome]:
    """:class:`RefreshThenSync` run to the end in this thread (commands)."""
    task = RefreshThenSync(
        session_factory,
        gateway,
        store,
        settings,
        item_id,
        notifier=notifier,
        run_id=run_id,
        delays=delays,
        sleep=sleep,
    )
    run_steps(task, sleep)
    return task.outcomes


class Backfill:
    """After linking (or update mode): re-sync with backoff until HISTORICAL_UPDATE_COMPLETE, for
    about an hour at most (RESEARCH T1.2: webhooks would announce it; this app polls).

    Each call runs one sync and returns the seconds until the next (``None`` when done). It stops
    once history is complete (after at least ``min_runs`` syncs), at the cap, or when the Item
    needs reconnecting or the app failed (the next scheduled sync takes over). ``run_id``: a
    ``running`` run prepared for the first sync (:func:`prepare_run`).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: PlaidGateway,
        store: secrets.SecretStore,
        settings: Settings,
        item_id: int,
        *,
        notifier: Notifier,
        trigger: str = "link",
        delays: Sequence[float] = BACKFILL_DELAYS,
        cap: float = BACKFILL_CAP,
        min_runs: int = 1,
        run_id: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.args = (session_factory, gateway, store, settings, item_id)
        self.notifier = notifier
        self.trigger = trigger
        self.delays = tuple(delays) or (BACKFILL_CAP,)
        self.cap = cap
        self.min_runs = min_runs
        self.run_id = run_id
        self.monotonic = monotonic
        self.sleep = sleep
        self.started: float | None = None
        self.outcomes: list[SyncOutcome] = []

    @property
    def complete(self) -> bool:
        return any(o.ok and o.update_status == HISTORICAL_UPDATE_COMPLETE for o in self.outcomes)

    def __call__(self) -> float | None:
        if self.started is None:
            self.started = self.monotonic()
        session_factory, gateway, store, settings, item_id = self.args
        outcome = sync_item(
            session_factory,
            gateway,
            store,
            settings,
            item_id,
            trigger=self.trigger,
            notifier=self.notifier,
            run_id=self.run_id if not self.outcomes else None,
            sleep=self.sleep,
        )
        self.outcomes.append(outcome)
        if outcome.status == "error" and (outcome.needs_reconnect or outcome.internal_error):
            return None
        if self.complete and len(self.outcomes) >= self.min_runs:
            return None
        delay = self.delays[min(len(self.outcomes) - 1, len(self.delays) - 1)]
        if self.monotonic() - self.started + delay > self.cap:
            log.info("Backfill of Item %s stopped after %d syncs", item_id, len(self.outcomes))
            return None
        return delay


def backfill_until_complete(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    item_id: int,
    *,
    notifier: Notifier,
    trigger: str = "link",
    delays: Sequence[float] = BACKFILL_DELAYS,
    cap: float = BACKFILL_CAP,
    min_runs: int = 1,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[SyncOutcome]:
    """:class:`Backfill` run to the end in this thread (commands)."""
    task = Backfill(
        session_factory,
        gateway,
        store,
        settings,
        item_id,
        notifier=notifier,
        trigger=trigger,
        delays=delays,
        cap=cap,
        min_runs=min_runs,
        monotonic=monotonic,
        sleep=sleep,
    )
    run_steps(task, sleep)
    return task.outcomes
