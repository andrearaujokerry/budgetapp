"""Rules (CAT-3) and re-applying categories to past transactions (CAT-4) — ARCHITECTURE §6.

Matching, validation, creation and re-categorization live in ``services.categorize``
(``rule_matches``, ``validate_rule``, ``create_rule``, ``recategorize``). This module adds what the
Rules page needs on top of them, without a second copy of the matching logic:

* :func:`list_rules`: every rule in run order ``(priority, id)`` with a human summary of its
  conditions ("Merchant contains UBER · amount ≥ $50") and how many transactions it categorized.
* :func:`create`, :func:`update_rule`, :func:`delete_rule`, :func:`set_active` and
  :func:`move_rule` (up/down; priorities are then renumbered 10, 20, 30, … in run order).
* :func:`preview`: what saving a rule (new or edited) with "also apply to past transactions" would
  change, without saving anything. The full precedence runs, so a card payment (step 0, which no
  rule can override) stays a card payment, a category you set yourself never changes, and an
  earlier rule still wins. Its counts are exactly what :func:`create` / :func:`update_rule` then
  change.
* :func:`reapply` (CAT-4): steps 0 and 2–4 again for every transaction in scope (all time, since a
  date, or only Uncategorized) whose category you didn't set yourself — dry run first. Rows you
  edited are never changed and are counted as skipped. Amex CSV rows are re-categorized through the
  Amex mapping like Plaid rows through Plaid's.

Services never commit; the route or command does.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, true
from sqlalchemy.orm import Session

from budgetapp.db import ledger_lock
from budgetapp.models import Account, Category, Rule, Transaction
from budgetapp.money import format_money
from budgetapp.services import categorize
from budgetapp.services.categories import CategoryRef
from budgetapp.services.categorize import (
    Categorizer,
    RecategorizeResult,
    RuleValidationError,
    recategorize,
    rule_matches,
)

MATCH_TYPE_LABELS: dict[str, str] = {
    "contains": "Contains",
    "equals": "Is exactly",
    "starts_with": "Starts with",
    "regex": "Regular expression",
}
_MATCH_VERBS = {
    "contains": "contains",
    "equals": "is",
    "starts_with": "starts with",
    "regex": "matches",
}
PRIORITY_STEP = 10
MAX_PRIORITY = 1_000_000
MAX_NAME_LENGTH = 80
#: Sorts a rule that isn't saved yet after every saved rule of the same priority (as its new id).
_UNSAVED_ID = 2**62
PREVIEW_LIMIT = 20
REAPPLY_SCOPES: dict[str, str] = {
    "all": "All past transactions",
    "since": "Transactions since a date",
    "uncategorized": "Only Uncategorized transactions",
}


class RuleNotFound(LookupError):
    """No rule with that id (the route answers 404)."""


# --------------------------------------------------------------------------- the form


@dataclass(frozen=True, slots=True)
class RuleDraft:
    """A rule as typed in the form. Text is kept as typed, so a form with errors re-renders it."""

    name: str = ""
    merchant_pattern: str = ""
    description_pattern: str = ""
    match_type: str = "contains"
    amount_min: str = ""
    amount_max: str = ""
    account_id: str = ""
    category_id: str = ""
    priority: str = ""
    is_active: bool = True
    apply_to_existing: bool = False

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> RuleDraft:
        """From posted fields (unchecked boxes send nothing: missing ``is_active`` is off)."""

        def text(name: str) -> str:
            value = form.get(name)
            return value.strip() if isinstance(value, str) else ""

        return cls(
            name=text("name"),
            merchant_pattern=text("merchant_pattern"),
            description_pattern=text("description_pattern"),
            match_type=text("match_type") or "contains",
            amount_min=text("amount_min"),
            amount_max=text("amount_max"),
            account_id=text("account_id"),
            category_id=text("category_id"),
            priority=text("priority"),
            is_active=bool(text("is_active")),
            apply_to_existing=bool(text("apply_to_existing")),
        )

    @classmethod
    def from_rule(cls, rule: Rule) -> RuleDraft:
        def amount(value: Decimal | None) -> str:
            return "" if value is None else f"{value:.2f}"

        return cls(
            name=rule.name or "",
            merchant_pattern=rule.merchant_pattern or "",
            description_pattern=rule.description_pattern or "",
            match_type=rule.match_type,
            amount_min=amount(rule.amount_min),
            amount_max=amount(rule.amount_max),
            account_id=str(rule.account_id) if rule.account_id is not None else "",
            category_id=str(rule.category_id),
            priority=str(rule.priority),
            is_active=rule.is_active,
        )

    @property
    def is_blank(self) -> bool:
        """No condition typed yet (the preview then just explains what to do)."""
        return not any(
            (
                self.merchant_pattern,
                self.description_pattern,
                self.amount_min,
                self.amount_max,
                self.account_id,
            )
        )


@dataclass(frozen=True, slots=True)
class CleanRule:
    """Validated rule fields (``categorize.validate_rule`` plus name, priority and state)."""

    name: str | None
    category_id: int
    merchant_pattern: str | None
    description_pattern: str | None
    match_type: str
    amount_min: Decimal | None
    amount_max: Decimal | None
    account_id: int | None
    priority: int
    is_active: bool


def _int_field(
    value: str, field: str, message: str, errors: dict[str, str], *, limit: int = 2**62
) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        errors[field] = message
        return None
    if abs(number) > limit:
        errors[field] = message
        return None
    return number


def next_priority(session: Session) -> int:
    """The priority that puts a new rule last (100 when there are no rules yet)."""
    highest = session.scalar(select(func.max(Rule.priority)))
    return 100 if highest is None else highest + PRIORITY_STEP


def clean_rule(session: Session, draft: RuleDraft, *, current: Rule | None = None) -> CleanRule:
    """Validate a draft; raises ``RuleValidationError`` with a message per field.

    An empty priority keeps the rule's current one, or puts a new rule last.
    """
    errors: dict[str, str] = {}
    name = " ".join(draft.name.split()) or None
    if name is not None and len(name) > MAX_NAME_LENGTH:
        errors["name"] = f"Keep the name under {MAX_NAME_LENGTH} characters."
    category_id = _int_field(draft.category_id, "category_id", "Choose a category.", errors)
    account_id = _int_field(draft.account_id, "account_id", "Choose a card.", errors)
    priority = _int_field(
        draft.priority,
        "priority",
        f"Enter a whole number between −{MAX_PRIORITY:,} and {MAX_PRIORITY:,}.",
        errors,
        limit=MAX_PRIORITY,
    )
    try:
        values = categorize.validate_rule(
            session,
            category_id=category_id,
            merchant_pattern=draft.merchant_pattern,
            description_pattern=draft.description_pattern,
            match_type=draft.match_type,
            amount_min=draft.amount_min,
            amount_max=draft.amount_max,
            account_id=account_id,
        )
    except RuleValidationError as exc:
        values = None
        for field, message in exc.errors.items():
            errors.setdefault(field, message)
    if errors or values is None:
        raise RuleValidationError(errors)
    if priority is None:
        priority = current.priority if current is not None else next_priority(session)
    return CleanRule(
        name=name,
        priority=priority,
        is_active=draft.is_active,
        **values,
    )


# --------------------------------------------------------------------------- listing


@dataclass(frozen=True, slots=True)
class RuleRow:
    """One rule as the Rules page lists it."""

    id: int
    position: int  #: 1 = runs first
    name: str | None
    priority: int
    is_active: bool
    is_system: bool
    match_type: str
    merchant_pattern: str | None
    description_pattern: str | None
    amount_min: Decimal | None
    amount_max: Decimal | None
    account_id: int | None
    account_label: str | None
    category: CategoryRef
    summary: str
    #: Transactions whose category this rule set (``transactions.rule_id``).
    transaction_count: int
    source_transaction_id: int | None

    @property
    def title(self) -> str:
        return self.name or self.summary


def _amount_text(value: Decimal) -> str:
    text = format_money(value)
    return text[:-3] if text.endswith(".00") else text


def summarize(rule: Any, account_label: str | None = None) -> str:
    """Conditions in words: ``Merchant contains UBER · amount ≥ $50``."""
    verb = _MATCH_VERBS.get(rule.match_type, rule.match_type)
    parts: list[str] = []
    if rule.merchant_pattern:
        parts.append(f"Merchant {verb} {rule.merchant_pattern}")
    if rule.description_pattern:
        parts.append(f"Description {verb} {rule.description_pattern}")
    low, high = rule.amount_min, rule.amount_max
    if low is not None and high is not None:
        if low == high:
            parts.append(f"Amount is {_amount_text(low)}")
        else:
            parts.append(f"Amount {_amount_text(low)} to {_amount_text(high)}")
    elif low is not None:
        parts.append(f"Amount ≥ {_amount_text(low)}")
    elif high is not None:
        parts.append(f"Amount ≤ {_amount_text(high)}")
    if rule.account_id is not None:
        parts.append(f"Card {account_label or '#' + str(rule.account_id)}")
    if not parts:
        return "No conditions"
    return " · ".join([parts[0], *(p[:1].lower() + p[1:] for p in parts[1:])])


def get_rule(session: Session, rule_id: int) -> Rule:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise RuleNotFound(rule_id)
    return rule


def ordered_rules(session: Session) -> list[Rule]:
    """Every rule in run order: ``(priority, id)``."""
    return list(session.scalars(select(Rule).order_by(Rule.priority, Rule.id)))


def account_labels(session: Session) -> dict[int, str]:
    return {a.id: a.display_name for a in session.scalars(select(Account).order_by(Account.id))}


def list_rules(session: Session) -> list[RuleRow]:
    """Every rule in run order, with its summary, category and how many rows it categorized."""
    labels = account_labels(session)
    categories = {c.id: CategoryRef.of(c) for c in session.scalars(select(Category))}
    counts = dict(
        session.execute(
            select(Transaction.rule_id, func.count())
            .where(Transaction.rule_id.is_not(None))
            .group_by(Transaction.rule_id)
        ).all()
    )
    rows = []
    for position, rule in enumerate(ordered_rules(session), start=1):
        label = labels.get(rule.account_id) if rule.account_id is not None else None
        rows.append(
            RuleRow(
                id=rule.id,
                position=position,
                name=rule.name,
                priority=rule.priority,
                is_active=rule.is_active,
                is_system=rule.is_system,
                match_type=rule.match_type,
                merchant_pattern=rule.merchant_pattern,
                description_pattern=rule.description_pattern,
                amount_min=rule.amount_min,
                amount_max=rule.amount_max,
                account_id=rule.account_id,
                account_label=label,
                category=categories[rule.category_id],
                summary=summarize(rule, label),
                transaction_count=int(counts.get(rule.id, 0)),
                source_transaction_id=rule.source_transaction_id,
            )
        )
    return rows


def get_row(session: Session, rule_id: int) -> RuleRow:
    for row in list_rules(session):
        if row.id == rule_id:
            return row
    raise RuleNotFound(rule_id)


# --------------------------------------------------------------------------- changes


@dataclass(frozen=True, slots=True)
class RuleSaveResult:
    rule: Rule
    #: What "also apply to past transactions" changed (``None`` when it wasn't asked for).
    applied: RecategorizeResult | None


def create(session: Session, draft: RuleDraft) -> RuleSaveResult:
    """Save a new rule through ``categorize.create_rule`` (optionally applied to past rows)."""
    clean = clean_rule(session, draft)
    result = categorize.create_rule(
        session,
        category_id=clean.category_id,
        name=clean.name,
        merchant_pattern=clean.merchant_pattern,
        description_pattern=clean.description_pattern,
        match_type=clean.match_type,
        amount_min=clean.amount_min,
        amount_max=clean.amount_max,
        account_id=clean.account_id,
        priority=clean.priority,
        is_active=clean.is_active,
        apply_to_existing=draft.apply_to_existing,
    )
    return RuleSaveResult(result.rule, result.applied)


def update_rule(session: Session, rule_id: int, draft: RuleDraft) -> RuleSaveResult:
    """Change a rule. With ``draft.apply_to_existing`` the full precedence runs again on every
    transaction it matches now or categorized before (your own edits are never changed)."""
    rule = get_rule(session, rule_id)
    clean = clean_rule(session, draft, current=rule)
    if draft.apply_to_existing:
        ledger_lock(session)
    previously = set(session.scalars(select(Transaction.id).where(Transaction.rule_id == rule.id)))
    for field in dataclasses.fields(CleanRule):
        setattr(rule, field.name, getattr(clean, field.name))
    session.flush()
    applied = None
    if draft.apply_to_existing:
        matching, matching_user = _matches(session, rule)
        result = recategorize(session, transaction_ids=sorted(set(matching) | previously))
        applied = dataclasses.replace(result, skipped_user=matching_user)
    return RuleSaveResult(rule, applied)


@dataclass(frozen=True, slots=True)
class DeletedRule:
    summary: str
    #: Transactions it had categorized: they keep their category until categories are re-applied.
    transaction_count: int


def delete_rule(session: Session, rule_id: int) -> DeletedRule:
    rule = get_rule(session, rule_id)
    row = get_row(session, rule.id)
    session.execute(delete(Rule).where(Rule.id == rule.id))
    session.expire_all()
    return DeletedRule(row.title, row.transaction_count)


def set_active(session: Session, rule_id: int, active: bool) -> Rule:
    rule = get_rule(session, rule_id)
    rule.is_active = bool(active)
    session.flush()
    return rule


def renumber(session: Session, rules: list[Rule] | None = None) -> None:
    """Priorities 10, 20, 30, … in run order (or in the order of ``rules``)."""
    for position, rule in enumerate(rules if rules is not None else ordered_rules(session), 1):
        rule.priority = position * PRIORITY_STEP
    session.flush()


def move_rule(session: Session, rule_id: int, direction: str) -> bool:
    """Move a rule one place ``up`` (runs earlier) or ``down``; False when it can't move."""
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', not {direction!r}")
    rules = ordered_rules(session)
    index = next((i for i, r in enumerate(rules) if r.id == rule_id), None)
    if index is None:
        raise RuleNotFound(rule_id)
    other = index - 1 if direction == "up" else index + 1
    if not 0 <= other < len(rules):
        return False
    rules[index], rules[other] = rules[other], rules[index]
    renumber(session, rules)
    return True


