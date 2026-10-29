"""Linking the Amex card through Plaid: Link tokens, the token exchange, update mode, re-attaching
(ARCHITECTURE §3, §7 "Linking safety"; SYNC-1, SYNC-3, SYNC-5, SYNC-6).

Principle 4: each real link permanently uses one of the Trial plan's 10 connections, and a second
Amex link can also break the first. So in production:

* :func:`new_link_token` issues a new-link token only when there is no ``plaid_items`` row, no
  production access token in Keychain, no unexpired link-in-progress marker and no unsaved token
  in a recovery file (``secrets.production_link_blockers``) — checked, and the marker set, under
  ``db.link_lock``.
* :func:`exchange` never exchanges a public token while an Item exists (a duplicate would use a
  connection); it retries transport failures within the public token's 30-minute life; it saves
  the access token in Keychain (write-once, read back) BEFORE writing the database; a failed
  Keychain write leaves the token in a 0600 recovery file and stops with instructions.
* Repairs and consent renewals use update mode (:func:`update_link_token`,
  :func:`complete_update`): same Item, same token, no connection used.
* A token in Keychain without an Item row (lost or restored database) is re-attached
  (:func:`recover_orphaned_items`), never linked again.

Tokens are never logged, returned in messages or stored outside Keychain (SEC-2). Functions that
need several transactions take a ``sessionmaker`` (like ``services.sync``).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import clock, db, secrets
from budgetapp.config import Settings
from budgetapp.logsetup import register_secret
from budgetapp.models import (
    Account,
    Category,
    PlaidItem,
    SyncRun,
    Transaction,
    TransactionTombstone,
    get_app_settings,
)
from budgetapp.plaid_gateway import (
    ExchangeResult,
    PlaidError,
    PlaidGateway,
    PlaidGatewayError,
    PlaidTransportError,
)
from budgetapp.services import reconcile, userstate
from budgetapp.services import status as status_service
from budgetapp.services import sync as sync_service
from budgetapp.services.status import ConnectionStatus, ItemStatus

log = logging.getLogger(__name__)

#: The public token lives 30 minutes; retries stop a little before that.
EXCHANGE_WINDOW = 25 * 60.0
DEFAULT_SANDBOX_INSTITUTION = "ins_109508"
SANDBOX_INSTITUTIONS = {
    "ins_109508": "First Platypus Bank",
    "ins_127287": "Platypus OAuth Bank",
    "ins_129644": "First Platypus Bank - OAuth",
    "ins_10": "American Express",
}
RECENT_RUNS = 20
LOST_EDITS_LIMIT = 50

#: The production pre-flight (RESEARCH T2.8): done by hand in Plaid's Dashboard before the link.
CHECKLIST: tuple[tuple[str, str, str, str], ...] = (
    (
        "use_case",
        "A Data Transparency use case is selected",
        "Dashboard → Link → Customization → Data Transparency Messaging. In production Link "
        "doesn't open without one.",
        "https://dashboard.plaid.com/",
    ),
    (
        "profiles",
        "The App and Company profiles are complete",
        "Dashboard → Compliance Center: fill in the App Profile and Company Profile tabs.",
        "https://dashboard.plaid.com/",
    ),
    (
        "amex_oauth",
        "American Express shows as enabled for OAuth",
        "Dashboard → US OAuth institutions: American Express must say enabled.",
        "https://dashboard.plaid.com/activity/status/oauth-institutions",
    ),
)


class LinkError(Exception):
    """A link step was refused or failed; the message is written for the user (no secrets)."""


class LinkRefused(LinkError):
    """Refused before anything at Plaid happened (no connection was used)."""


class LinkFailed(LinkError):
    """A link step failed; the message says what to do."""


# --------------------------------------------------------------------------- Link metadata


@dataclass(frozen=True, slots=True)
class LinkAccount:
    id: str
    name: str | None = None
    mask: str | None = None
    type: str | None = None
    subtype: str | None = None


@dataclass(frozen=True, slots=True)
class LinkMetadata:
    """The parts of Link's ``onSuccess`` metadata the app uses (nothing else is kept)."""

    institution_id: str | None = None
    institution_name: str | None = None
    accounts: tuple[LinkAccount, ...] = ()
    link_session_id: str | None = None

    @classmethod
    def from_json(cls, data: Any) -> LinkMetadata:
        """Parse untrusted JSON from the browser; anything malformed is simply dropped."""
        if not isinstance(data, Mapping):
            return cls()

        def text(value: Any) -> str | None:
            return value.strip()[:200] or None if isinstance(value, str) else None

        institution = data.get("institution")
        institution = institution if isinstance(institution, Mapping) else {}
        accounts = []
        for entry in data.get("accounts") or ():
            if isinstance(entry, Mapping) and text(entry.get("id")):
                accounts.append(
                    LinkAccount(
                        id=text(entry.get("id")) or "",
                        name=text(entry.get("name")),
                        mask=text(entry.get("mask")),
                        type=text(entry.get("type")),
                        subtype=text(entry.get("subtype")),
                    )
                )
        return cls(
            institution_id=text(institution.get("institution_id")),
            institution_name=text(institution.get("name")),
            accounts=tuple(accounts[:20]),
            link_session_id=text(data.get("link_session_id")),
        )


