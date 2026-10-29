"""Categories (CAT-1) and the Plaid/Amex category mapping editor (CAT-2) — ARCHITECTURE §5–§6.

* :func:`list_categories`: every category with its usage: transactions (all time, from
  ``v_allocations``, so split parts count in their own categories and suspected duplicates don't)
  and spending in the current budget period through ``spending.period_spending`` (the one
  definition). Hidden categories are listed too: they only leave pickers and budget lists, their
  money always counts.
* :func:`create_category`, :func:`update_category` (rename, kind, colour, icon, sort order) and
  :func:`set_hidden`. Names are unique ignoring case, exactly like the database's unique index on
  ``lower(name)``, and a clash is a friendly :class:`CategoryError`, never an ``IntegrityError``.
  The system categories (Uncategorized, Card Payments) keep their kind and can't be hidden.
* :func:`merge_preview` / :func:`merge_categories`: ONE transaction, under the ledger lock, that
  re-points every reference — transactions, split parts, rules, Plaid/Amex mappings, budgets,
  alert events and the category ids inside tombstones' ``user_state`` — then deletes the source.
  Categories are never deleted any other way. System categories can't be merged away.
* :func:`list_mappings` / :func:`set_mapping`: the "Plaid & Amex categories" editor. Plaid PFC v2
  codes are grouped by their primary code, Amex CSV ``Group-Subgroup`` values by their group;
  each row has its transaction count, its own mapping (or none: then the group's mapping, else
  Uncategorized, applies) and ``|credit`` keys (tried first for credits) are shown as such.
  External categories found in transactions but not mapped (for example Amex subgroups nobody
  has verified, such as ``Other-Charities``) are listed so they can be mapped. A mapping change
  affects new transactions; past ones change only when categories are re-applied (CAT-4), and
  never where the user set the category.

Services never commit; the route or command does.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.db import ledger_lock
from budgetapp.models import (
    CATEGORY_KINDS,
    MAPPING_SOURCES,
    Account,
    AlertEvent,
    Budget,
    Category,
    CategoryMapping,
    Rule,
    Statement,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
    get_app_settings,
    v_allocations,
)
from budgetapp.money import ZERO
from budgetapp.periods import Period, budget_period_for
from budgetapp.services import spending, userstate
from budgetapp.services.categorize import CREDIT_KEY_SUFFIX, amex_category_keys, looks_like_fee

#: How each kind is explained to the user (Categories page, pickers).
KIND_LABELS: dict[str, str] = {
    "expense": "Spending",
    "income": "Credits & income",
    "transfer": "Card payments & transfers",
}
KIND_HELP: dict[str, str] = {
    "expense": "Counts toward budgets and spending totals. Refunds in it reduce its spending.",
    "income": "Not spending: cash back, rewards and other credits.",
    "transfer": "Not spending: paying the card and moving money.",
}
#: The theme-aware palette slots (``components/README.md`` §3.3) and the neutral grey.
PALETTE: tuple[str, ...] = tuple(f"cat-{n}" for n in range(1, 17))
NEUTRAL_COLOR = "other"
#: What the editor's swatches are called (screen readers, tooltips); true in both themes.
COLOR_NAMES: dict[str, str] = {
    "cat-1": "Blue",
    "cat-2": "Orange",
    "cat-3": "Green",
    "cat-4": "Amber",
    "cat-5": "Pink",
    "cat-6": "Forest green",
    "cat-7": "Violet",
    "cat-8": "Red",
    "cat-9": "Cyan",
    "cat-10": "Brown",
    "cat-11": "Magenta",
    "cat-12": "Olive",
    "cat-13": "Indigo",
    "cat-14": "Rose",
    "cat-15": "Gold",
    "cat-16": "Teal",
    NEUTRAL_COLOR: "Grey",
}
MAX_NAME_LENGTH = 60
MAX_ICON_LENGTH = 16
_HEX_COLOR = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})")
_NAME_INDEX = "uq_categories_lower_name"
_UNSET: Any = object()


class CategoryNotFound(LookupError):
    """No category with that id (the route answers 404)."""


class CategoryError(ValueError):
    """A category change that can't be made; ``errors`` maps a form field to a message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(" ".join(self.errors.values()))


