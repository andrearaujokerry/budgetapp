"""The Settings page: ``app_settings`` and each card's statement closing day (BUD-2, ALR-1..3,
SYNC-6, REL-3), plus the read-only facts about this installation.

:func:`load` reads the current values, :func:`form_values` turns them into the strings a form
shows, and :func:`save` validates a posted form and applies it (the caller commits). Validation
collects every problem at once in :class:`SettingsInvalid` (field name → message written for the
user), so the page can show each one next to its field.

:func:`about` describes the environment: never a secret, only whether the Plaid keys are saved.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import __version__, secrets
from budgetapp.config import Settings
from budgetapp.models import Account, Statement, get_app_settings
from budgetapp.money import parse_money_input

PERIOD_MODES = (("calendar", "Calendar month"), ("statement", "Statement cycle"))
THRESHOLD_MIN, THRESHOLD_MAX, THRESHOLDS_MAX_COUNT = 1, 200, 5
REMINDER_MIN, REMINDER_MAX = 1, 120
SETTLE_MIN, SETTLE_MAX = 0, 14
CLOSING_PREFIX = "closing_day_"


class SettingsInvalid(Exception):
    """The posted settings have problems; ``errors`` maps each field name to its message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


@dataclass(frozen=True, slots=True)
class CardOption:
    """An account whose statement cycle can set the budget periods."""

    id: int
    label: str
    detail: str
    closing_day: int | None
    statements: int
    linked: bool

    @property
    def field(self) -> str:
        return f"{CLOSING_PREFIX}{self.id}"

    @property
    def has_cycle(self) -> bool:
        """A closing day or stored statement dates: statement cycles can be computed."""
        return self.closing_day is not None or self.statements > 0


@dataclass(frozen=True, slots=True)
class SettingsView:
    budget_period_mode: str
    budget_cycle_account_id: int | None
    include_pending_in_budgets: bool
    budget_alerts_enabled: bool
    alert_thresholds: tuple[int, ...]
    sync_alerts_enabled: bool
    large_charge_threshold: Decimal | None
    consent_reminder_days: int
    statement_settle_days: int
    cards: tuple[CardOption, ...]

    @property
    def cycle_card(self) -> CardOption | None:
        return next((c for c in self.cards if c.id == self.budget_cycle_account_id), None)


def card_label(account: Account) -> str:
    """``Blue Cash Preferred ··3005`` (the name the user knows, plus the last digits)."""
    name = account.name or account.official_name or "Card"
    return f"{name} ··{account.mask}" if account.mask else name


def _cards(session: Session, keep_id: int | None) -> tuple[CardOption, ...]:
    counts = dict(
        session.execute(
            select(Statement.account_id, func.count()).group_by(Statement.account_id)
        ).all()
    )
    accounts = session.scalars(
        select(Account).order_by(Account.item_id.is_(None), Account.name, Account.id)
    ).all()
    cards = []
    for account in accounts:
        if account.is_hidden and account.id != keep_id:
            continue
        linked = account.item_id is not None
        statements = int(counts.get(account.id, 0))
        detail = "Linked through Plaid" if linked else "From CSV imports"
        if account.is_hidden:
            detail += " · hidden"
        cards.append(
            CardOption(
                id=account.id,
                label=card_label(account),
                detail=detail,
                closing_day=account.statement_closing_day,
                statements=statements,
                linked=linked,
            )
        )
    return tuple(cards)


def load(session: Session) -> SettingsView:
    """The current settings and the cards that can set budget periods."""
    row = get_app_settings(session)
    return SettingsView(
        budget_period_mode=row.budget_period_mode,
        budget_cycle_account_id=row.budget_cycle_account_id,
        include_pending_in_budgets=row.include_pending_in_budgets,
        budget_alerts_enabled=row.budget_alerts_enabled,
        alert_thresholds=tuple(row.alert_thresholds or ()),
        sync_alerts_enabled=row.sync_alerts_enabled,
        large_charge_threshold=row.large_charge_threshold,
        consent_reminder_days=row.consent_reminder_days,
        statement_settle_days=row.statement_settle_days,
        cards=_cards(session, row.budget_cycle_account_id),
    )