@dataclass(frozen=True, slots=True)
class LinkResult:
    """A saved Item. ``created`` is False when the row already existed (a repeated request)."""

    item_id: int
    plaid_item_id: str
    institution_name: str | None
    account_count: int
    created: bool = True
    attached: reconcile.AttachResult | None = None
    #: manual CSV accounts exist but which one belongs to which card must be chosen by hand
    attach_needed: bool = False


@dataclass(frozen=True, slots=True)
class UpdateResult:
    item_id: int
    status: str
    consent_expires_at: dt.datetime | None
    liabilities_consented: bool


# --------------------------------------------------------------------------- new links


def _item_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(PlaidItem)) or 0


def new_link_token(
    session: Session,
    gateway: PlaidGateway,
    settings: Settings,
    *,
    store: secrets.SecretStore | None = None,
) -> str:
    """A Link token for a NEW Item (``products=['transactions']``, 730 days, Liabilities as an
    additional consented product; the gateway builds the exact request).

    Production: refused (:class:`LinkRefused`) unless the link guard passes, checked under the
    link lock, which also covers setting the link-in-progress marker. The caller commits (that
    releases the lock). Sandbox may hold several Items.
    """
    if not settings.is_production:
        return gateway.create_link_token()
    db.link_lock(session)
    if _item_count(session):
        raise LinkRefused(
            "American Express is already linked. Use Reconnect or Renew instead: a second link "
            "would use another of your 10 Plaid connections and could break the first."
        )
    blockers = secrets.production_link_blockers(settings, store=store)
    if blockers:
        raise LinkRefused(" ".join(blockers))
    secrets.set_link_in_progress(store=store)
    try:
        return gateway.create_link_token()
    except BaseException:
        secrets.clear_link_in_progress(store=store)
        raise


def link_exited(
    settings: Settings,
    *,
    store: secrets.SecretStore | None = None,
    mode: str = "new",
    error_code: str | None = None,
    event: str | None = None,
    status: str | None = None,
    link_session_id: str | None = None,
) -> None:
    """Link closed without linking (``onExit``, or the ``CLOSE_OAUTH`` / ``FAIL_OAUTH`` events):
    logged as "not linked"; in production a new-link attempt's marker is cleared."""
    log.info(
        "Plaid Link closed without linking (%s mode; event %s, error %s, status %s, session %s)",
        mode,
        event or "EXIT",
        error_code or "none",
        status or "unknown",
        link_session_id or "unknown",
    )
    if settings.is_production and mode == "new":
        secrets.clear_link_in_progress(store=store)


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    key: str
    label: str
    detail: str
    href: str


@dataclass(frozen=True, slots=True)
class InstitutionCheck:
    """``/institutions/search`` for American Express with Liabilities (best effort)."""

    status: str  # ok | missing | error
    message: str
    institution_id: str | None = None
    oauth: bool | None = None


@dataclass(frozen=True, slots=True)
class Preflight:
    checklist: tuple[ChecklistItem, ...]
    institution: InstitutionCheck


def checklist() -> tuple[ChecklistItem, ...]:
    return tuple(ChecklistItem(*row) for row in CHECKLIST)