# --------------------------------------------------------------------------- read models


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """A category as pages show it (``ui.category_badge`` reads name, color and is_hidden)."""

    id: int
    name: str
    kind: str
    color: str | None
    icon: str | None
    is_hidden: bool
    system_key: str | None

    @classmethod
    def of(cls, category: Category) -> CategoryRef:
        return cls(
            category.id,
            category.name,
            category.kind,
            category.color,
            category.icon,
            category.is_hidden,
            category.system_key,
        )

    @property
    def is_system(self) -> bool:
        return self.system_key is not None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True, slots=True)
class CategoryUsage:
    """One row of the Categories page."""

    id: int
    name: str
    kind: str
    color: str | None
    icon: str | None
    is_hidden: bool
    system_key: str | None
    sort_order: int
    #: Transactions with money in this category, all time (split parts count where they go).
    transaction_count: int
    #: Net spending in the current budget period (``period_spending``); ZERO for other kinds.
    period_amount: Decimal

    @property
    def is_system(self) -> bool:
        return self.system_key is not None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def counts_as_spending(self) -> bool:
        return self.kind == "expense"


@dataclass(frozen=True, slots=True)
class CategoryOverview:
    """Every category (regular ones first, then the two system categories) and the period."""

    period: Period
    categories: tuple[CategoryUsage, ...]
    #: ``period_spending(...).total``: every expense category, hidden ones included.
    period_total: Decimal

    @property
    def hidden_count(self) -> int:
        return sum(1 for c in self.categories if c.is_hidden)


# --------------------------------------------------------------------------- reading


def get_category(session: Session, category_id: int) -> Category:
    """The category, or :class:`CategoryNotFound`."""
    category = session.get(Category, category_id)
    if category is None:
        raise CategoryNotFound(category_id)
    return category


def all_categories(session: Session) -> list[Category]:
    """Every category in display order: regular ones by sort order and name, then system ones."""
    return list(
        session.scalars(
            select(Category).order_by(
                Category.system_key.is_not(None),
                Category.sort_order,
                func.lower(Category.name),
                Category.id,
            )
        )
    )


def current_period(session: Session, *, today: dt.date | None = None) -> Period:
    """The budget period containing ``today`` (the setting's calendar month or statement cycle).

    Statement mode uses the budget card's stored closing dates and closing day, like the Budgets
    page; without them it falls back to the calendar month.
    """
    day = today or clock.today()
    settings = get_app_settings(session)
    if settings.budget_period_mode != "statement" or settings.budget_cycle_account_id is None:
        return budget_period_for(day, mode="calendar")
    account = session.get(Account, settings.budget_cycle_account_id)
    rows = session.execute(
        select(Statement.closing_date, Statement.period_start_override)
        .where(Statement.account_id == settings.budget_cycle_account_id)
        .order_by(Statement.closing_date)
    ).all()
    return budget_period_for(
        day,
        mode="statement",
        closings=[r.closing_date for r in rows],
        closing_day=account.statement_closing_day if account is not None else None,
        first_period_start=rows[0].period_start_override if rows else None,
    )


def list_categories(session: Session, *, today: dt.date | None = None) -> CategoryOverview:
    """Every category with its transaction count and spending this budget period."""
    period = current_period(session, today=today)
    spent = spending.period_spending(session, period)
    counts = dict(
        session.execute(
            select(
                v_allocations.c.category_id,
                func.count(v_allocations.c.transaction_id.distinct()),
            ).group_by(v_allocations.c.category_id)
        ).all()
    )
    rows = tuple(
        CategoryUsage(
            id=c.id,
            name=c.name,
            kind=c.kind,
            color=c.color,
            icon=c.icon,
            is_hidden=c.is_hidden,
            system_key=c.system_key,
            sort_order=c.sort_order,
            transaction_count=int(counts.get(c.id, 0)),
            period_amount=spent.amount_for(c.id) if c.kind == "expense" else ZERO,
        )
        for c in all_categories(session)
    )
    return CategoryOverview(period, rows, spent.total)