def form_values(view: SettingsView) -> dict[str, Any]:
    """What the form shows for ``view`` (text fields as strings, switches as booleans)."""
    values: dict[str, Any] = {
        "budget_period_mode": view.budget_period_mode,
        "budget_cycle_account_id": str(view.budget_cycle_account_id or ""),
        "include_pending_in_budgets": view.include_pending_in_budgets,
        "budget_alerts_enabled": view.budget_alerts_enabled,
        "alert_thresholds": ", ".join(str(t) for t in view.alert_thresholds),
        "sync_alerts_enabled": view.sync_alerts_enabled,
        "large_charge_threshold": (
            f"{view.large_charge_threshold:.2f}" if view.large_charge_threshold is not None else ""
        ),
        "consent_reminder_days": str(view.consent_reminder_days),
        "statement_settle_days": str(view.statement_settle_days),
    }
    for card in view.cards:
        values[card.field] = "" if card.closing_day is None else str(card.closing_day)
    return values


def posted_values(view: SettingsView, form: Mapping[str, Any]) -> dict[str, Any]:
    """The form as the user submitted it (to show again next to the errors); a text field the
    post left out shows its stored value, as :func:`save` keeps it."""
    values = form_values(view)
    for key in values:
        if isinstance(values[key], bool):
            values[key] = key in form
        elif key in form:
            values[key] = str(form.get(key) or "")
    return values


# --------------------------------------------------------------------------- validation


def _whole_number(text: str, low: int, high: int, message: str) -> int:
    text = text.strip()
    if not re.fullmatch(r"\d{1,4}", text):
        raise ValueError(message)
    number = int(text)
    if not low <= number <= high:
        raise ValueError(message)
    return number


def parse_thresholds(text: str) -> tuple[int, ...]:
    """``"80, 100"`` (also ``80% 100%``) → ``(80, 100)``: unique, sorted, 1–200."""
    parts = [p for p in re.split(r"[\s,;]+", text.replace("%", " ").strip()) if p]
    if not parts:
        raise ValueError("Enter at least one percentage, e.g. 80, 100.")
    numbers = set()
    for part in parts:
        if not part.isdigit():
            raise ValueError("Use whole percentages separated by commas, e.g. 80, 100.")
        number = int(part)
        if not THRESHOLD_MIN <= number <= THRESHOLD_MAX:
            raise ValueError(
                f"Each percentage must be from {THRESHOLD_MIN} to {THRESHOLD_MAX}, e.g. 80, 100."
            )
        numbers.add(number)
    if len(numbers) > THRESHOLDS_MAX_COUNT:
        raise ValueError(f"Use at most {THRESHOLDS_MAX_COUNT} percentages.")
    return tuple(sorted(numbers))