def preflight(gateway: PlaidGateway, settings: Settings) -> Preflight:
    """The production pre-flight (ARCHITECTURE §7): the manual Dashboard checklist plus the live
    ``search_institutions('American Express', ['liabilities'])`` result. A failed search is
    shown, never fatal (it creates no Item and uses no connection)."""
    try:
        body = gateway.search_institutions("American Express", ["liabilities"])
    except PlaidGatewayError as err:
        message = err.display_message or err.error_message or "Plaid couldn't be reached."
        check = InstitutionCheck(
            "error", f"Couldn't check with Plaid just now ({message}). This check is optional."
        )
        return Preflight(checklist(), check)
    found = [
        inst
        for inst in body.get("institutions") or ()
        if isinstance(inst, Mapping)
        and (inst.get("institution_id") == "ins_10" or inst.get("name") == "American Express")
    ]
    if found:
        inst = found[0]
        oauth = inst.get("oauth") if isinstance(inst.get("oauth"), bool) else None
        message = "Plaid lists American Express with statement details (Liabilities)"
        message += ", signing in through Amex's own pop-up (OAuth)." if oauth else "."
        check = InstitutionCheck("ok", message, inst.get("institution_id"), oauth)
    else:
        check = InstitutionCheck(
            "missing",
            "Plaid's search didn't list American Express with statement details (Liabilities). "
            "Transactions can still be linked; balances due and statement snapshots may be "
            "missing.",
        )
    return Preflight(checklist(), check)


def _exchange_message(err: PlaidError) -> str:
    if err.is_trial_limit:
        return (
            "Plaid refused to create the connection: your Trial plan has used all 10 connections. "
            "Nothing was linked."
        )
    detail = err.display_message or err.error_message or err.error_code or "unknown error"
    return f"Plaid couldn't finish linking ({detail}). Nothing was saved."


def _exchange_with_retries(
    gateway: PlaidGateway,
    public_token: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    window: float,
) -> tuple[ExchangeResult | None, PlaidError | None, bool]:
    """``(result, plaid_error, retried)``: transport failures are retried with backoff while the
    public token is still valid; a Plaid error is returned (never retried: a 429 here can be the
    Trial connection limit)."""
    deadline = monotonic() + window
    delay = 2.0
    retried = False
    while True:
        try:
            return gateway.exchange_public_token(public_token), None, retried
        except PlaidError as err:
            return None, err, retried
        except PlaidTransportError as err:
            if monotonic() + delay > deadline:
                raise LinkFailed(
                    "Plaid couldn't be reached to finish linking, and the link has expired. If "
                    "Plaid did create the connection, it shows on the Connection page once you "
                    "re-attach it; otherwise link again later."
                ) from err
            log.warning("Exchange failed (%s); retrying in %.0f s", err.error_message, delay)
            retried = True
            sleep(delay)
            delay = min(delay * 2, 60.0)


def _details(gateway: PlaidGateway, token: str) -> tuple[dict | None, dict | None]:
    """``/item/get`` and ``/accounts/get`` right after linking (best effort, one retry each)."""
    results: list[dict | None] = []
    for call in (gateway.get_item, gateway.get_accounts):
        body = None
        for _attempt in range(2):
            try:
                body = call(token)
                break
            except PlaidTransportError:
                continue
            except PlaidGatewayError as err:
                log.warning("After linking, %s failed: %s", call.__name__, err.error_code)
                break
        results.append(body)
    return results[0], results[1]


def save_item(
    session_factory: sessionmaker[Session],
    plaid_item_id: str,
    metadata: LinkMetadata,
    item_body: Mapping[str, Any] | None,
    accounts_body: Mapping[str, Any] | None,
) -> tuple[int, bool, int]:
    """Create (or complete) the Item and its accounts in ONE transaction.

    Returns ``(item id, created, account count)``. Accounts come from ``/accounts/get``, else from
    Link's metadata; the first sync completes them either way.
    """
    now = clock.now()
    with db.transaction(session_factory) as s:
        item = s.scalars(
            select(PlaidItem).where(PlaidItem.plaid_item_id == plaid_item_id)
        ).one_or_none()
        created = item is None
        if item is None:
            item = PlaidItem(
                plaid_item_id=plaid_item_id,
                institution_id=metadata.institution_id,
                institution_name=metadata.institution_name,
                status="healthy",
                linked_at=now,
                last_consented_at=now,
            )
            s.add(item)
            s.flush()
        if item_body:
            sync_service.apply_item_snapshot(item, item_body)
        if accounts_body and accounts_body.get("accounts"):
            accounts = list(accounts_body["accounts"])
        else:
            accounts = [
                {
                    "account_id": a.id,
                    "name": a.name,
                    "mask": a.mask,
                    "type": a.type,
                    "subtype": a.subtype,
                }
                for a in metadata.accounts
            ]
        for data in accounts:
            account = s.scalars(
                select(Account).where(Account.plaid_account_id == data["account_id"])
            ).one_or_none()
            if account is None:
                account = Account(item_id=item.id, plaid_account_id=data["account_id"])
                s.add(account)
            sync_service.update_account(account, data, now)
        s.flush()
        count = s.scalar(
            select(func.count()).select_from(Account).where(Account.item_id == item.id)
        )
        return item.id, created, count or 0