def get_usage(session: Session, category_id: int, *, today: dt.date | None = None) -> CategoryUsage:
    """One row of :func:`list_categories` (for re-rendering a single table row)."""
    for row in list_categories(session, today=today).categories:
        if row.id == category_id:
            return row
    raise CategoryNotFound(category_id)


# --------------------------------------------------------------------------- validation


def normalize_color(value: str | None) -> str | None:
    """``cat-1`` … ``cat-16``, ``other`` or a ``#rrggbb`` hex, lower-cased; ``None`` if blank."""
    if value is None:
        return None
    color = value.strip().lower()
    if not color:
        return None
    if color in PALETTE or color == NEUTRAL_COLOR or _HEX_COLOR.fullmatch(color):
        return color
    raise CategoryError({"color": "Choose one of the colours."})


def suggest_color(session: Session) -> str:
    """The palette slot used by the fewest visible categories (the lowest slot on a tie)."""
    used: dict[str, int] = dict.fromkeys(PALETTE, 0)
    for color in session.scalars(select(Category.color).where(Category.is_hidden.is_(False))):
        key = (color or "").strip().lower()
        if key in used:
            used[key] += 1
    return min(PALETTE, key=lambda slot: (used[slot], PALETTE.index(slot)))


def _clean_name(value: str | None, errors: dict[str, str]) -> str:
    name = " ".join((value or "").split())
    if not name:
        errors["name"] = "Enter a name."
    elif len(name) > MAX_NAME_LENGTH:
        errors["name"] = f"Keep the name under {MAX_NAME_LENGTH} characters."
    return name


def _clean_icon(value: str | None, errors: dict[str, str]) -> str | None:
    icon = (value or "").strip()
    if not icon:
        return None
    if len(icon) > MAX_ICON_LENGTH or not icon.isprintable():
        errors["icon"] = "Use one emoji or a few characters."
    return icon


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> Category | None:
    """A category whose ``lower(name)`` equals this one's (the unique index's own comparison)."""
    query = select(Category).where(func.lower(Category.name) == func.lower(name))
    if exclude_id is not None:
        query = query.where(Category.id != exclude_id)
    return session.scalars(query.limit(1)).first()


def _taken_message(existing: Category) -> str:
    return f"There is already a category named “{existing.name}”."


def _save(session: Session, category: Category, values: dict[str, Any], *, new: bool) -> None:
    """Write ``values`` in a savepoint; a name clash that slipped past the check is friendly."""
    try:
        with session.begin_nested():
            if new:
                session.add(category)
            for field, value in values.items():
                setattr(category, field, value)
            session.flush()
    except IntegrityError as exc:
        diag = getattr(exc.orig, "diag", None)
        if getattr(diag, "constraint_name", None) == _NAME_INDEX:
            raise CategoryError(
                {"name": f"There is already a category named “{values.get('name', '')}”."}
            ) from None
        raise


# --------------------------------------------------------------------------- changes


def create_category(
    session: Session,
    name: str,
    kind: str = "expense",
    color: str | None = None,
    icon: str | None = None,
) -> Category:
    """Add a category (last in the list). A blank colour picks the least-used palette slot."""
    errors: dict[str, str] = {}
    clean_name = _clean_name(name, errors)
    if kind not in CATEGORY_KINDS:
        errors["kind"] = "Choose what kind of category this is."
    try:
        clean_color = normalize_color(color)
    except CategoryError as exc:
        errors.update(exc.errors)
        clean_color = None
    clean_icon = _clean_icon(icon, errors)
    if "name" not in errors:
        existing = _name_taken(session, clean_name)
        if existing is not None:
            errors["name"] = _taken_message(existing)
    if errors:
        raise CategoryError(errors)
    last = session.scalar(
        select(func.max(Category.sort_order)).where(Category.system_key.is_(None))
    )
    category = Category()
    _save(
        session,
        category,
        {
            "name": clean_name,
            "kind": kind,
            "color": clean_color or suggest_color(session),
            "icon": clean_icon,
            "sort_order": (last or 0) + 10,
        },
        new=True,
    )
    return category