# --------------------------------------------------------------------------- preview


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An unsaved rule with what ``rule_matches`` and ``Categorizer`` read from a ``Rule``."""

    id: int
    category_id: int
    merchant_pattern: str | None
    description_pattern: str | None
    match_type: str
    amount_min: Decimal | None
    amount_max: Decimal | None
    account_id: int | None
    priority: int
    is_active: bool


_TXN_COLUMNS = (
    Transaction.id,
    Transaction.account_id,
    Transaction.source,
    Transaction.amount,
    Transaction.name,
    Transaction.merchant_name,
    Transaction.original_description,
    Transaction.transaction_code,
    Transaction.pfc_primary,
    Transaction.pfc_detailed,
    Transaction.external_category,
    Transaction.category_id,
    Transaction.category_source,
    Transaction.rule_id,
    Transaction.date,
    Transaction.authorized_date,
    Transaction.pending,
)


def _rows(session: Session, rule: Any, *, also_rule_id: int | None = None):
    """Transactions the rule can match (amount and card filtered in SQL), newest first, plus
    those ``also_rule_id`` categorized. Text conditions are checked by ``rule_matches``."""
    conditions = []
    if rule.account_id is not None:
        conditions.append(Transaction.account_id == rule.account_id)
    if rule.amount_min is not None:
        conditions.append(Transaction.amount >= rule.amount_min)
    if rule.amount_max is not None:
        conditions.append(Transaction.amount <= rule.amount_max)
    where = and_(*conditions) if conditions else true()
    if also_rule_id is not None:
        where = or_(where, Transaction.rule_id == also_rule_id)
    spend_date = func.coalesce(Transaction.authorized_date, Transaction.date)
    query = select(*_TXN_COLUMNS).where(where).order_by(spend_date.desc(), Transaction.id.desc())
    return session.execute(query).all()


def _matches(session: Session, rule: Any) -> tuple[list[int], int]:
    """Ids of the non-user rows ``rule`` matches, and how many user-edited rows it matches."""
    ids: list[int] = []
    user = 0
    for row in _rows(session, rule):
        if rule_matches(rule, row):
            if row.category_source == "user":
                user += 1
            else:
                ids.append(row.id)
    return ids, user


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """One transaction in a rule preview."""

    transaction_id: int
    date: dt.date
    description: str
    detail: str | None
    amount: Decimal
    pending: bool
    current: CategoryRef
    proposed: CategoryRef | None  #: set when the category would change
    #: ``change`` · ``same`` · ``user`` · ``payment`` · ``earlier_rule`` · ``off`` · ``released``
    status: str
    note: str


@dataclass(frozen=True, slots=True)
class RulePreview:
    """What saving the rule with "also apply to past transactions" would do."""

    match_count: int
    would_change_count: int
    skipped_user_count: int
    payment_count: int
    earlier_rule_count: int
    #: Editing only: rows the rule categorized that it no longer matches (they get re-decided).
    released_count: int
    is_active: bool
    is_new: bool
    sample: tuple[PreviewItem, ...]


def preview(
    session: Session, draft: RuleDraft, *, rule_id: int | None = None, limit: int = PREVIEW_LIMIT
) -> RulePreview:
    """What saving ``draft`` (a new rule, or rule ``rule_id`` edited) and applying it to past
    transactions would change, without saving. Raises ``RuleValidationError`` like saving."""
    current = get_rule(session, rule_id) if rule_id is not None else None
    clean = clean_rule(session, draft, current=current)
    candidate = _Candidate(
        id=rule_id if rule_id is not None else _UNSAVED_ID,
        category_id=clean.category_id,
        merchant_pattern=clean.merchant_pattern,
        description_pattern=clean.description_pattern,
        match_type=clean.match_type,
        amount_min=clean.amount_min,
        amount_max=clean.amount_max,
        account_id=clean.account_id,
        priority=clean.priority,
        is_active=clean.is_active,
    )
    categorizer = Categorizer.load(session)
    rules: list[Any] = [r for r in categorizer.rules if r.id != rule_id]
    if candidate.is_active:
        rules.append(candidate)
    rules.sort(key=lambda r: (r.priority, r.id))
    categorizer.rules = rules
    names = {r.id: r.name or summarize(r) for r in ordered_rules(session)}
    categories = {c.id: CategoryRef.of(c) for c in session.scalars(select(Category))}
    # A new inactive rule is saved without touching past rows (categorize.create_rule); an edit
    # with "apply" re-decides what it matches and what it categorized before, active or not.
    applies = rule_id is not None or candidate.is_active

    matched = changed = user = payments = earlier = released = 0
    sample: list[PreviewItem] = []
    for row in _rows(session, candidate, also_rule_id=rule_id):
        matches = rule_matches(candidate, row)
        before = rule_id is not None and row.rule_id == rule_id
        if not matches and not before:
            continue
        matched += 1 if matches else 0
        released += 1 if before and not matches else 0
        current_category = categories[row.category_id]
        proposed = None
        if row.category_source == "user":
            user += 1
            status, note = "user", "You set this category: it never changes."
        elif not applies:
            status, note = "off", "The rule is off: saving it changes nothing."
        else:
            decision = categorizer.decide(row)
            if decision.category_id != row.category_id:
                changed += 1
                proposed = categories[decision.category_id]
            if decision.source == "system":
                payments += 1
                status, note = "payment", "A card payment: rules never change it."
            elif decision.source == "rule" and decision.rule_id != candidate.id:
                earlier += 1
                status = "earlier_rule"
                note = f"“{names.get(decision.rule_id, 'Another rule')}” runs first."
            elif not matches:
                status, note = "released", "No longer matches this rule."
            elif proposed is not None:
                status, note = "change", ""
            else:
                status, note = "same", "Already in this category."
            if proposed is not None and status != "change":
                note = f"{note} Moves to {proposed.name}."
        if len(sample) < limit:
            sample.append(
                PreviewItem(
                    transaction_id=row.id,
                    date=row.authorized_date or row.date,
                    description=row.merchant_name or row.name,
                    detail=row.name if row.merchant_name else None,
                    amount=row.amount,
                    pending=row.pending,
                    current=current_category,
                    proposed=proposed,
                    status=status,
                    note=note.strip(),
                )
            )
    return RulePreview(
        match_count=matched,
        would_change_count=changed,
        skipped_user_count=user,
        payment_count=payments,
        earlier_rule_count=earlier,
        released_count=released,
        is_active=candidate.is_active,
        is_new=rule_id is None,
        sample=tuple(sample),
    )


# --------------------------------------------------------------------------- re-apply (CAT-4)


class ReapplyScopeError(ValueError):
    """An invalid re-apply scope; ``errors`` maps a form field to a message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(" ".join(self.errors.values()))