def exchange(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    public_token: str,
    metadata: LinkMetadata | Mapping[str, Any] | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    window: float = EXCHANGE_WINDOW,
) -> LinkResult:
    """Turn Link's public token into a saved Item (ARCHITECTURE §7 "Exchange"); one code path.

    Production: refused (nothing exchanged, no connection used) while any Item exists — in
    particular one at the same institution. The access token goes to Keychain (write-once, read
    back) BEFORE the database; then the Item, its accounts and ``/item/get`` details are saved in
    one transaction, and a lone manual CSV account is attached to a lone linked card. The caller
    starts the post-link backfill (``sync.Backfill``).
    """
    if not public_token or not isinstance(public_token, str):
        raise LinkRefused("Plaid Link didn't send a public token. Nothing was linked.")
    register_secret(public_token)
    meta = metadata if isinstance(metadata, LinkMetadata) else LinkMetadata.from_json(metadata)
    if settings.is_production:
        with db.transaction(session_factory) as s:
            db.link_lock(s)
            existing = list(s.scalars(select(PlaidItem)))
        if existing:
            same = any(
                meta.institution_id and item.institution_id == meta.institution_id
                for item in existing
            )
            secrets.clear_link_in_progress(store=store)
            raise LinkRefused(
                (
                    "This institution is already linked. "
                    if same
                    else "A connection already exists. "
                )
                + "The new link was not completed, so no Plaid connection was used. Use Reconnect "
                "on the existing connection instead."
            )
    result, error, retried = _exchange_with_retries(
        gateway, public_token, sleep=sleep, monotonic=monotonic, window=window
    )
    if error is not None:
        log.warning("Exchange refused by Plaid: %s", error.error_code)
        if settings.is_production and not retried:
            secrets.clear_link_in_progress(store=store)  # a definite answer: nothing was created
        raise LinkFailed(_exchange_message(error)) from error
    assert result is not None
    try:
        secrets.store_access_token(settings, result.item_id, result.access_token, store=store)
    except secrets.TokenNotSavedError as exc:
        log.error("The access token for Item %s could not be saved to Keychain", result.item_id)
        raise LinkFailed(
            f"The connection was created but its access token couldn't be saved to Keychain. "
            f"{exc} Don't link again: run ./run setup --env {settings.plaid_env} --access-token, "
            "then re-attach the connection here."
        ) from exc
    except secrets.AccessTokenConflict as exc:
        raise LinkFailed(
            f"A different access token is already saved in Keychain, so the new one wasn't "
            f"stored there. {exc} Re-attach the existing connection instead of linking again."
        ) from exc
    if settings.is_production:
        secrets.clear_link_in_progress(store=store)
    item_body, accounts_body = _details(gateway, result.access_token)
    item_id, created, count = save_item(
        session_factory, result.item_id, meta, item_body, accounts_body
    )
    attached, attach_needed = attach_after_link(session_factory)
    with session_factory() as s:
        name = s.scalar(select(PlaidItem.institution_name).where(PlaidItem.id == item_id))
    log.info("Linked Item %s (%s) with %d accounts", item_id, name or "unknown institution", count)
    return LinkResult(item_id, result.item_id, name, count, created, attached, attach_needed)


def sandbox_create_item(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    *,
    institution_id: str = DEFAULT_SANDBOX_INSTITUTION,
    username: str | None = None,
    password: str | None = None,
    initial_products: Sequence[str] = ("transactions", "liabilities"),
    sleep: Callable[[float], None] = time.sleep,
) -> LinkResult:
    """Sandbox only: a test Item without Plaid Link (``/sandbox/public_token/create``, 730 days),
    saved through the same :func:`exchange` path as a real link."""
    if settings.is_production or settings.plaid_env != "sandbox":
        raise LinkRefused("Test Items exist only in the Sandbox.")
    public_token = gateway.sandbox_create_public_token(
        institution_id,
        override_username=username,
        override_password=password,
        initial_products=tuple(initial_products),
    )
    meta = LinkMetadata(institution_id, SANDBOX_INSTITUTIONS.get(institution_id))
    return exchange(session_factory, gateway, store, settings, public_token, meta, sleep=sleep)