def update_category(
    session: Session,
    category_id: int,
    *,
    name: str = _UNSET,
    kind: str = _UNSET,
    color: str | None = _UNSET,
    icon: str | None = _UNSET,
    sort_order: int = _UNSET,
) -> Category:
    """Rename, change kind, colour, icon or sort order (only the fields given).

    System categories keep their kind (Uncategorized counts as spending, Card Payments never does).
    """
    category = get_category(session, category_id)
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    if name is not _UNSET:
        values["name"] = _clean_name(name, errors)
        if "name" not in errors:
            existing = _name_taken(session, values["name"], exclude_id=category.id)
            if existing is not None:
                errors["name"] = _taken_message(existing)
    if kind is not _UNSET:
        if kind not in CATEGORY_KINDS:
            errors["kind"] = "Choose what kind of category this is."
        elif category.system_key is not None and kind != category.kind:
            errors["kind"] = f"“{category.name}” is built in and keeps its kind."
        else:
            values["kind"] = kind
    if color is not _UNSET:
        try:
            values["color"] = normalize_color(color)
        except CategoryError as exc:
            errors.update(exc.errors)
    if icon is not _UNSET:
        values["icon"] = _clean_icon(icon, errors)
    if sort_order is not _UNSET:
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            errors["sort_order"] = "Enter a whole number."
        elif abs(sort_order) > 1_000_000_000:
            errors["sort_order"] = "Enter a smaller number."
        else:
            values["sort_order"] = sort_order
    if errors:
        raise CategoryError(errors)
    _save(session, category, values, new=False)
    return category


def set_hidden(session: Session, category_id: int, hidden: bool) -> Category:
    """Hide a category from pickers and budget lists (its money still counts everywhere)."""
    category = get_category(session, category_id)
    if hidden and category.system_key is not None:
        raise CategoryError({"is_hidden": f"“{category.name}” is built in and can't be hidden."})
    category.is_hidden = bool(hidden)
    session.flush()
    return category


# --------------------------------------------------------------------------- merging


@dataclass(frozen=True, slots=True)
class MergeCounts:
    """What a merge moves (or dropped, for budgets the target already has)."""

    transactions: int
    splits: int
    rules: int
    mappings: int
    budgets_moved: int
    budgets_dropped: int
    alerts: int
    tombstones: int

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.transactions,
                self.splits,
                self.rules,
                self.mappings,
                self.budgets_moved,
                self.budgets_dropped,
                self.alerts,
                self.tombstones,
            )
        )


@dataclass(frozen=True, slots=True)
class MergePreview:
    source: CategoryRef
    target: CategoryRef
    counts: MergeCounts

    @property
    def kind_changes(self) -> bool:
        """The source's money starts counting as the target's kind (e.g. no longer spending)."""
        return self.source.kind != self.target.kind


@dataclass(frozen=True, slots=True)
class MergeResult:
    source: CategoryRef  # deleted
    target: CategoryRef
    counts: MergeCounts


def _state_mentions(state: dict[str, Any] | None, category_id: int) -> bool:
    if not state:
        return False
    if state.get("category_id") == category_id:
        return True
    return any(s.get("category_id") == category_id for s in state.get("splits") or ())


def _tombstones_mentioning(session: Session, category_id: int) -> list[TransactionTombstone]:
    return [
        t
        for t in session.scalars(
            select(TransactionTombstone).where(TransactionTombstone.user_state.is_not(None))
        )
        if _state_mentions(t.user_state, category_id)
    ]


def _count(session: Session, column, category_id: int) -> int:
    return int(session.scalar(select(func.count()).where(column == category_id)) or 0)


def _merge_pair(
    session: Session, source_id: int, target_id: int | None
) -> tuple[Category, Category]:
    source = get_category(session, source_id)
    if source.system_key is not None:
        raise CategoryError(
            {"target_id": f"“{source.name}” is built in and can't be merged into another category."}
        )
    if target_id is None:
        raise CategoryError({"target_id": "Choose the category to merge into."})
    if target_id == source.id:
        raise CategoryError(
            {"target_id": "Choose a different category: it can't merge into itself."}
        )
    target = session.get(Category, target_id)
    if target is None:
        raise CategoryError({"target_id": "That category no longer exists. Choose another one."})
    return source, target


