"""Categorization (CAT-2, CAT-3, CAT-4, TXN-3) — ARCHITECTURE §6 "Categorization precedence".

For a new transaction, the first step that applies wins:

0. **Payments** (fixed; rules cannot override): Card Payments (``category_source='system'``) when
   ``amount < 0`` and any of: ``transaction_code = 'payment'``; ``pfc_detailed`` is
   ``LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`` or ``LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT``;
   ``pfc_primary = 'TRANSFER_IN'``; the name or original description matches
   ``PAYMENT.*(THANK YOU|RECEIVED)|AUTOPAY``; or a CSV row with a blank Amex category. A positive
   ``PAYMENT … RETURNED/REVERSED`` (not a fee) is also Card Payments.
1. User state carried over from the transaction this one replaces (``category_source='user'``).
2. The first matching active rule by ``(priority, id)`` (``'rule'``).
3. The Plaid PFC v2 mapping (detailed, then primary; ``'plaid'``) or, for CSV rows, the Amex
   category mapping (``Group-Subgroup``, then ``Group``; ``'amex'``).
4. Uncategorized (``'default'``).

Refund rule: a row with ``transaction_code = 'refund'`` never lands in an ``income`` or
``transfer`` category; it takes the most recent expense category used for the same merchant, else
Uncategorized (``'default'``). A refund is never treated as a payment by step 0.

Existing rows: a user edit (``category_source='user'``) is never overridden by
:func:`apply_decision` or :func:`recategorize`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp.db import ledger_lock
from budgetapp.models import (
    MATCH_TYPES,
    Account,
    Category,
    CategoryMapping,
    Rule,
    Transaction,
)
from budgetapp.money import to_money
from budgetapp.services.userstate import UserState

PAYMENT_PFC_DETAILED = frozenset(
    {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT"}
)
PAYMENT_TEXT = re.compile(r"PAYMENT.*(THANK YOU|RECEIVED)|AUTOPAY", re.IGNORECASE)
PAYMENT_REVERSAL_TEXT = re.compile(
    r"\bPAYMENT\b.*\b(RETURNED|REVERSED|REVERSAL)\b|\b(RETURNED|REVERSED)\b.*\bPAYMENT\b",
    re.IGNORECASE,
)
FEE_TEXT = re.compile(r"\bFEES?\b", re.IGNORECASE)
FEE_CODES = frozenset(
    {"late fee", "membership fee", "returned item fee", "bank charge", "interest"}
)
MAX_PATTERN_LENGTH = 300
CHANGE_SAMPLE_LIMIT = 200


class TransactionLike(Protocol):
    """The fields categorization reads (a ``Transaction`` or any object with these attributes)."""

    id: Any
    account_id: int
    source: str
    amount: Decimal
    name: str
    merchant_name: str | None
    original_description: str | None
    transaction_code: str | None
    pfc_primary: str | None
    pfc_detailed: str | None
    external_category: str | None


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of categorizing one transaction."""

    category_id: int
    source: str
    rule_id: int | None = None
    reason: str = ""