# --------------------------------------------------------------------------- update mode


def update_link_token(
    session: Session,
    gateway: PlaidGateway,
    item: PlaidItem | int,
    *,
    add_liabilities: bool = False,
    store: secrets.SecretStore | None = None,
    settings: Settings,
) -> str:
    """An update-mode Link token (Reconnect, Renew consent, or ``add_liabilities`` to ask for
    statement details): the Item's own access token, no products — the same Item and token
    afterwards, so no Plaid connection is used."""
    row = item if isinstance(item, PlaidItem) else session.get(PlaidItem, item)
    if row is None:
        raise LinkRefused("That connection no longer exists.")
    token = sync_service.access_token_for(settings, row.plaid_item_id, store or secrets.get_store())
    products = ("liabilities",) if add_liabilities else ()
    return gateway.create_link_token(access_token=token, additional_consented_products=products)


def complete_update(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
    item_id: int,
    *,
    metadata: LinkMetadata | Mapping[str, Any] | None = None,
) -> UpdateResult:
    """After update mode's ``onSuccess`` (the public token is ignored, never exchanged): the Item
    is healthy again, ``last_consented_at`` is now, and ``/item/get`` refreshes consent and
    consented products. The caller schedules the post-update syncs (``sync.Backfill`` with
    ``trigger="update"``)."""
    meta = metadata if isinstance(metadata, LinkMetadata) else LinkMetadata.from_json(metadata)
    with session_factory() as s:
        row = s.get(PlaidItem, item_id)
        if row is None:
            raise LinkRefused("That connection no longer exists.")
        plaid_item_id = row.plaid_item_id
    token = sync_service.access_token_for(settings, plaid_item_id, store)
    body = None
    try:
        body = gateway.get_item(token)
    except PlaidGatewayError as err:
        log.warning("After update mode, /item/get failed: %s", err.error_code)
    now = clock.now()
    with db.transaction(session_factory) as s:
        item = s.get(PlaidItem, item_id)
        assert item is not None
        item.status = "healthy"
        item.error_code = None
        item.error_message = None
        item.error_display_message = None
        item.last_consented_at = now
        if body is not None:
            sync_service.apply_item_snapshot(item, body)
        consented = "liabilities" in (item.consented_products or ())
        if consented and item.liabilities_status == "not_consented":
            item.liabilities_status = None  # the next sync asks Plaid again
        log.info(
            "Update mode finished for Item %s (session %s)",
            item_id,
            meta.link_session_id or "unknown",
        )
        return UpdateResult(item.id, item.status, item.consent_expires_at, consented)


# --------------------------------------------------------------------------- re-attach


def orphaned_sandbox_items(
    session: Session, *, store: secrets.SecretStore | None = None
) -> list[str]:
    """Sandbox Item ids that have a token in Keychain but no database row (a crash between the
    Keychain write and the commit, or a recreated database)."""
    known = set(session.scalars(select(PlaidItem.plaid_item_id)))
    orphans = []
    for item_id in secrets.sandbox_item_ids(store=store):
        if item_id not in known and secrets.get_access_token("sandbox", item_id, store=store):
            orphans.append(item_id)
    return orphans


def reattach_available(
    session: Session, settings: Settings, *, store: secrets.SecretStore | None = None
) -> bool:
    """Production: Keychain holds THE access token but the database has no Item."""
    if not settings.is_production or _item_count(session):
        return False
    return bool(secrets.get_access_token("production", store=store))