def _merge_counts(session: Session, source: Category, target: Category) -> MergeCounts:
    budgets = _count(session, Budget.category_id, source.id)
    target_has_budgets = _count(session, Budget.category_id, target.id) > 0
    return MergeCounts(
        transactions=_count(session, Transaction.category_id, source.id),
        splits=_count(session, TransactionSplit.category_id, source.id),
        rules=_count(session, Rule.category_id, source.id),
        mappings=_count(session, CategoryMapping.category_id, source.id),
        budgets_moved=0 if target_has_budgets else budgets,
        budgets_dropped=budgets if target_has_budgets else 0,
        alerts=_count(session, AlertEvent.category_id, source.id),
        tombstones=len(_tombstones_mentioning(session, source.id)),
    )


def merge_preview(session: Session, source_id: int, target_id: int | None) -> MergePreview:
    """What :func:`merge_categories` would move, without changing anything."""
    source, target = _merge_pair(session, source_id, target_id)
    return MergePreview(
        CategoryRef.of(source), CategoryRef.of(target), _merge_counts(session, source, target)
    )


def merge_categories(session: Session, source_id: int, target_id: int) -> MergeResult:
    """Merge ``source`` into ``target`` and delete ``source`` (one transaction, ledger lock first).

    Every reference moves: transactions (their category source, rule and user edits stay as they
    were), split parts, rules, Plaid/Amex mappings, alert events, and category ids inside
    tombstones' ``user_state`` (so an edit carried to a future replacement row lands in
    ``target``). Budgets: the target keeps its own budget rows; the source's rows move only when
    the target has none at all, and are dropped otherwise (moving them could replace the target's
    amount from a later date, or give one category both a monthly and an annual budget).

    Expires every object in the session afterwards (their references moved). The caller commits.
    """
    ledger_lock(session)
    source, target = _merge_pair(session, source_id, target_id)
    counts = _merge_counts(session, source, target)
    result = MergeResult(CategoryRef.of(source), CategoryRef.of(target), counts)
    s, t = source.id, target.id
    for column in (
        Transaction.category_id,
        TransactionSplit.category_id,
        Rule.category_id,
        CategoryMapping.category_id,
        AlertEvent.category_id,
    ):
        session.execute(
            update(column.class_).where(column == s).values({column.key: t}),
            execution_options={"synchronize_session": False},
        )
    if counts.budgets_dropped:
        session.execute(
            delete(Budget).where(Budget.category_id == s),
            execution_options={"synchronize_session": False},
        )
    else:
        session.execute(
            update(Budget).where(Budget.category_id == s).values(category_id=t),
            execution_options={"synchronize_session": False},
        )
    for tombstone in _tombstones_mentioning(session, s):
        tombstone.user_state = userstate.remap_category_ids(tombstone.user_state, s, t)
    session.flush()
    session.execute(delete(Category).where(Category.id == s))
    session.expire_all()
    return result


# --------------------------------------------------------------------------- mappings (CAT-2)

SOURCE_LABELS: dict[str, str] = {"plaid_pfc": "Plaid", "amex_csv": "Amex CSV"}
#: The 18 primary codes of Plaid's personal finance categories, v2 (the seeded taxonomy).
PFC_PRIMARIES: tuple[str, ...] = (
    "BANK_FEES",
    "ENTERTAINMENT",
    "FOOD_AND_DRINK",
    "GENERAL_MERCHANDISE",
    "GENERAL_SERVICES",
    "GOVERNMENT_AND_NON_PROFIT",
    "HOME_IMPROVEMENT",
    "INCOME",
    "LOAN_DISBURSEMENTS",
    "LOAN_PAYMENTS",
    "MEDICAL",
    "OTHER",
    "PERSONAL_CARE",
    "RENT_AND_UTILITIES",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "TRANSPORTATION",
    "TRAVEL",
)
_ACRONYMS = frozenset({"ATM", "BNPL", "EWA", "TV"})
_PLAID_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,99}")
MAX_AMEX_KEY_LENGTH = 120