class RuleValidationError(ValueError):
    """Invalid rule fields; ``errors`` maps a field name to a human message."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


# --------------------------------------------------------------------------- matching helpers


def normalize_text(value: str | None) -> str:
    """Collapse whitespace runs (Amex descriptions have many) and casefold."""
    return " ".join(value.split()).casefold() if value else ""


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def text_matches(pattern: str, value: str | None, match_type: str) -> bool:
    """Case-insensitive ``contains`` / ``equals`` / ``starts_with`` / ``regex`` match.

    Whitespace runs are collapsed on both sides first (also for ``regex``, which uses ``search``).
    """
    if not pattern or value is None:
        return False
    if match_type == "regex":
        compiled = _compile(pattern)
        return compiled is not None and compiled.search(" ".join(value.split())) is not None
    p, v = normalize_text(pattern), normalize_text(value)
    if match_type == "contains":
        return p in v
    if match_type == "equals":
        return v == p
    if match_type == "starts_with":
        return v.startswith(p)
    return False


def rule_matches(rule: Rule, txn: TransactionLike) -> bool:
    """True when every condition the rule sets matches ``txn`` (AND). Ignores ``is_active``.

    * ``merchant_pattern`` → ``merchant_name`` (falls back to ``name`` when null);
    * ``description_pattern`` → ``name`` or ``original_description``;
    * ``amount_min``/``amount_max`` → inclusive bounds on the signed amount;
    * ``account_id`` → equality. A rule with no condition matches nothing.
    """
    has_condition = False
    if rule.account_id is not None:
        has_condition = True
        if txn.account_id != rule.account_id:
            return False
    amount = to_money(txn.amount)
    if rule.amount_min is not None:
        has_condition = True
        if amount < to_money(rule.amount_min):
            return False
    if rule.amount_max is not None:
        has_condition = True
        if amount > to_money(rule.amount_max):
            return False
    if rule.merchant_pattern:
        has_condition = True
        target = txn.merchant_name if txn.merchant_name else txn.name
        if not text_matches(rule.merchant_pattern, target, rule.match_type):
            return False
    if rule.description_pattern:
        has_condition = True
        if not (
            text_matches(rule.description_pattern, txn.name, rule.match_type)
            or text_matches(rule.description_pattern, txn.original_description, rule.match_type)
        ):
            return False
    return has_condition


def is_payment(txn: TransactionLike) -> bool:
    """Step 0: is this a card payment (or a returned payment)?"""
    code = (txn.transaction_code or "").strip().lower()
    if code == "refund":
        return False
    amount = to_money(txn.amount)
    texts = [t for t in (txn.name, txn.original_description) if t]
    if amount < 0:
        return (
            code == "payment"
            or txn.pfc_detailed in PAYMENT_PFC_DETAILED
            or txn.pfc_primary == "TRANSFER_IN"
            or any(PAYMENT_TEXT.search(t) for t in texts)
            or (txn.source == "csv" and not (txn.external_category or "").strip())
        )
    if amount > 0 and code not in FEE_CODES:
        return any(PAYMENT_REVERSAL_TEXT.search(t) and not FEE_TEXT.search(t) for t in texts)
    return False


_AMEX_SEPARATOR = re.compile(r"\s*(?:-|–|—|>)\s*")


def amex_category_keys(raw: str | None) -> tuple[str, ...]:
    """Lookup keys for an Amex CSV ``Category``: ``("Group-Subgroup", "Group")``; ``()`` if blank.

    Splits on the first separator (Amex group names contain ``&`` and spaces but no hyphen) and
    tolerates the spaced or en-dash spellings seen in the wild, e.g.
    ``"Transportation - Taxis & Coach"``.
    """
    if not raw or not raw.strip():
        return ()
    text = " ".join(raw.split())
    parts = _AMEX_SEPARATOR.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        group, sub = parts[0].strip(), parts[1].strip()
        return (f"{group}-{sub}", group)
    return (text,)


def system_category_id(session: Session, key: str) -> int:
    """Id of the system category ``uncategorized`` or ``card_payments`` (cached per session)."""
    cache: dict[str, int] = session.info.setdefault("budgetapp.system_categories", {})
    if key not in cache:
        with session.no_autoflush:
            category_id = session.scalar(select(Category.id).where(Category.system_key == key))
        if category_id is None:
            raise LookupError(f"system category {key!r} is missing; run the migrations")
        cache[key] = category_id
    return cache[key]


# --------------------------------------------------------------------------- the categorizer


class Categorizer:
    """Categorizes transactions with the rules and mappings loaded once (one per batch).

    Build with :meth:`load`; reuse it for every row of a sync batch, an import or a re-apply.
    """

    def __init__(
        self,
        session: Session,
        *,
        rules: list[Rule],
        plaid_map: dict[str, int],
        amex_map: dict[str, int],
        kinds: dict[int, str],
        uncategorized_id: int,
        card_payments_id: int,
    ) -> None:
        self.session = session
        self.rules = rules
        self.plaid_map = plaid_map
        self.amex_map = amex_map
        self.kinds = kinds
        self.uncategorized_id = uncategorized_id
        self.card_payments_id = card_payments_id
        self._refund_cache: dict[str, int] = {}

    @classmethod
    def load(cls, session: Session) -> Categorizer:
        """Load active rules (by priority, id), both mappings and category kinds."""
        with session.no_autoflush:
            rules = list(
                session.scalars(
                    select(Rule).where(Rule.is_active.is_(True)).order_by(Rule.priority, Rule.id)
                )
            )
            plaid_map: dict[str, int] = {}
            amex_map: dict[str, int] = {}
            for source, key, category_id in session.execute(
                select(
                    CategoryMapping.source,
                    CategoryMapping.external_key,
                    CategoryMapping.category_id,
                )
            ):
                if source == "plaid_pfc":
                    plaid_map[key] = category_id
                else:
                    amex_map[key.casefold()] = category_id
            kinds = {cid: kind for cid, kind in session.execute(select(Category.id, Category.kind))}
        return cls(
            session,
            rules=rules,
            plaid_map=plaid_map,
            amex_map=amex_map,
            kinds=kinds,
            uncategorized_id=system_category_id(session, "uncategorized"),
            card_payments_id=system_category_id(session, "card_payments"),
        )

    def kind_of(self, category_id: int) -> str:
        if category_id not in self.kinds:
            with self.session.no_autoflush:
                kind = self.session.scalar(select(Category.kind).where(Category.id == category_id))
            self.kinds[category_id] = kind or "expense"
        return self.kinds[category_id]

    def decide(self, txn: TransactionLike, *, inherited: UserState | None = None) -> Decision:
        """Apply the precedence (module docstring) to ``txn``.

        ``inherited`` is the user state carried over from the row ``txn`` replaces (step 1). Note
        that step 0 ranks above it, as ARCHITECTURE §6 orders them; a caller that then moves the
        state with ``userstate.apply`` (which sets the user's category) makes the user's choice win.
        """
        if is_payment(txn):
            return Decision(self.card_payments_id, "system", reason="card payment")
        if inherited is not None and inherited.category_id is not None:
            return Decision(
                inherited.category_id, "user", reason="carried over from the replaced row"
            )
        return self._refund_rule(txn, self.automatic(txn))

    def automatic(self, txn: TransactionLike) -> Decision:
        """Steps 2–4 only (rules, mapping, Uncategorized), without the refund rule."""
        for rule in self.rules:
            if rule_matches(rule, txn):
                return Decision(rule.category_id, "rule", rule.id, reason=f"rule {rule.id}")
        if txn.source == "csv":
            for key in amex_category_keys(txn.external_category):
                category_id = self.amex_map.get(key.casefold())
                if category_id is not None:
                    return Decision(category_id, "amex", reason=f"Amex category {key}")
        else:
            for key in (txn.pfc_detailed, txn.pfc_primary):
                if key and key in self.plaid_map:
                    return Decision(self.plaid_map[key], "plaid", reason=f"Plaid category {key}")
        return Decision(self.uncategorized_id, "default", reason="no rule or mapping")

    def _refund_rule(self, txn: TransactionLike, decision: Decision) -> Decision:
        if (txn.transaction_code or "").strip().lower() != "refund":
            return decision
        if self.kind_of(decision.category_id) == "expense":
            return decision
        return Decision(self.refund_category(txn), "default", reason="refund")

    def refund_category(self, txn: TransactionLike) -> int:
        """Most recent expense category used for the same merchant, else Uncategorized."""
        merchant = (txn.merchant_name or txn.name or "").strip()
        if not merchant:
            return self.uncategorized_id
        key = merchant.casefold()
        if key not in self._refund_cache:
            spend_date = func.coalesce(Transaction.authorized_date, Transaction.date)
            query = (
                select(Transaction.category_id)
                .join(Category, Category.id == Transaction.category_id)
                .where(
                    Category.kind == "expense",
                    Transaction.amount > 0,
                    func.lower(func.coalesce(Transaction.merchant_name, Transaction.name))
                    == merchant.lower(),
                )
                .order_by(spend_date.desc(), Transaction.id.desc())
                .limit(1)
            )
            if getattr(txn, "id", None) is not None:
                query = query.where(Transaction.id != txn.id)
            with self.session.no_autoflush:
                found = self.session.scalar(query)
            self._refund_cache[key] = found if found is not None else self.uncategorized_id
        return self._refund_cache[key]


def apply_decision(txn: Transaction, decision: Decision) -> bool:
    """Write ``decision`` onto ``txn`` unless the user set its category; True if it changed."""
    if txn.category_source == "user":
        return False
    rule_id = decision.rule_id if decision.source == "rule" else None
    changed = (txn.category_id, txn.category_source, txn.rule_id) != (
        decision.category_id,
        decision.source,
        rule_id,
    )
    txn.category_id = decision.category_id
    txn.category_source = decision.source
    txn.rule_id = rule_id
    return changed


# --------------------------------------------------------------------------- re-apply (CAT-4)


@dataclass(frozen=True, slots=True)
class CategoryChange:
    transaction_id: int
    old_category_id: int
    new_category_id: int
    new_source: str


@dataclass(frozen=True, slots=True)
class RecategorizeResult:
    """``examined`` non-user rows in scope, ``changed`` (new category), ``skipped_user`` rows the
    user edited (never touched), and up to 200 example ``changes``."""

    examined: int
    changed: int
    skipped_user: int
    changes: tuple[CategoryChange, ...]
    dry_run: bool


def _scope(query, start: dt.date | None, end: dt.date | None):
    spend_date = func.coalesce(Transaction.authorized_date, Transaction.date)
    if start is not None:
        query = query.where(spend_date >= start)
    if end is not None:
        query = query.where(spend_date <= end)
    return query


def recategorize(
    session: Session,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    only_uncategorized: bool = False,
    dry_run: bool = False,
    transaction_ids: list[int] | None = None,
) -> RecategorizeResult:
    """Re-run steps 0 and 2–4 on every non-user-edited transaction in scope (CAT-4).

    Scope: spend date (``COALESCE(authorized_date, date)``) within ``[start, end]``, optionally only
    Uncategorized rows, optionally only ``transaction_ids``. Takes the ledger lock first.
    ``dry_run`` counts without writing. The caller commits.
    """
    ledger_lock(session)
    categorizer = Categorizer.load(session)
    user_query = _scope(
        select(func.count()).select_from(Transaction).where(Transaction.category_source == "user"),
        start,
        end,
    )
    query = _scope(select(Transaction).where(Transaction.category_source != "user"), start, end)
    if only_uncategorized:
        query = query.where(Transaction.category_id == categorizer.uncategorized_id)
        user_query = user_query.where(Transaction.category_id == categorizer.uncategorized_id)
    if transaction_ids is not None:
        query = query.where(Transaction.id.in_(transaction_ids))
        user_query = user_query.where(Transaction.id.in_(transaction_ids))
    skipped_user = session.scalar(user_query) or 0

    examined = changed = 0
    changes: list[CategoryChange] = []
    for txn in session.scalars(query.order_by(Transaction.id)):
        examined += 1
        decision = categorizer.decide(txn)
        if decision.category_id != txn.category_id:
            changed += 1
            if len(changes) < CHANGE_SAMPLE_LIMIT:
                changes.append(
                    CategoryChange(txn.id, txn.category_id, decision.category_id, decision.source)
                )
        if not dry_run:
            apply_decision(txn, decision)
    if not dry_run:
        session.flush()
    return RecategorizeResult(examined, changed, skipped_user, tuple(changes), dry_run)


# --------------------------------------------------------------------------- rules (CAT-3)


def _clean_pattern(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_amount(value: Any, field: str, errors: dict[str, str]) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, str):
            from budgetapp.money import parse_money_input

            return parse_money_input(value)
        return to_money(value)
    except (TypeError, ValueError) as exc:
        errors[field] = str(exc) if isinstance(exc, ValueError) else "Enter an amount like 25.00."
        return None


def validate_rule(
    session: Session,
    *,
    category_id: int | None,
    merchant_pattern: str | None = None,
    description_pattern: str | None = None,
    match_type: str = "contains",
    amount_min: Any = None,
    amount_max: Any = None,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize rule fields; raises :class:`RuleValidationError` (per-field errors).

    Returns the cleaned values (patterns trimmed, amounts as ``Decimal``).
    """
    errors: dict[str, str] = {}
    merchant_pattern = _clean_pattern(merchant_pattern)
    description_pattern = _clean_pattern(description_pattern)
    if match_type not in MATCH_TYPES:
        errors["match_type"] = f"Choose one of: {', '.join(MATCH_TYPES)}."
    for field, pattern in (
        ("merchant_pattern", merchant_pattern),
        ("description_pattern", description_pattern),
    ):
        if pattern is None:
            continue
        if len(pattern) > MAX_PATTERN_LENGTH:
            errors[field] = f"Keep the pattern under {MAX_PATTERN_LENGTH} characters."
        elif match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                errors[field] = f"Not a valid regular expression: {exc}."
    low = _clean_amount(amount_min, "amount_min", errors)
    high = _clean_amount(amount_max, "amount_max", errors)
    if low is not None and high is not None and low > high:
        errors["amount_max"] = "The maximum must be at least the minimum."
    if (
        merchant_pattern is None
        and description_pattern is None
        and low is None
        and high is None
        and account_id is None
        and "amount_min" not in errors
        and "amount_max" not in errors
    ):
        errors["conditions"] = "Add at least one condition (merchant, description, amount or card)."
    if category_id is None or session.get(Category, category_id) is None:
        errors["category_id"] = "Choose a category."
    if account_id is not None and session.get(Account, account_id) is None:
        errors["account_id"] = "That card does not exist."
    if errors:
        raise RuleValidationError(errors)
    return {
        "category_id": category_id,
        "merchant_pattern": merchant_pattern,
        "description_pattern": description_pattern,
        "match_type": match_type,
        "amount_min": low,
        "amount_max": high,
        "account_id": account_id,
    }


@dataclass(frozen=True, slots=True)
class CreateRuleResult:
    rule: Rule
    applied: RecategorizeResult | None


def create_rule(
    session: Session,
    *,
    category_id: int,
    name: str | None = None,
    merchant_pattern: str | None = None,
    description_pattern: str | None = None,
    match_type: str = "contains",
    amount_min: Any = None,
    amount_max: Any = None,
    account_id: int | None = None,
    priority: int = 100,
    is_active: bool = True,
    is_system: bool = False,
    source_transaction_id: int | None = None,
    apply_to_existing: bool = False,
) -> CreateRuleResult:
    """Validate and insert a rule (CAT-3); optionally apply it to past transactions.

    With ``apply_to_existing``, every non-user-edited transaction the new rule matches is
    re-categorized with the full precedence (so payments stay payments and higher-priority rules
    still win). Takes the ledger lock first in that case. The caller commits.
    """
    if apply_to_existing:
        ledger_lock(session)
    values = validate_rule(
        session,
        category_id=category_id,
        merchant_pattern=merchant_pattern,
        description_pattern=description_pattern,
        match_type=match_type,
        amount_min=amount_min,
        amount_max=amount_max,
        account_id=account_id,
    )
    rule = Rule(
        name=(name or "").strip() or None,
        priority=priority,
        is_active=is_active,
        is_system=is_system,
        source_transaction_id=source_transaction_id,
        **values,
    )
    session.add(rule)
    session.flush()
    applied = None
    if apply_to_existing and is_active:
        matching: list[int] = []
        matching_user = 0
        for txn in session.scalars(select(Transaction).order_by(Transaction.id)):
            if rule_matches(rule, txn):
                if txn.category_source == "user":
                    matching_user += 1
                else:
                    matching.append(txn.id)
        result = recategorize(session, transaction_ids=matching)
        applied = dataclasses.replace(result, skipped_user=matching_user)
    return CreateRuleResult(rule, applied)