def recover_orphaned_items(
    session_factory: sessionmaker[Session],
    gateway: PlaidGateway,
    store: secrets.SecretStore,
    settings: Settings,
) -> list[LinkResult]:
    """Re-create the database rows of connections whose token is in Keychain but whose Item row is
    missing: rebuilt from ``/item/get`` and ``/accounts/get``; the caller then syncs from a null
    cursor (which reconciles). Production re-attaches THE token, and only while no Item exists.
    Never a new link."""
    if settings.is_production:
        with session_factory() as s:
            if _item_count(s):
                return []
        token = secrets.get_access_token("production", store=store)
        candidates = [(None, token)] if token else []
    else:
        with session_factory() as s:
            orphans = orphaned_sandbox_items(s, store=store)
        candidates = [(i, secrets.get_access_token("sandbox", i, store=store)) for i in orphans]
    results = []
    for expected_id, token in candidates:
        if not token:
            continue
        try:
            item_body = gateway.get_item(token)
        except PlaidGatewayError as err:
            raise LinkFailed(
                f"Plaid didn't recognise the saved connection ({err.error_code}). Nothing was "
                "changed."
            ) from err
        data = item_body.get("item") or {}
        plaid_item_id = data.get("item_id") or expected_id
        if not plaid_item_id or (expected_id and plaid_item_id != expected_id):
            raise LinkFailed("Plaid's answer didn't match the saved connection; nothing changed.")
        try:
            accounts_body = gateway.get_accounts(token)
        except PlaidGatewayError as err:
            log.warning("/accounts/get failed while re-attaching: %s", err.error_code)
            accounts_body = None
        meta = LinkMetadata(data.get("institution_id"), data.get("institution_name"))
        item_id, created, count = save_item(
            session_factory, plaid_item_id, meta, item_body, accounts_body
        )
        attached, attach_needed = attach_after_link(session_factory)
        log.info("Re-attached Item %s", item_id)
        results.append(
            LinkResult(
                item_id,
                plaid_item_id,
                meta.institution_name,
                count,
                created,
                attached,
                attach_needed,
            )
        )
    return results


# --------------------------------------------------------------------------- manual CSV accounts


@dataclass(frozen=True, slots=True)
class AccountOption:
    id: int
    label: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttachChoice:
    """Manual (CSV-only) accounts and the linked credit cards their history could move to."""

    manual: tuple[AccountOption, ...]
    linked: tuple[AccountOption, ...]

    @property
    def automatic(self) -> bool:
        return len(self.manual) == 1 and len(self.linked) == 1


def attach_choice(session: Session) -> AttachChoice | None:
    """``None`` unless manual accounts AND linked credit accounts both exist."""
    manual = list(
        session.scalars(select(Account).where(Account.item_id.is_(None)).order_by(Account.id))
    )
    if not manual:
        return None
    linked = list(
        session.scalars(
            select(Account)
            .where(Account.item_id.is_not(None), Account.type == "credit")
            .order_by(Account.id)
        )
    )
    if not linked:
        return None
    counts = dict(
        session.execute(
            select(Transaction.account_id, func.count())
            .where(Transaction.account_id.in_([a.id for a in manual]))
            .group_by(Transaction.account_id)
        ).all()
    )

    def option(account: Account, detail: str | None = None) -> AccountOption:
        return AccountOption(account.id, account.display_name, detail)

    def imported(account: Account) -> str:
        n = counts.get(account.id, 0)
        return f"{n:,} imported transaction{'' if n == 1 else 's'}"

    return AttachChoice(
        tuple(option(a, imported(a)) for a in manual), tuple(option(a) for a in linked)
    )


def attach_after_link(
    session_factory: sessionmaker[Session],
) -> tuple[reconcile.AttachResult | None, bool]:
    """After linking: attach the lone manual account to the lone linked card (§6), else report
    that the Connection page must ask. Never fails the link itself."""
    try:
        with db.transaction(session_factory) as s:
            choice = attach_choice(s)
            if choice is None:
                return None, False
            if not choice.automatic:
                return None, True
            db.ledger_lock(s)
            result = reconcile.attach_manual_account(s, choice.manual[0].id, choice.linked[0].id)
            log.info("Attached the CSV history to the linked card")
            return result, False
    except NotImplementedError:
        log.info("Attaching CSV history isn't available yet; the Connection page offers it")
        return None, True
    except Exception:
        log.warning("Attaching the CSV history after linking failed", exc_info=True)
        return None, True


def attach_manual(session: Session, manual_id: int, account_id: int) -> reconcile.AttachResult:
    """Move a manual CSV account's history onto a linked card (the caller commits)."""
    manual = session.get(Account, manual_id)
    linked = session.get(Account, account_id)
    if manual is None or not manual.is_manual:
        raise LinkRefused("That imported account no longer exists.")
    if linked is None or linked.is_manual:
        raise LinkRefused("Choose one of the linked cards.")
    db.ledger_lock(session)
    return reconcile.attach_manual_account(session, manual.id, linked.id)


# --------------------------------------------------------------------------- edits not carried