class MappingError(ValueError):
    """A mapping change that can't be made (the message is for the user)."""


@dataclass(frozen=True, slots=True)
class MappingRow:
    """One external category and where it goes."""

    source: str
    key: str  #: the mapping key (as stored, or as found in transactions when not mapped)
    code: str  #: the key without the ``|credit`` suffix (what Plaid or Amex calls it)
    label: str
    group: str
    is_group: bool  #: the group's own key (the fallback for its other codes)
    for_credits: bool  #: a ``|credit`` key: tried first for credits (negative amounts)
    category_id: int | None  #: its own mapping; ``None`` = not mapped
    #: What applies while it has no mapping of its own (the group's), ``None`` = Uncategorized.
    fallback_category_id: int | None
    transaction_count: int

    @property
    def is_mapped(self) -> bool:
        return self.category_id is not None

    @property
    def effective_category_id(self) -> int | None:
        return self.category_id if self.category_id is not None else self.fallback_category_id


@dataclass(frozen=True, slots=True)
class MappingGroup:
    key: str
    label: str
    rows: tuple[MappingRow, ...]

    @property
    def transaction_count(self) -> int:
        return sum(r.transaction_count for r in self.rows)


@dataclass(frozen=True, slots=True)
class MappingList:
    source: str
    groups: tuple[MappingGroup, ...]
    only_used: bool
    #: Codes found in transactions that have no mapping of their own.
    unmapped_in_use: int
    #: Rows in the full list (before ``only_used``) and codes found in transactions.
    total_rows: int
    used_rows: int

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS[self.source]

    @property
    def rows(self) -> tuple[MappingRow, ...]:
        return tuple(r for g in self.groups for r in g.rows)


@dataclass(frozen=True, slots=True)
class MappingChange:
    source: str
    key: str
    label: str
    old_category_id: int | None
    new_category_id: int | None
    #: Transactions with this external category (they change only when categories are re-applied).
    transaction_count: int


def _check_source(source: str) -> None:
    if source not in MAPPING_SOURCES:
        raise MappingError("Choose Plaid or Amex CSV categories.")


def _canon(source: str, key: str) -> str:
    """How categorization compares keys: Plaid codes exactly, Amex values ignoring case."""
    return key if source == "plaid_pfc" else key.casefold()


def _split_credit(key: str) -> tuple[str, bool]:
    if key.endswith(CREDIT_KEY_SUFFIX):
        return key[: -len(CREDIT_KEY_SUFFIX)], True
    return key, False