@dataclass(frozen=True, slots=True)
class ReapplyScope:
    since: dt.date | None = None
    only_uncategorized: bool = False

    @property
    def key(self) -> str:
        if self.only_uncategorized:
            return "uncategorized"
        return "since" if self.since is not None else "all"

    @property
    def description(self) -> str:
        since = f" since {_long_date(self.since)}" if self.since is not None else ""
        if self.only_uncategorized:
            return f"Uncategorized transactions{since}"
        return f"transactions{since}" if since else "all past transactions"


def _long_date(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def parse_scope(scope: str, since: str = "") -> ReapplyScope:
    """``all`` | ``since`` (with a ``YYYY-MM-DD`` date) | ``uncategorized``."""
    if scope not in REAPPLY_SCOPES:
        raise ReapplyScopeError({"scope": "Choose which transactions to update."})
    if scope == "uncategorized":
        return ReapplyScope(only_uncategorized=True)
    if scope == "all":
        return ReapplyScope()
    try:
        day = dt.date.fromisoformat((since or "").strip())
    except ValueError:
        raise ReapplyScopeError({"since": "Enter the first date to update."}) from None
    return ReapplyScope(since=day)


@dataclass(frozen=True, slots=True)
class ReapplyItem:
    transaction_id: int
    date: dt.date
    description: str
    amount: Decimal
    old: CategoryRef
    new: CategoryRef


@dataclass(frozen=True, slots=True)
class ReapplyResult:
    scope: ReapplyScope
    dry_run: bool
    #: Transactions in scope whose category you didn't set (each was re-decided).
    examined: int
    #: Transactions whose category changes (or changed).
    changed: int
    #: Transactions in scope whose category you set yourself: never changed.
    skipped_user: int
    sample: tuple[ReapplyItem, ...]


def reapply(
    session: Session, scope: ReapplyScope, *, dry_run: bool = True, limit: int = 10
) -> ReapplyResult:
    """Re-run categorization steps 0 and 2–4 (``categorize.recategorize``) on ``scope``.

    Takes the ledger lock. A dry run changes nothing; the caller commits a real run.
    """
    result = recategorize(
        session,
        start=scope.since,
        only_uncategorized=scope.only_uncategorized,
        dry_run=dry_run,
    )
    sample: list[ReapplyItem] = []
    changes = result.changes[:limit]
    if changes:
        categories = {c.id: CategoryRef.of(c) for c in session.scalars(select(Category))}
        rows = {
            r.id: r
            for r in session.execute(
                select(
                    Transaction.id,
                    Transaction.date,
                    Transaction.authorized_date,
                    Transaction.name,
                    Transaction.merchant_name,
                    Transaction.amount,
                ).where(Transaction.id.in_([c.transaction_id for c in changes]))
            )
        }
        for change in changes:
            row = rows[change.transaction_id]
            sample.append(
                ReapplyItem(
                    transaction_id=row.id,
                    date=row.authorized_date or row.date,
                    description=row.merchant_name or row.name,
                    amount=row.amount,
                    old=categories[change.old_category_id],
                    new=categories[change.new_category_id],
                )
            )
    return ReapplyResult(
        scope=scope,
        dry_run=dry_run,
        examined=result.examined,
        changed=result.changed,
        skipped_user=result.skipped_user,
        sample=tuple(sample),
    )