@dataclass(frozen=True, slots=True)
class LostEdit:
    """A removed Plaid transaction whose edits found no replacement (yet)."""

    plaid_transaction_id: str
    account_name: str | None
    name: str | None
    merchant_name: str | None
    amount: Decimal
    date: dt.date
    pending: bool
    removed_at: dt.datetime
    category_name: str | None
    notes: str | None
    tags: tuple[str, ...]
    split_count: int
    expires_on: dt.date


def lost_edits(session: Session, *, limit: int = LOST_EDITS_LIMIT) -> list[LostEdit]:
    """Unconsumed tombstones with user edits, newest first ("edits we couldn't carry over")."""
    rows = session.execute(
        select(TransactionTombstone, Account)
        .join(Account, Account.id == TransactionTombstone.account_id)
        .where(
            TransactionTombstone.consumed_by_transaction_id.is_(None),
            sync_service.has_user_state(),
        )
        .order_by(TransactionTombstone.removed_at.desc(), TransactionTombstone.plaid_transaction_id)
        .limit(limit)
    ).all()
    states = {t.plaid_transaction_id: userstate.from_json(t.user_state) for t, _ in rows}
    category_ids = {s.category_id for s in states.values() if s and s.category_id}
    names = (
        dict(
            session.execute(
                select(Category.id, Category.name).where(Category.id.in_(category_ids))
            ).all()
        )
        if category_ids
        else {}
    )
    result = []
    for tomb, account in rows:
        state = states.get(tomb.plaid_transaction_id)
        if state is None:
            continue
        result.append(
            LostEdit(
                plaid_transaction_id=tomb.plaid_transaction_id,
                account_name=account.display_name,
                name=tomb.name,
                merchant_name=tomb.merchant_name,
                amount=tomb.amount,
                date=tomb.authorized_date or tomb.date,
                pending=tomb.pending,
                removed_at=tomb.removed_at,
                category_name=names.get(state.category_id) if state.category_id else None,
                notes=state.notes,
                tags=state.tags,
                split_count=len(state.splits),
                expires_on=(tomb.removed_at + sync_service.TOMBSTONE_RETENTION).astimezone().date(),
            )
        )
    return result


def dismiss_lost_edit(session: Session, plaid_transaction_id: str) -> bool:
    """Forget the edits of an unconsumed tombstone (it keeps blocking the removed id). The caller
    commits. False when there was nothing to dismiss."""
    db.ledger_lock(session)
    tomb = session.get(TransactionTombstone, plaid_transaction_id)
    if (
        tomb is None
        or tomb.consumed_by_transaction_id is not None
        or userstate.from_json(tomb.user_state) is None
    ):
        return False
    tomb.user_state = None
    return True


# --------------------------------------------------------------------------- the Connection page


@dataclass(frozen=True, slots=True)
class AccountView:
    id: int
    display_name: str
    official_name: str | None
    type: str | None
    subtype: str | None
    iso_currency_code: str | None
    current_balance: Decimal | None
    available_balance: Decimal | None
    credit_limit: Decimal | None
    balances_as_of: dt.datetime | None
    statement_balance: Decimal | None
    statement_issue_date: dt.date | None
    payment_due_date: dt.date | None
    minimum_payment: Decimal | None
    last_payment_amount: Decimal | None
    last_payment_date: dt.date | None
    is_overdue: bool | None
    liabilities_as_of: dt.datetime | None
    notice: str | None
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class ItemView:
    """One linked connection as the Connection page shows it."""

    id: int
    plaid_item_id: str
    institution_name: str | None
    status: ItemStatus
    error_code: str | None
    error_message: str | None
    error_display_message: str | None
    error_help: str | None
    transactions_update_status: str | None
    history_complete_at: dt.datetime | None
    plaid_last_failed_update_at: dt.datetime | None
    linked_at: dt.datetime
    last_consented_at: dt.datetime
    liabilities_status: str | None
    liabilities_consented: bool
    accounts: tuple[AccountView, ...]

    @property
    def needs_reconnect(self) -> bool:
        return self.status.item_status == "needs_reconnect"

    @property
    def consent_expired(self) -> bool:
        return self.status.consent_days_left < 0

    @property
    def name(self) -> str:
        return self.institution_name or "Plaid connection"