def _humanize(code: str) -> str:
    words = [w if w in _ACRONYMS else w.lower() for w in code.split("_") if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def _group_of(source: str, base: str, primaries: Iterable[str]) -> str:
    if source == "amex_csv":
        keys = amex_category_keys(base)
        return keys[-1] if keys else base
    best = ""
    for primary in primaries:
        if (base == primary or base.startswith(primary + "_")) and len(primary) > len(best):
            best = primary
    return best or base


def _label(source: str, base: str, group: str) -> str:
    if source == "amex_csv":
        keys = amex_category_keys(base)
        return keys[0][len(keys[1]) + 1 :] if len(keys) == 2 else f"{base} (general)"
    if base == group:
        return f"{_humanize(group)} (general)"
    return _humanize(base[len(group) + 1 :])


def _group_label(source: str, group: str) -> str:
    return group if source == "amex_csv" else _humanize(group)


def _raw_keys(source: str, row: Any) -> tuple[str, ...]:
    """The lookup keys of a transaction, most specific first (as ``Categorizer`` builds them)."""
    if source == "amex_csv":
        return amex_category_keys(row.external_category)
    return tuple(k for k in (row.pfc_detailed, row.pfc_primary) if k)


def _key_counts(session: Session, source: str, mapped: set[str]) -> dict[str, tuple[str, int]]:
    """Transactions per most specific external key: ``canonical key → (spelling, count)``.

    A credit (negative amount, not a fee reversal) counts on the key's ``|credit`` row when that
    mapping exists, because that is the mapping it uses.
    """
    txn_source = "csv" if source == "amex_csv" else "plaid"
    cols = (
        (Transaction.external_category,)
        if source == "amex_csv"
        else (Transaction.pfc_detailed, Transaction.pfc_primary)
    )
    counts: dict[str, tuple[str, int]] = {}

    def add(key: str, n: int) -> None:
        canon = _canon(source, key)
        spelling, total = counts.get(canon, (key, 0))
        counts[canon] = (spelling, total + n)

    charges = select(*cols, func.count().label("n")).where(
        Transaction.source == txn_source, Transaction.amount >= 0
    )
    for row in session.execute(charges.group_by(*cols)):
        keys = _raw_keys(source, row)
        if keys:
            add(keys[0], row.n)
    detail = (Transaction.name, Transaction.original_description, Transaction.transaction_code)
    credits = select(*cols, *detail, func.count().label("n")).where(
        Transaction.source == txn_source, Transaction.amount < 0
    )
    for row in session.execute(credits.group_by(*cols, *detail)):
        keys = _raw_keys(source, row)
        if not keys:
            continue
        credit_key = keys[0] + CREDIT_KEY_SUFFIX
        fee = looks_like_fee(
            SimpleNamespace(
                name=row.name,
                original_description=row.original_description,
                transaction_code=row.transaction_code,
            )
        )
        add(credit_key if not fee and _canon(source, credit_key) in mapped else keys[0], row.n)
    return counts


def _first_mapped(source: str, keys: Iterable[str], mapped: Mapping[str, int]) -> int | None:
    for key in keys:
        category_id = mapped.get(_canon(source, key))
        if category_id is not None:
            return category_id
    return None


def list_mappings(session: Session, source: str, *, only_used: bool = False) -> MappingList:
    """Every external category of ``source`` with its count and mapping, grouped.

    The list holds the stored mappings plus every code found in transactions (mapped or not) and
    each group's own row. With ``only_used``, it keeps the codes found in transactions and the
    group rows that unmapped ones fall back to.
    """
    _check_source(source)
    stored = session.scalars(
        select(CategoryMapping).where(CategoryMapping.source == source).order_by(CategoryMapping.id)
    )
    mapped: dict[str, int] = {}
    spelling: dict[str, str] = {}
    for m in stored:
        canon = _canon(source, m.external_key)
        mapped.setdefault(canon, m.category_id)
        spelling.setdefault(canon, m.external_key)
    counts = _key_counts(session, source, set(mapped))
    for canon, (key, _n) in counts.items():
        spelling.setdefault(canon, key)

    primaries = set(PFC_PRIMARIES)
    if source == "plaid_pfc":
        primaries.update(
            p
            for p in session.scalars(
                select(Transaction.pfc_primary)
                .where(Transaction.source == "plaid", Transaction.pfc_primary.is_not(None))
                .distinct()
            )
            if p
        )

    # group (canonical) -> its members (canonical keys); every group also lists its own key.
    members: dict[str, list[str]] = {}
    for canon, key in list(spelling.items()):
        group = _group_of(source, _split_credit(key)[0], primaries)
        group_canon = _canon(source, group)
        spelling.setdefault(group_canon, group)
        members.setdefault(group_canon, [])
        if canon not in members[group_canon]:
            members[group_canon].append(canon)
    for group_canon, keys in members.items():
        if group_canon not in keys:
            keys.append(group_canon)

    def make_row(canon: str, group: str) -> MappingRow:
        key = spelling[canon]
        base, credit = _split_credit(key)
        is_group = _canon(source, base) == _canon(source, group)
        if credit:
            chain = [group] if is_group else [group + CREDIT_KEY_SUFFIX, base, group]
        else:
            chain = [] if is_group else [group]
        return MappingRow(
            source=source,
            key=key,
            code=base,
            label=_label(source, base, group),
            group=group,
            is_group=is_group,
            for_credits=credit,
            category_id=mapped.get(canon),
            fallback_category_id=_first_mapped(source, chain, mapped),
            transaction_count=counts[canon][1] if canon in counts else 0,
        )

    all_rows = {
        group_canon: [make_row(canon, spelling[group_canon]) for canon in keys]
        for group_canon, keys in members.items()
    }
    flat = [r for rows in all_rows.values() for r in rows]
    built: list[MappingGroup] = []
    for group_canon, rows in all_rows.items():
        if only_used:
            needs_group = any(r.transaction_count and not r.is_mapped for r in rows)
            rows = [r for r in rows if r.transaction_count or (r.is_group and needs_group)]
            if not any(r.transaction_count for r in rows):
                continue
        rows = sorted(rows, key=lambda r: (not r.is_group, r.code.casefold(), r.for_credits))
        group = spelling[group_canon]
        built.append(MappingGroup(key=group, label=_group_label(source, group), rows=tuple(rows)))
    built.sort(key=lambda g: g.label.casefold())
    return MappingList(
        source=source,
        groups=tuple(built),
        only_used=only_used,
        unmapped_in_use=sum(1 for r in flat if r.transaction_count and not r.is_mapped),
        total_rows=len(flat),
        used_rows=sum(1 for r in flat if r.transaction_count),
    )


def normalize_mapping_key(source: str, key: str) -> str:
    """The canonical spelling of a mapping key (Amex: ``Group-Subgroup`` without spaces)."""
    _check_source(source)
    base, credit = _split_credit((key or "").strip())
    base = base.strip()
    if source == "plaid_pfc":
        if not _PLAID_KEY.fullmatch(base):
            raise MappingError("That isn't a Plaid category code.")
    else:
        keys = amex_category_keys(base)
        if not keys:
            raise MappingError("That isn't an Amex category.")
        base = keys[0]
        if len(base) > MAX_AMEX_KEY_LENGTH or not base.isprintable():
            raise MappingError("That isn't an Amex category.")
    return base + (CREDIT_KEY_SUFFIX if credit else "")


def _transactions_with_key(session: Session, source: str, key: str) -> int:
    base, _credit = _split_credit(key)
    canon = _canon(source, base)
    if source == "plaid_pfc":
        return int(
            session.scalar(
                select(func.count()).where(
                    Transaction.source == "plaid",
                    (Transaction.pfc_detailed == base)
                    | ((Transaction.pfc_detailed.is_(None)) & (Transaction.pfc_primary == base)),
                )
            )
            or 0
        )
    total = 0
    query = (
        select(Transaction.external_category, func.count())
        .where(Transaction.source == "csv", Transaction.external_category.is_not(None))
        .group_by(Transaction.external_category)
    )
    for raw, n in session.execute(query):
        keys = amex_category_keys(raw)
        if keys and _canon(source, keys[0]) == canon:
            total += n
    return total


def set_mapping(session: Session, source: str, key: str, category_id: int | None) -> MappingChange:
    """Map an external category to a category, or (``None``) remove its own mapping.

    Without its own mapping a code uses its group's, else Uncategorized. Amex keys match ignoring
    case, so an existing mapping spelled differently is updated rather than duplicated. New
    transactions use the change at once; past ones when categories are re-applied.
    """
    key = normalize_mapping_key(source, key)
    if category_id is not None and session.get(Category, category_id) is None:
        raise MappingError("That category no longer exists. Reload the page and try again.")
    canon = _canon(source, key)
    existing = [
        m
        for m in session.scalars(
            select(CategoryMapping)
            .where(CategoryMapping.source == source)
            .order_by(CategoryMapping.id)
        )
        if _canon(source, m.external_key) == canon
    ]
    old = existing[0].category_id if existing else None
    if category_id is None:
        for mapping in existing:
            session.delete(mapping)
    elif existing:
        existing[0].category_id = category_id
        for mapping in existing[1:]:
            session.delete(mapping)
    else:
        session.add(CategoryMapping(source=source, external_key=key, category_id=category_id))
    session.flush()
    base, _credit = _split_credit(key)
    primaries = set(PFC_PRIMARIES)
    group = _group_of(source, base, primaries)
    return MappingChange(
        source=source,
        key=key,
        label=_label(source, base, group),
        old_category_id=old,
        new_category_id=category_id,
        transaction_count=_transactions_with_key(session, source, key),
    )