def _text(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def save(session: Session, form: Mapping[str, Any]) -> SettingsView:
    """Validate a posted settings form and apply it; returns the new view.

    Raises :class:`SettingsInvalid` (nothing changed) when any field is wrong. Never commits.
    """
    view = load(session)
    errors: dict[str, str] = {}
    cards = {card.id: card for card in view.cards}

    mode = _text(form, "budget_period_mode")
    if mode not in dict(PERIOD_MODES):
        errors["budget_period_mode"] = "Choose Calendar month or Statement cycle."

    closing_days: dict[int, int | None] = {}
    for card in view.cards:
        if card.field not in form:
            continue  # not on the submitted form (a card added since): keep what is stored
        raw = _text(form, card.field).strip()
        if not raw:
            closing_days[card.id] = None
            continue
        try:
            closing_days[card.id] = _whole_number(
                raw, 1, 31, "Enter a day from 1 to 31, or leave it blank."
            )
        except ValueError as exc:
            errors[card.field] = str(exc)

    cycle_id: int | None = None
    raw_cycle = _text(form, "budget_cycle_account_id").strip()
    if raw_cycle:
        if raw_cycle.isdigit() and int(raw_cycle) in cards:
            cycle_id = int(raw_cycle)
        else:
            errors["budget_cycle_account_id"] = "Choose a card from the list."
    if mode == "statement" and "budget_cycle_account_id" not in errors:
        if not cards:
            errors["budget_cycle_account_id"] = (
                "Statement cycles need a card: link your Amex or import a CSV first."
            )
        elif cycle_id is None:
            errors["budget_cycle_account_id"] = (
                "Choose the card whose statement cycle sets your budget periods."
            )
        else:
            card = cards[cycle_id]
            if (
                card.field not in errors
                and closing_days.get(cycle_id, card.closing_day) is None
                and card.statements == 0
            ):
                errors[card.field] = (
                    "Enter this card's statement closing day (1–31): statement-cycle budgets "
                    "need it until a statement date is known."
                )

    alerts_on = "budget_alerts_enabled" in form
    raw_thresholds = _text(form, "alert_thresholds")
    thresholds = view.alert_thresholds
    if raw_thresholds.strip() or alerts_on:
        try:
            thresholds = parse_thresholds(raw_thresholds)
        except ValueError as exc:
            errors["alert_thresholds"] = str(exc)

    large: Decimal | None = None
    raw_large = _text(form, "large_charge_threshold").strip()
    if raw_large:
        try:
            large = parse_money_input(raw_large)
        except ValueError as exc:
            errors["large_charge_threshold"] = str(exc)
        else:
            if large <= 0:
                errors["large_charge_threshold"] = (
                    "Enter an amount above $0, or leave it blank to turn these alerts off."
                )

    reminder = settle = 0
    try:
        reminder = _whole_number(
            _text(form, "consent_reminder_days"),
            REMINDER_MIN,
            REMINDER_MAX,
            f"Enter a number of days from {REMINDER_MIN} to {REMINDER_MAX}.",
        )
    except ValueError as exc:
        errors["consent_reminder_days"] = str(exc)
    try:
        settle = _whole_number(
            _text(form, "statement_settle_days"),
            SETTLE_MIN,
            SETTLE_MAX,
            f"Enter a number of days from {SETTLE_MIN} to {SETTLE_MAX}.",
        )
    except ValueError as exc:
        errors["statement_settle_days"] = str(exc)

    if errors:
        raise SettingsInvalid(errors)

    row = get_app_settings(session)
    row.budget_period_mode = mode
    row.budget_cycle_account_id = cycle_id
    row.include_pending_in_budgets = "include_pending_in_budgets" in form
    row.budget_alerts_enabled = alerts_on
    row.alert_thresholds = list(thresholds)
    row.sync_alerts_enabled = "sync_alerts_enabled" in form
    row.large_charge_threshold = large
    row.consent_reminder_days = reminder
    row.statement_settle_days = settle
    for account_id, day in closing_days.items():
        account = session.get(Account, account_id)
        if account is not None and account.statement_closing_day != day:
            account.statement_closing_day = day
    session.flush()
    return load(session)


# --------------------------------------------------------------------------- about


@dataclass(frozen=True, slots=True)
class About:
    """Read-only facts about this installation (no secrets)."""

    env: str
    database: str
    role: str
    server: str
    data_dir: str
    logs_dir: str
    backups_dir: str
    app_version: str
    plaid_env: str | None
    plaid_keys: str  # "saved" | "missing" | "approval" | "error" | "demo"
    newest_backup_at: dt.datetime | None
    install_commit: str | None


def _home_short(path: Path | str) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def plaid_keys_status(settings: Settings, store: secrets.SecretStore | None) -> str:
    """Whether the Plaid client ID and this environment's secret are saved (values unread
    beyond the check, never returned)."""
    try:
        client_id = secrets.get_secret(secrets.PLAID_CLIENT_ID, store=store)
        secret = secrets.get_secret(secrets.plaid_secret_key(settings.plaid_env), store=store)
    except secrets.KeychainApprovalNeeded:
        return "approval"
    except secrets.SecretStoreError:
        return "error"
    return "saved" if client_id and secret else "missing"


def about(
    settings: Settings,
    store: secrets.SecretStore | None,
    *,
    demo: bool = False,
    now: dt.datetime | None = None,
) -> About:
    """What the Settings page shows under "This app" (paths shortened to ``~``)."""
    from budgetapp.services import agents
    from budgetapp.services import backup as backups

    newest = backups.newest_backup(settings)
    install_commit = None
    if settings.is_production:
        stamp = agents.read_stamp(agents.default_layout(settings))
        install_commit = stamp.short if stamp else None
    return About(
        env="demo" if demo else settings.env,
        database=settings.db_name,
        role=settings.db_role,
        server=f"PostgreSQL on {settings.pg_host}:{settings.pg_port}",
        data_dir=_home_short(settings.data_dir),
        logs_dir=_home_short(settings.logs_dir),
        backups_dir=_home_short(settings.backups_dir),
        app_version=__version__,
        plaid_env=None if demo else settings.plaid_env,
        plaid_keys="demo" if demo else plaid_keys_status(settings, store),
        newest_backup_at=newest.created_at if newest else None,
        install_commit=install_commit,
    )