@dataclass(frozen=True, slots=True)
class RunView:
    id: int
    item_id: int
    institution_name: str | None
    trigger: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    added: int
    modified: int
    removed: int
    update_status: str | None
    error_code: str | None
    error_message: str | None

    @property
    def seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class ConnectionOverview:
    """Everything the Connection page shows (built in one request session)."""

    status: ConnectionStatus
    items: tuple[ItemView, ...]
    runs: tuple[RunView, ...]
    lost_edits: tuple[LostEdit, ...]
    attach: AttachChoice | None
    production: bool
    can_link: bool
    link_blockers: tuple[str, ...]
    reattach_available: bool
    orphaned_ids: tuple[str, ...]
    reminder_days: int

    @property
    def linked(self) -> bool:
        return bool(self.items)

    @property
    def syncing(self) -> bool:
        return self.status.syncing

    @property
    def link_actions(self) -> bool:
        """Whether the page offers a Plaid Link action (so it needs Plaid's script)."""
        if not self.items:
            return self.can_link
        return any(
            i.needs_reconnect or i.status.consent_warning or not i.liabilities_consented
            for i in self.items
        )


def _account_view(a: Account) -> AccountView:
    return AccountView(
        a.id,
        a.display_name,
        a.official_name,
        a.type,
        a.subtype,
        a.iso_currency_code,
        a.current_balance,
        a.available_balance,
        a.credit_limit,
        a.balances_as_of,
        a.statement_balance,
        a.statement_issue_date,
        a.payment_due_date,
        a.minimum_payment,
        a.last_payment_amount,
        a.last_payment_date,
        a.is_overdue,
        a.liabilities_as_of,
        a.notice,
        a.is_hidden,
    )


def connection_overview(
    session: Session,
    settings: Settings,
    *,
    store: secrets.SecretStore | None = None,
    now: dt.datetime | None = None,
) -> ConnectionOverview:
    """The Connection page's data: Items with their accounts, the last 20 sync runs, edits that
    found no replacement, the CSV-attach choice, and — before anything is linked — the link guard
    (production) or the Sandbox orphans."""
    status = status_service.connection_status(session, now=now)
    by_id = {i.item_id: i for i in status.items}
    items = []
    for item in session.scalars(select(PlaidItem).order_by(PlaidItem.id)):
        accounts = session.scalars(
            select(Account).where(Account.item_id == item.id).order_by(Account.id)
        )
        items.append(
            ItemView(
                id=item.id,
                plaid_item_id=item.plaid_item_id,
                institution_name=item.institution_name,
                status=by_id[item.id],
                error_code=item.error_code,
                error_message=item.error_message,
                error_display_message=item.error_display_message,
                error_help=sync_service.describe_error(item.error_code),
                transactions_update_status=item.transactions_update_status,
                history_complete_at=item.history_complete_at,
                plaid_last_failed_update_at=item.plaid_last_failed_update_at,
                linked_at=item.linked_at,
                last_consented_at=item.last_consented_at,
                liabilities_status=item.liabilities_status,
                liabilities_consented="liabilities" in (item.consented_products or ()),
                accounts=tuple(_account_view(a) for a in accounts),
            )
        )
    order = {s.item_id: n for n, s in enumerate(status.items)}  # worst first, like the status
    items.sort(key=lambda i: order[i.id])
    runs = [
        RunView(
            run.id,
            run.item_id,
            name,
            run.trigger,
            run.status,
            run.started_at,
            run.finished_at,
            run.added_count,
            run.modified_count,
            run.removed_count,
            run.update_status,
            run.error_code,
            run.error_message,
        )
        for run, name in session.execute(
            select(SyncRun, PlaidItem.institution_name)
            .join(PlaidItem, PlaidItem.id == SyncRun.item_id)
            .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
            .limit(RECENT_RUNS)
        ).all()
    ]
    blockers: tuple[str, ...] = ()
    reattach = False
    orphans: tuple[str, ...] = ()
    if not items:
        if settings.is_production:
            blockers = tuple(secrets.production_link_blockers(settings, store=store))
            reattach = reattach_available(session, settings, store=store)
        else:
            orphans = tuple(orphaned_sandbox_items(session, store=store))
    can_link = not items and not blockers and not reattach if settings.is_production else True
    return ConnectionOverview(
        status=status,
        items=tuple(items),
        runs=tuple(runs),
        lost_edits=tuple(lost_edits(session)),
        attach=attach_choice(session),
        production=settings.is_production,
        can_link=can_link,
        link_blockers=blockers,
        reattach_available=reattach,
        orphaned_ids=orphans,
        reminder_days=get_app_settings(session).consent_reminder_days,
    )
