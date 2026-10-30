"""Transactions: search and filters, edits, tags, splits, details and CSV export (TXN-1..5).

* :class:`TransactionFilter` is parsed from (and written back to) the page's query parameters. It
  never raises: a value it cannot use is dropped and described in ``problems``.
* :func:`search` returns one page of rows plus the count and the raw sums of every matching row.
  Suspected duplicates (``duplicate_status = 'suspect'``) are left out, as from every total in the
  app, unless the filter asks for them (``show=duplicates``).
* The date shown, filtered and sorted on is ``COALESCE(authorized_date, date)`` (the spend date).
* A category filter follows ``v_allocations``: a row whose splits add up to its amount matches
  through its split categories, any other row through its own category.
* Edits (:func:`update`, :func:`reset_to_automatic`, :func:`set_splits`, :func:`clear_splits`,
  :func:`clear_review`, :func:`create_one_click_rule`) take the ledger lock first and never commit;
  the caller (a route) commits. Invalid input raises :class:`TransactionEditError` with a message
  per field; a missing row raises :class:`TransactionNotFound`.
* :func:`export_csv` streams the filtered rows as CSV (TXN-5) with spreadsheet formulas
  neutralised.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    exists,
    func,
    literal,
    not_,
    or_,
    select,
)
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.db import ledger_lock
from budgetapp.models import (
    Account,
    Category,
    CsvImport,
    Rule,
    Statement,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
    get_app_settings,
)
from budgetapp.money import ZERO, format_money, is_cents, parse_money_input, to_money
from budgetapp.services import categorize, spending, userstate

PER_PAGE = 50
MAX_PER_PAGE = 200

SORTS = ("-date", "date", "-amount", "amount")
DEFAULT_SORT = "-date"

#: Date presets (``range`` parameter).
RANGES = ("all", "period", "last_month", "90d", "year", "custom")
STATUSES = ("all", "pending", "posted")
#: Which rows: everything counted (default), rows that need review, or suspected duplicates.
SHOWS = ("all", "review", "duplicates")

UNCATEGORIZED_TOKEN = "uncategorized"
MAX_CATEGORY_TOKENS = 50
MAX_SEARCH_TERMS = 8

MAX_NOTES_LENGTH = 2000
MAX_TAG_LENGTH = 40
MAX_TAGS = 20
MAX_SPLIT_PARTS = 12
MAX_SPLIT_NOTE_LENGTH = 200
#: ``rules.priority`` when nothing needs outranking (the column default).
DEFAULT_RULE_PRIORITY = 100

#: How the source of a category is shown (``system`` is the fixed card-payment step).
CATEGORY_SOURCE_LABELS = {
    "user": "You",
    "system": "Card payment",
    "rule": "Rule",
    "plaid": "Plaid category",
    "amex": "Amex category",
    "default": "Automatic",
}

EXPORT_COLUMNS = (
    "Date",
    "Authorized date",
    "Posted date",
    "Description",
    "Merchant",
    "Category",
    "Amount",
    "Pending",
    "Account",
    "Card member",
    "Notes",
    "Tags",
    "Source",
)
#: A cell starting with one of these is read as a formula by spreadsheet apps (OWASP CSV
#: injection); it gets a leading apostrophe. The numeric Amount column is written as is.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Payment processors that put their own prefix before the merchant ("SQ *DELFINA").
PROCESSOR_PREFIXES = frozenset(
    "AMZ BT CKE CLR DD EB FSP GGL GOOGLE IC IN LS PAYPAL PG PP PY SP SPO SQ SQU TM TST WPY "  # noqa: SIM905
    "ZETTLE".split()
)
_DOMAIN_TOKEN = re.compile(r"\.(COM|NET|ORG|CO|IO|US)\b|^WWW\.", re.IGNORECASE)
_STATE_CODE = re.compile(r"^[A-Z]{2}$")
#: Amex pads the merchant part of a CSV Description before the city ("BLUE BOTTLE COFFEE     SAN
#: FRANCISCO CA"); the first run of two or more spaces ends the merchant.
_PADDING = re.compile(r"\s{2,}")
#: C0 control characters and DEL, except tab and newline: invisible on the page, and PostgreSQL
#: refuses NUL in text, so user text and URL parameters are cleaned of them first.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: Whitespace runs as ``str.split`` sees them (SQL side of the merchant filter).
_SQL_WHITESPACE = "[\\s\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"


def clean_text(value: Any) -> str:
    """``value`` as text without control characters (tab and newline stay; CR LF becomes LF)."""
    text = "" if value is None else str(value)
    return _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def parse_id(value: Any) -> int | None:
    """A database id from text (ASCII digits, 1 to 2**63 − 1), else ``None``.

    ``str.isdigit`` alone is not enough: it accepts "²", which ``int`` refuses.
    """
    text = str(value).strip() if value is not None else ""
    if not (text.isascii() and text.isdecimal()):
        return None
    number = int(text)
    return number if 0 < number < 2**63 else None


class TransactionNotFound(LookupError):
    """No transaction has that id (it may have been removed by a sync)."""


class TransactionEditError(ValueError):
    """Invalid edit; ``errors`` maps a field name to a message written for the user."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: "Leave this field alone" for :func:`update`.
UNSET: Any = _Unset()


# --------------------------------------------------------------------------- the filter


def _getlist(params: Any, key: str) -> list[str]:
    """Every value of ``key`` as text, without control characters (see :func:`clean_text`)."""
    if hasattr(params, "getlist"):
        values = list(params.getlist(key))
    elif isinstance(params, list | tuple):  # (key, value) pairs, e.g. TransactionFilter.to_params()
        values = [v for k, v in params if k == key]
    else:
        value = params.get(key) if isinstance(params, Mapping) else None
        if value is None:
            values = []
        elif isinstance(value, list | tuple):
            values = list(value)
        else:
            values = [value]
    return [clean_text(v) for v in values]


def _get(params: Any, key: str) -> str:
    """The last non-empty value of ``key`` (stripped), else ``""``."""
    for value in reversed(_getlist(params, key)):
        if value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True, slots=True)
class TransactionFilter:
    """What the list shows: every field is optional; the defaults show everything counted.

    Query parameters: ``q``, ``range`` (``all``, ``period``, ``last_month``, ``90d``, ``year``,
    ``custom``), ``from``/``to`` (ISO dates, ``custom`` only), ``category`` (repeatable: an id or
    ``uncategorized``), ``merchant``, ``min``/``max`` (the signed amount as shown: charges
    positive, credits negative), ``status`` (``pending``/``posted``), ``account``, ``tag``,
    ``member`` (card member), ``credits`` (``1``) and ``show`` (``review``/``duplicates``).
    """

    q: str = ""
    range: str = "all"
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    categories: tuple[str, ...] = ()
    merchant: str = ""
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    status: str = "all"
    account_id: int | None = None
    tag: str = ""
    member: str = ""
    credits_only: bool = False
    show: str = "all"
    problems: tuple[str, ...] = ()

    @classmethod
    def from_params(cls, params: Any) -> TransactionFilter:
        """Parse query parameters: Starlette ``QueryParams``, a dict of strings or lists, or
        ``(key, value)`` pairs."""
        problems: list[str] = []

        q = " ".join(_get(params, "q").split())[:200]

        date_from = _parse_date(_get(params, "from"), "start date", problems)
        date_to = _parse_date(_get(params, "to"), "end date", problems)
        preset = _get(params, "range").lower()
        if preset not in RANGES:
            preset = "custom" if (date_from or date_to) else "all"
        if preset != "custom":
            date_from = date_to = None
        elif date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from

        tokens: list[str] = []
        for raw in _getlist(params, "category"):
            token = raw.strip().lower()
            if token != UNCATEGORIZED_TOKEN:
                category_id = parse_id(token)
                if category_id is None:
                    continue
                token = str(category_id)
            if token not in tokens:
                tokens.append(token)
        tokens = tokens[:MAX_CATEGORY_TOKENS]

        amount_min = _parse_amount(_get(params, "min"), "minimum amount", problems)
        amount_max = _parse_amount(_get(params, "max"), "maximum amount", problems)
        if amount_min is not None and amount_max is not None and amount_min > amount_max:
            amount_min, amount_max = amount_max, amount_min

        status = _get(params, "status").lower()
        if status not in STATUSES:
            status = "all"
        show = _get(params, "show").lower()
        if show not in SHOWS:
            show = "all"

        account_id = parse_id(_get(params, "account"))

        credits_only = _get(params, "credits").lower() in ("1", "true", "on", "yes")
        if credits_only and amount_min is not None and amount_min > 0:
            # Amounts are signed as shown (credits are negative), so this can never match.
            problems.append(
                f"Credits are negative amounts (shown like −$54.99), so “Credits only” with an "
                f"amount from {format_money(amount_min)} finds nothing. Type credit amounts with "
                "a minus sign, for example from −50 to −10."
            )
        return cls(
            q=q,
            range=preset,
            date_from=date_from,
            date_to=date_to,
            categories=tuple(tokens),
            merchant=" ".join(_get(params, "merchant").split())[:200],
            amount_min=amount_min,
            amount_max=amount_max,
            status=status,
            account_id=account_id,
            tag=" ".join(_get(params, "tag").split())[: MAX_TAG_LENGTH * 2],
            member=_get(params, "member")[:200],
            credits_only=credits_only,
            show=show,
            problems=tuple(problems),
        )

    def to_params(self) -> list[tuple[str, str]]:
        """The non-default values as query parameters, in a stable order."""
        out: list[tuple[str, str]] = []
        if self.q:
            out.append(("q", self.q))
        if self.range != "all":
            out.append(("range", self.range))
        if self.range == "custom":
            if self.date_from:
                out.append(("from", self.date_from.isoformat()))
            if self.date_to:
                out.append(("to", self.date_to.isoformat()))
        out.extend(("category", token) for token in self.categories)
        if self.merchant:
            out.append(("merchant", self.merchant))
        if self.amount_min is not None:
            out.append(("min", str(self.amount_min)))
        if self.amount_max is not None:
            out.append(("max", str(self.amount_max)))
        if self.status != "all":
            out.append(("status", self.status))
        if self.account_id is not None:
            out.append(("account", str(self.account_id)))
        if self.tag:
            out.append(("tag", self.tag))
        if self.member:
            out.append(("member", self.member))
        if self.credits_only:
            out.append(("credits", "1"))
        if self.show != "all":
            out.append(("show", self.show))
        return out

    def replace(self, **changes: Any) -> TransactionFilter:
        return dataclasses.replace(self, **changes)

    def without_unknown_categories(self, known_ids: Iterable[int]) -> TransactionFilter:
        """This filter without the category ids that no longer exist (a bookmark or link from
        before a category was merged away), and a note in ``problems`` saying so. Otherwise the
        list would be narrowed by a category the picker cannot show."""
        known = {str(i) for i in known_ids}
        kept = tuple(t for t in self.categories if t == UNCATEGORIZED_TOKEN or t in known)
        dropped = len(self.categories) - len(kept)
        if not dropped:
            return self
        note = (
            "Ignored a category that no longer exists (it may have been merged into another)."
            if dropped == 1
            else f"Ignored {dropped} categories that no longer exist (they may have been merged "
            "into others)."
        )
        return self.replace(categories=kept, problems=(*self.problems, note))

    @property
    def is_default(self) -> bool:
        """True when nothing narrows the list (the ``show`` view aside)."""
        return not [p for p in self.to_params() if p[0] != "show"]

    @property
    def advanced_count(self) -> int:
        """How many of the filters behind "More filters" are set."""
        return sum(
            (
                bool(self.merchant),
                self.amount_min is not None,
                self.amount_max is not None,
                self.account_id is not None,
                bool(self.tag),
                bool(self.member),
                self.credits_only,
            )
        )


def _parse_date(text: str, label: str, problems: list[str]) -> dt.date | None:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        problems.append(f"Ignored the {label} “{text[:40]}”: it isn't a date.")
        return None


def _parse_amount(text: str, label: str, problems: list[str]) -> Decimal | None:
    if not text:
        return None
    try:
        return parse_money_input(text)
    except TypeError, ValueError:
        problems.append(f"Ignored the {label} “{text[:40]}”: it isn't an amount.")
        return None


def parse_sort(value: str | None) -> str:
    """A valid sort key (``-date`` newest first by default)."""
    return value if value in SORTS else DEFAULT_SORT


def parse_page(value: str | None) -> int:
    """A page number ≥ 1 (anything else is page 1)."""
    number = parse_id(value)
    return number if number is not None and number < 10**6 else 1


# --------------------------------------------------------------------------- reference data


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """What pages need of a category (the fields ``ui.category_badge`` and pickers read)."""

    id: int
    name: str
    color: str | None
    icon: str | None
    is_hidden: bool
    kind: str
    system_key: str | None

    @property
    def is_card_payments(self) -> bool:
        return self.system_key == "card_payments"


def load_categories(session: Session) -> dict[int, CategoryRef]:
    """Every category by id, in picker order (``sort_order``, then name)."""
    rows = session.execute(
        select(
            Category.id,
            Category.name,
            Category.color,
            Category.icon,
            Category.is_hidden,
            Category.kind,
            Category.system_key,
        ).order_by(Category.sort_order, func.lower(Category.name), Category.id)
    )
    return {r.id: CategoryRef(*r) for r in rows}


@dataclass(frozen=True, slots=True)
class AccountRef:
    id: int
    name: str
    is_manual: bool


@dataclass(frozen=True, slots=True)
class FilterOptions:
    """Choices for the filter bar."""

    categories: tuple[CategoryRef, ...]
    accounts: tuple[AccountRef, ...]
    members: tuple[str, ...]
    tags: tuple[str, ...]
    merchants: tuple[str, ...]
    period_label: str


def all_tags(session: Session) -> list[str]:
    """Every tag in use, de-duplicated case-insensitively, most used first (autocomplete)."""
    tag = func.unnest(Transaction.tags).label("tag")
    inner = select(tag).subquery()
    rows = session.execute(
        select(inner.c.tag, func.count().label("n"))
        .group_by(inner.c.tag)
        .order_by(func.count().desc(), inner.c.tag)
    )
    return userstate.normalize_tags(r.tag for r in rows)


def filter_options(session: Session, *, today: dt.date | None = None) -> FilterOptions:
    """Categories, accounts, card members, tags and merchants for the filter bar."""
    categories = tuple(load_categories(session).values())
    accounts = tuple(
        AccountRef(a.id, a.display_name, a.is_manual)
        for a in session.scalars(select(Account).order_by(Account.id))
    )
    members = tuple(
        session.scalars(
            select(Transaction.account_owner)
            .where(Transaction.account_owner.is_not(None), Transaction.account_owner != "")
            .distinct()
            .order_by(Transaction.account_owner)
        )
    )
    merchants = tuple(
        session.scalars(
            select(Transaction.merchant_name)
            .where(Transaction.merchant_name.is_not(None), Transaction.merchant_name != "")
            .group_by(Transaction.merchant_name)
            .order_by(func.count().desc(), Transaction.merchant_name)
            .limit(300)
        )
    )
    period = budget_period(session, today or clock.today())
    return FilterOptions(
        categories=categories,
        accounts=accounts,
        members=members,
        tags=tuple(all_tags(session)),
        merchants=merchants,
        period_label=_period_option_label(period),
    )


# --------------------------------------------------------------------------- dates


@dataclass(frozen=True, slots=True)
class DateRange:
    """The dates a filter covers (``None`` = open-ended), how to say it, and which date of a
    transaction they apply to: ``spending.BASIS_SPEND_DATE`` (the date the list shows) or, for
    the budget period in statement mode, ``spending.BASIS_POSTED_DATE`` (as Budgets count it)."""

    start: dt.date | None
    end: dt.date | None
    label: str
    basis: str = spending.BASIS_SPEND_DATE


def budget_period(session: Session, today: dt.date) -> periods.Period:
    """The budget period containing ``today`` (``app_settings``: calendar month or the budget
    card's statement cycle, falling back to the calendar month when no cycle is known)."""
    settings = get_app_settings(session)
    if settings.budget_period_mode != "statement" or settings.budget_cycle_account_id is None:
        return periods.month_of(today)
    account_id = settings.budget_cycle_account_id
    statements = session.execute(
        select(Statement.closing_date, Statement.period_start_override)
        .where(Statement.account_id == account_id)
        .order_by(Statement.closing_date)
    ).all()
    closing_day = session.scalar(
        select(Account.statement_closing_day).where(Account.id == account_id)
    )
    return periods.budget_period_for(
        today,
        mode="statement",
        closings=[s.closing_date for s in statements],
        closing_day=closing_day,
        first_period_start=statements[0].period_start_override if statements else None,
    )


def _period_option_label(period: periods.Period) -> str:
    return "This month" if period.kind == periods.MONTH else "This statement period"


def _span_label(start: dt.date | None, end: dt.date | None) -> str:
    def short(d: dt.date) -> str:
        return f"{d:%b} {d.day}"

    def long(d: dt.date) -> str:
        return f"{d:%b} {d.day}, {d.year}"

    if start and end:
        if start == end:
            return long(start)
        if start.year == end.year:
            return f"{short(start)} – {long(end)}"
        return f"{long(start)} – {long(end)}"
    if start:
        return f"Since {long(start)}"
    if end:
        return f"Until {long(end)}"
    return "All dates"


def resolve_dates(
    session: Session, filt: TransactionFilter, today: dt.date | None = None
) -> DateRange:
    """The concrete dates of the filter's preset, measured from ``today``.

    Every preset covers spend dates, except the budget period (``period``): it uses the date
    basis of ``spending.period_spending`` (the posting date in statement mode), so the list shows
    exactly the rows Budgets count in that period.
    """
    today = today or clock.today()
    preset = filt.range
    if preset == "period":
        period = budget_period(session, today)
        basis = spending.basis_for_mode(get_app_settings(session).budget_period_mode)
        name = "This month" if period.kind == periods.MONTH else "This statement period"
        label = f"{name} ({period.label})"
        if basis == spending.BASIS_POSTED_DATE:
            label += ", by posting date"
        return DateRange(period.start, period.end, label, basis)
    if preset == "last_month":
        year, month = periods.add_months(today.year, today.month, -1)
        month_period = periods.month_period(year, month)
        return DateRange(month_period.start, month_period.end, f"Last month ({month_period.label})")
    if preset == "90d":
        start = today - dt.timedelta(days=89)
        return DateRange(start, None, f"Last 90 days (since {start:%b} {start.day})")
    if preset == "year":
        year_period = periods.year_of(today)
        return DateRange(year_period.start, year_period.end, f"This year ({today.year})")
    if preset == "custom":
        return DateRange(filt.date_from, filt.date_to, _span_label(filt.date_from, filt.date_to))
    return DateRange(None, None, "All dates")


# --------------------------------------------------------------------------- query building


def spend_date_expr() -> ColumnElement[dt.date]:
    """``COALESCE(authorized_date, date)``: the date shown, filtered and sorted on."""
    return func.coalesce(Transaction.authorized_date, Transaction.date)


def posted_date_expr() -> ColumnElement[dt.date]:
    """``COALESCE(posted_date, date)``: the posting date, as ``spending.basis_date`` reads it for
    statement-mode budget periods (a pending row is dated by its Plaid date)."""
    return func.coalesce(Transaction.posted_date, Transaction.date)


def basis_date_expr(basis: str) -> ColumnElement[dt.date]:
    """The transaction date a :class:`DateRange` with this ``basis`` applies to."""
    return posted_date_expr() if basis == spending.BASIS_POSTED_DATE else spend_date_expr()


def merchant_expr() -> ColumnElement[str]:
    """The merchant as ``services.spending.top_merchants`` names it (else the description)."""
    return func.coalesce(Transaction.merchant_name, Transaction.name)


def merchant_key_expr() -> ColumnElement[str]:
    """:func:`merchant_expr` lower-cased with whitespace runs collapsed and trimmed, so the
    merchant filter matches Amex CSV descriptions, which keep long runs of spaces."""
    collapsed = func.regexp_replace(merchant_expr(), _SQL_WHITESPACE, " ", "g")
    return func.lower(func.btrim(collapsed))


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_AMOUNT_TERM = re.compile(r"^[−-]?\$?\d[\d,]*(\.\d{1,2})?$")


def _amount_term(term: str) -> Decimal | None:
    """A search word that looks like an amount ("54.99", "$1,200"); matched against amounts."""
    if not ("." in term or "$" in term) or not _AMOUNT_TERM.match(term):
        return None
    try:
        return abs(parse_money_input(term))
    except ValueError:
        return None


def _search_condition(q: str) -> ColumnElement[bool] | None:
    """Every word must appear in the description, merchant, original description, notes or tags
    (case-insensitive); a word like "54.99" also matches that amount."""
    terms = q.split()[:MAX_SEARCH_TERMS]
    if not terms:
        return None
    fields = (
        Transaction.name,
        Transaction.merchant_name,
        Transaction.original_description,
        Transaction.notes,
        func.array_to_string(Transaction.tags, " "),
    )
    conditions = []
    for term in terms:
        pattern = f"%{_like_escape(term)}%"
        options: list[ColumnElement[bool]] = [f.ilike(pattern, escape="\\") for f in fields]
        amount = _amount_term(term)
        if amount is not None:
            options.append(func.abs(Transaction.amount) == amount)
        conditions.append(or_(*options))
    return and_(*conditions)


def _split_total() -> Any:
    return (
        select(func.sum(TransactionSplit.amount))
        .where(TransactionSplit.transaction_id == Transaction.id)
        .scalar_subquery()
    )


def _category_condition(category_ids: Sequence[int]) -> ColumnElement[bool]:
    """Matches rows with money in these categories, the way ``v_allocations`` allocates it."""
    in_split = exists().where(
        TransactionSplit.transaction_id == Transaction.id,
        TransactionSplit.category_id.in_(category_ids),
    )
    return case(
        (_split_total() == Transaction.amount, in_split),
        else_=Transaction.category_id.in_(category_ids),
    )


def _tag_condition(tag: str) -> ColumnElement[bool]:
    tags = func.unnest(Transaction.tags).table_valued("tag").render_derived()
    return exists(select(1).select_from(tags).where(func.lower(tags.c.tag) == tag.lower()))


def resolve_category_ids(session: Session, tokens: Iterable[str]) -> list[int]:
    """Category ids for filter tokens (``uncategorized`` is the system category)."""
    ids: list[int] = []
    for token in tokens:
        if token == UNCATEGORIZED_TOKEN:
            category_id = categorize.system_category_id(session, "uncategorized")
        else:
            category_id = int(token)
        if category_id not in ids:
            ids.append(category_id)
    return ids


def filter_conditions(
    session: Session, filt: TransactionFilter, *, today: dt.date | None = None
) -> list[ColumnElement[bool]]:
    """SQL conditions on ``transactions`` for ``filt`` (suspected duplicates excluded unless the
    filter shows only them)."""
    conditions: list[ColumnElement[bool]] = []
    if filt.show == "duplicates":
        conditions.append(Transaction.duplicate_status == "suspect")
    else:
        conditions.append(Transaction.duplicate_status != "suspect")
        if filt.show == "review":
            conditions.append(Transaction.needs_review.is_(True))

    dates = resolve_dates(session, filt, today)
    day = basis_date_expr(dates.basis)
    if dates.start is not None:
        conditions.append(day >= dates.start)
    if dates.end is not None:
        conditions.append(day <= dates.end)

    search = _search_condition(filt.q)
    if search is not None:
        conditions.append(search)
    if filt.categories:
        conditions.append(_category_condition(resolve_category_ids(session, filt.categories)))
    if filt.merchant:
        merchant = " ".join(filt.merchant.split())
        conditions.append(merchant_key_expr() == func.lower(literal(merchant)))
    if filt.amount_min is not None:
        conditions.append(Transaction.amount >= filt.amount_min)
    if filt.amount_max is not None:
        conditions.append(Transaction.amount <= filt.amount_max)
    if filt.status == "pending":
        conditions.append(Transaction.pending.is_(True))
    elif filt.status == "posted":
        conditions.append(Transaction.pending.is_(False))
    if filt.account_id is not None:
        conditions.append(Transaction.account_id == filt.account_id)
    if filt.tag:
        conditions.append(_tag_condition(filt.tag))
    if filt.member:
        conditions.append(Transaction.account_owner == filt.member)
    if filt.credits_only:
        conditions.append(Transaction.amount < 0)
    return conditions


def _order_by(sort: str) -> list[Any]:
    spend_date = spend_date_expr()
    if sort == "date":
        return [spend_date.asc(), Transaction.id.asc()]
    if sort == "-amount":
        return [Transaction.amount.desc(), spend_date.desc(), Transaction.id.desc()]
    if sort == "amount":
        return [Transaction.amount.asc(), spend_date.desc(), Transaction.id.desc()]
    return [spend_date.desc(), Transaction.id.desc()]


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class SplitPart:
    category: CategoryRef
    amount: Decimal
    note: str | None


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """One transaction as the list shows it."""

    id: int
    account_id: int
    source: str
    pending: bool
    spend_date: dt.date
    date: dt.date
    authorized_date: dt.date | None
    posted_date: dt.date | None
    name: str
    merchant_name: str | None
    original_description: str | None
    amount: Decimal
    transaction_code: str | None
    category: CategoryRef
    category_source: str
    rule_id: int | None
    rule_label: str | None
    notes: str | None
    tags: tuple[str, ...]
    needs_review: bool
    review_reason: str | None
    duplicate_status: str
    account_owner: str | None
    user_edited_at: dt.datetime | None
    splits: tuple[SplitPart, ...]

    @property
    def title(self) -> str:
        """The merchant, else the description."""
        return self.merchant_name or self.name

    @property
    def subtitle(self) -> str | None:
        """The description under the merchant, or the raw description when there is no
        merchant."""
        if self.merchant_name:
            return self.name if self.name != self.merchant_name else None
        raw = self.original_description
        if raw and " ".join(raw.split()) != " ".join(self.name.split()):
            return raw
        return None

    @property
    def splits_total(self) -> Decimal:
        return to_money(sum((s.amount for s in self.splits), ZERO))

    @property
    def is_split(self) -> bool:
        """Split into parts that add up to the amount (only then do the parts count)."""
        return bool(self.splits) and self.splits_total == self.amount

    @property
    def splits_mismatch(self) -> bool:
        """Has parts that don't add up to the amount (the whole amount counts in the category)."""
        return bool(self.splits) and self.splits_total != self.amount

    @property
    def is_payment(self) -> bool:
        """In a card-payment (transfer) category: muted, never spending (TXN-3)."""
        return self.category.kind == "transfer"

    @property
    def is_credit(self) -> bool:
        return self.amount < 0

    @property
    def is_refund(self) -> bool:
        """A credit in a spending category: it counts against that category (TXN-3)."""
        return self.amount < 0 and not self.is_payment and self.category.kind == "expense"

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_status == "suspect"

    @property
    def is_user_category(self) -> bool:
        return self.category_source == "user"

    @property
    def category_source_label(self) -> str:
        return CATEGORY_SOURCE_LABELS.get(self.category_source, self.category_source)

    @property
    def can_split(self) -> bool:
        """Posted, not $0.00 and not a card payment (see :func:`validate_splits`)."""
        return not self.pending and self.amount != 0 and not self.is_payment


def rule_label(
    rule_id: int | None,
    name: str | None,
    merchant_pattern: str | None,
    description_pattern: str | None,
    match_type: str | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
) -> str:
    """A rule's name, else a short summary of its conditions."""
    if name:
        return name
    verb = {"equals": "is", "starts_with": "starts with", "regex": "matches"}.get(
        match_type or "", "contains"
    )
    parts = []
    if merchant_pattern:
        parts.append(f"Merchant {verb} “{merchant_pattern}”")
    if description_pattern:
        parts.append(f"Description {verb} “{description_pattern}”")
    if amount_min is not None and amount_max is not None:
        parts.append(f"{format_money(amount_min)} to {format_money(amount_max)}")
    elif amount_min is not None:
        parts.append(f"at least {format_money(amount_min)}")
    elif amount_max is not None:
        parts.append(f"at most {format_money(amount_max)}")
    return " · ".join(parts) or (f"Rule {rule_id}" if rule_id is not None else "Rule")


def _row_columns() -> tuple[Any, ...]:
    return (
        Transaction.id,
        Transaction.account_id,
        Transaction.source,
        Transaction.pending,
        spend_date_expr().label("spend_date"),
        Transaction.date,
        Transaction.authorized_date,
        Transaction.posted_date,
        Transaction.name,
        Transaction.merchant_name,
        Transaction.original_description,
        Transaction.amount,
        Transaction.transaction_code,
        Transaction.category_id,
        Transaction.category_source,
        Transaction.rule_id,
        Rule.name.label("rule_name"),
        Rule.merchant_pattern.label("rule_merchant_pattern"),
        Rule.description_pattern.label("rule_description_pattern"),
        Rule.match_type.label("rule_match_type"),
        Rule.amount_min.label("rule_amount_min"),
        Rule.amount_max.label("rule_amount_max"),
        Transaction.notes,
        Transaction.tags,
        Transaction.needs_review,
        Transaction.review_reason,
        Transaction.duplicate_status,
        Transaction.account_owner,
        Transaction.user_edited_at,
    )


def _missing_category(category_id: int) -> CategoryRef:
    return CategoryRef(category_id, "Unknown category", None, None, False, "expense", None)


def _splits_for(
    session: Session, ids: Sequence[int], categories: Mapping[int, CategoryRef]
) -> dict[int, list[SplitPart]]:
    out: dict[int, list[SplitPart]] = {}
    if not ids:
        return out
    rows = session.execute(
        select(
            TransactionSplit.transaction_id,
            TransactionSplit.category_id,
            TransactionSplit.amount,
            TransactionSplit.note,
        )
        .where(TransactionSplit.transaction_id.in_(ids))
        .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
    )
    for r in rows:
        category = categories.get(r.category_id) or _missing_category(r.category_id)
        out.setdefault(r.transaction_id, []).append(SplitPart(category, to_money(r.amount), r.note))
    return out


def _rows(
    session: Session,
    conditions: Sequence[ColumnElement[bool]],
    *,
    order_by: Sequence[Any] = (),
    limit: int | None = None,
    offset: int = 0,
    categories: Mapping[int, CategoryRef] | None = None,
) -> list[TransactionRow]:
    query = (
        select(*_row_columns())
        .outerjoin(Rule, Rule.id == Transaction.rule_id)
        .where(*conditions)
        .order_by(*order_by)
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    records = session.execute(query).all()
    if categories is None:
        categories = load_categories(session)
    splits = _splits_for(session, [r.id for r in records], categories)
    rows = []
    for r in records:
        rows.append(
            TransactionRow(
                id=r.id,
                account_id=r.account_id,
                source=r.source,
                pending=r.pending,
                spend_date=r.spend_date,
                date=r.date,
                authorized_date=r.authorized_date,
                posted_date=r.posted_date,
                name=r.name,
                merchant_name=r.merchant_name,
                original_description=r.original_description,
                amount=to_money(r.amount),
                transaction_code=r.transaction_code,
                category=categories.get(r.category_id) or _missing_category(r.category_id),
                category_source=r.category_source,
                rule_id=r.rule_id,
                rule_label=rule_label(
                    r.rule_id,
                    r.rule_name,
                    r.rule_merchant_pattern,
                    r.rule_description_pattern,
                    r.rule_match_type,
                    r.rule_amount_min,
                    r.rule_amount_max,
                )
                if r.rule_id is not None
                else None,
                notes=r.notes,
                tags=tuple(r.tags or ()),
                needs_review=r.needs_review,
                review_reason=r.review_reason,
                duplicate_status=r.duplicate_status,
                account_owner=r.account_owner,
                user_edited_at=r.user_edited_at,
                splits=tuple(splits.get(r.id, ())),
            )
        )
    return rows


def get_row(
    session: Session,
    transaction_id: int,
    *,
    categories: Mapping[int, CategoryRef] | None = None,
) -> TransactionRow:
    """One row as the list shows it; :class:`TransactionNotFound` if it is gone."""
    rows = _rows(session, [Transaction.id == transaction_id], categories=categories)
    if not rows:
        raise TransactionNotFound(transaction_id)
    return rows[0]


@dataclass(frozen=True, slots=True)
class Sums:
    """Raw sums of the matching rows: charges (positive amounts), credits (negative amounts,
    payments included) and their net."""

    charges: Decimal
    credits: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class TransactionPage:
    rows: tuple[TransactionRow, ...]
    total_count: int
    page: int
    pages: int
    per_page: int
    sums: Sums
    dates: DateRange
    sort: str

    @property
    def first_index(self) -> int:
        """1-based position of the first row on this page (0 when empty)."""
        return (self.page - 1) * self.per_page + 1 if self.rows else 0

    @property
    def last_index(self) -> int:
        return self.first_index + len(self.rows) - 1 if self.rows else 0


def search(
    session: Session,
    filt: TransactionFilter,
    *,
    page: int = 1,
    per_page: int = PER_PAGE,
    sort: str = DEFAULT_SORT,
    today: dt.date | None = None,
    categories: Mapping[int, CategoryRef] | None = None,
) -> TransactionPage:
    """One page of the rows matching ``filt``, with the count and raw sums of ALL matching rows.

    A page past the end shows the last page. ``sort``: ``-date`` (newest first, the default),
    ``date``, ``-amount`` (largest charge first) or ``amount``; ties by date then id.
    """
    sort = parse_sort(sort)
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))
    today = today or clock.today()
    conditions = filter_conditions(session, filt, today=today)
    amount = Transaction.amount
    totals = session.execute(
        select(
            func.count(Transaction.id),
            func.coalesce(func.sum(case((amount > 0, amount), else_=0)), 0),
            func.coalesce(func.sum(case((amount < 0, amount), else_=0)), 0),
        ).where(*conditions)
    ).one()
    total_count = int(totals[0])
    charges, credits = to_money(totals[1]), to_money(totals[2])
    pages = max(1, -(-total_count // per_page))
    page = max(1, min(int(page), pages))
    rows = _rows(
        session,
        conditions,
        order_by=_order_by(sort),
        limit=per_page,
        offset=(page - 1) * per_page,
        categories=categories,
    )
    return TransactionPage(
        rows=tuple(rows),
        total_count=total_count,
        page=page,
        pages=pages,
        per_page=per_page,
        sums=Sums(charges, credits, to_money(charges + credits)),
        dates=resolve_dates(session, filt, today),
        sort=sort,
    )


@dataclass(frozen=True, slots=True)
class AttentionCounts:
    """Rows the user should look at: ``needs_review`` and suspected duplicates (never counted)."""

    needs_review: int
    duplicates: int


def attention_counts(session: Session) -> AttentionCounts:
    suspect = Transaction.duplicate_status == "suspect"
    row = session.execute(
        select(
            func.count().filter(and_(Transaction.needs_review.is_(True), not_(suspect))),
            func.count().filter(suspect),
        )
    ).one()
    return AttentionCounts(int(row[0]), int(row[1]))


def has_transactions(session: Session) -> bool:
    """True once any transaction exists (else the page shows the "no transactions yet" state)."""
    return bool(session.scalar(select(exists().where(Transaction.id.is_not(None)))))


def several_card_members(session: Session) -> bool:
    """True when rows name two or more card members: only then do the Card member column and
    filter say anything. (Amex CSV files name the member on every row and Plaid rarely does, so
    with one member the column would only tell the CSV era from the Plaid era, and filtering on
    that member would hide every Plaid row.)"""
    names = session.scalars(
        select(Transaction.account_owner)
        .where(Transaction.account_owner.is_not(None), Transaction.account_owner != "")
        .distinct()
        .limit(2)
    ).all()
    return len(names) > 1


# --------------------------------------------------------------------------- edits


def _get_for_update(session: Session, transaction_id: int) -> Transaction:
    ledger_lock(session)
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise TransactionNotFound(transaction_id)
    return txn


def _category_id(session: Session, value: Any, errors: dict[str, str], field: str) -> int | None:
    category_id = parse_id(value)
    if category_id is None:
        errors[field] = "Choose a category."
        return None
    if session.get(Category, category_id) is None:
        errors[field] = "That category no longer exists. Reload the page and choose another."
        return None
    return category_id


def clean_tags(values: Iterable[Any], errors: dict[str, str]) -> list[str]:
    """Trim, drop empties and de-duplicate tags case-insensitively (first spelling kept)."""
    tags = userstate.normalize_tags(clean_text(v) for v in values)
    for tag in tags:
        if len(tag) > MAX_TAG_LENGTH:
            errors["tags"] = f"Keep each tag under {MAX_TAG_LENGTH} characters."
            break
        if "," in tag:
            errors["tags"] = "Tags can't contain commas."
            break
    if len(tags) > MAX_TAGS:
        errors["tags"] = f"Use at most {MAX_TAGS} tags."
    return tags


def _clear_flag_unless_split(txn: Transaction) -> None:
    """A category decision settles a review flag, unless the flag is about the splits."""
    if not txn.splits:
        txn.needs_review = False
        txn.review_reason = None


def update(
    session: Session,
    transaction_id: int,
    *,
    category_id: Any = UNSET,
    notes: Any = UNSET,
    tags: Any = UNSET,
) -> TransactionRow:
    """Edit the category, notes and/or tags (TXN-2); fields left ``UNSET`` don't change.

    A category choice sets ``category_source='user'`` and clears ``rule_id``; any change stamps
    ``user_edited_at``. Notes are trimmed (empty → none); tags are trimmed and de-duplicated
    case-insensitively, keeping the first spelling. Control characters are dropped from both
    (:func:`clean_text`). Takes the ledger lock first.
    """
    txn = _get_for_update(session, transaction_id)
    errors: dict[str, str] = {}
    new_category = None
    if category_id is not UNSET:
        new_category = _category_id(session, category_id, errors, "category_id")
    new_notes = None
    if notes is not UNSET:
        new_notes = clean_text(notes).strip() or None
        if new_notes is not None and len(new_notes) > MAX_NOTES_LENGTH:
            errors["notes"] = f"Keep notes under {MAX_NOTES_LENGTH:,} characters."
    new_tags: list[str] = []
    if tags is not UNSET:
        new_tags = clean_tags(tags or (), errors)
    if errors:
        raise TransactionEditError(errors)

    changed = False
    if new_category is not None and (
        new_category != txn.category_id or txn.category_source != "user"
    ):
        txn.category_id = new_category
        txn.category_source = "user"
        txn.rule_id = None
        _clear_flag_unless_split(txn)
        changed = True
    if notes is not UNSET and new_notes != txn.notes:
        txn.notes = new_notes
        changed = True
    if tags is not UNSET and new_tags != list(txn.tags or ()):
        txn.tags = new_tags
        changed = True
    if changed:
        txn.user_edited_at = clock.now()
    session.flush()
    return get_row(session, txn.id)


def reset_to_automatic(session: Session, transaction_id: int) -> TransactionRow:
    """Forget the user's category and categorize the row again (payments → rules → mapping →
    Uncategorized, plus the refund rule). Splits, notes and tags stay."""
    txn = _get_for_update(session, transaction_id)
    categorizer = categorize.Categorizer.load(session)
    was_user = txn.category_source == "user"
    txn.category_source = "default"
    categorize.apply_decision(txn, categorizer.decide(txn))
    if was_user:
        _clear_flag_unless_split(txn)
    session.flush()
    return get_row(session, txn.id)


def clear_review(session: Session, transaction_id: int) -> TransactionRow:
    """Mark a ``needs_review`` row as reviewed (the user accepts it as it is)."""
    txn = _get_for_update(session, transaction_id)
    txn.needs_review = False
    txn.review_reason = None
    session.flush()
    return get_row(session, txn.id)


SplitInput = tuple[Any, Any, str | None]


def validate_splits(
    session: Session, txn: Transaction, parts: Sequence[SplitInput]
) -> list[userstate.SplitState]:
    """Check split parts against ``txn`` (TXN-4); :class:`TransactionEditError` if any is wrong.

    Rules: the row is posted, not $0.00 and not a card payment (a row in a ``transfer``
    category: its parts would move a payment into spending; ARCHITECTURE §6 never gives payments
    splits); 2 to 12 parts; each part has a category that exists (each category once), a non-zero
    amount in cents with the transaction's sign, an optional note; the parts add up EXACTLY to
    the transaction's amount.
    """
    errors: dict[str, str] = {}
    amount = to_money(txn.amount)
    if txn.pending:
        raise TransactionEditError(
            {"parts": "A pending charge can't be split yet. Split it once it posts."}
        )
    if amount == 0:
        raise TransactionEditError({"parts": "A $0.00 transaction can't be split."})
    own_category = session.get(Category, txn.category_id)
    if own_category is not None and own_category.kind == "transfer":
        raise TransactionEditError(
            {
                "parts": "A card payment can't be split. If this isn't a card payment, "
                "change its category first."
            }
        )
    if len(parts) < 2:
        raise TransactionEditError({"parts": "Split it into at least two parts."})
    if len(parts) > MAX_SPLIT_PARTS:
        raise TransactionEditError({"parts": f"Use at most {MAX_SPLIT_PARTS} parts."})
    sign = 1 if amount > 0 else -1
    kind = "charge" if sign > 0 else "credit"
    cleaned: list[userstate.SplitState] = []
    seen: set[int] = set()
    total = ZERO
    for i, (raw_category, raw_amount, raw_note) in enumerate(parts):
        category_id = _category_id(session, raw_category, errors, f"part-{i}-category")
        value: Decimal | None = None
        if not isinstance(raw_amount, Decimal) or not is_cents(raw_amount):
            errors[f"part-{i}-amount"] = "Enter an amount in dollars and cents."
        elif raw_amount == 0:
            errors[f"part-{i}-amount"] = "A part can't be $0.00."
        elif (raw_amount > 0) != (sign > 0):
            errors[f"part-{i}-amount"] = f"This is a {kind}, so every part must be a {kind} too."
        else:
            value = to_money(raw_amount)
            total += value
        note = " ".join(clean_text(raw_note).split()) or None
        if note is not None and len(note) > MAX_SPLIT_NOTE_LENGTH:
            errors[f"part-{i}-note"] = f"Keep the note under {MAX_SPLIT_NOTE_LENGTH} characters."
        if category_id is not None:
            if category_id in seen:
                errors[f"part-{i}-category"] = "Use each category once (combine those parts)."
            seen.add(category_id)
        if category_id is not None and value is not None:
            cleaned.append(userstate.SplitState(category_id, value, note))
    if not errors and total != amount:
        difference = amount - total
        left = (
            f"{format_money(abs(difference))} left to assign"
            if (difference > 0) == (sign > 0)
            else f"{format_money(abs(difference))} too much"
        )
        errors["parts"] = (
            f"The parts add up to {format_money(abs(total))}, but the {kind} is "
            f"{format_money(abs(amount))} ({left})."
        )
    if errors:
        raise TransactionEditError(errors)
    return cleaned


def set_splits(
    session: Session, transaction_id: int, parts: Sequence[SplitInput]
) -> TransactionRow:
    """Split a transaction across categories (TXN-4): ``parts`` is ``[(category_id, amount,
    note), …]`` with signed ``Decimal`` amounts (see :func:`validate_splits`).

    Replaces any earlier split. The transaction's own category stays as the fallback; the parts
    feed ``v_allocations`` (and so every spending number) because they add up to the amount.
    Stamps ``user_edited_at`` and settles a review flag. Takes the ledger lock first.
    """
    txn = _get_for_update(session, transaction_id)
    cleaned = validate_splits(session, txn, parts)
    txn.splits.clear()
    session.flush()
    for part in cleaned:
        txn.splits.append(
            TransactionSplit(category_id=part.category_id, amount=part.amount, note=part.note)
        )
    txn.user_edited_at = clock.now()
    txn.needs_review = False
    txn.review_reason = None
    session.flush()
    return get_row(session, txn.id)


def clear_splits(session: Session, transaction_id: int) -> TransactionRow:
    """Remove a split: the whole amount goes back to the transaction's own category."""
    txn = _get_for_update(session, transaction_id)
    if txn.splits:
        txn.splits.clear()
        txn.user_edited_at = clock.now()
        txn.needs_review = False
        txn.review_reason = None
        session.flush()
    return get_row(session, txn.id)


# --------------------------------------------------------------------------- one-click rules


def clean_description(text: str | None) -> str:
    """A merchant-like pattern from a raw card description, for a one-click rule.

    ``"SQ *DELFINA SAN FRANCISCO CA"`` → ``"DELFINA SAN FRANCISCO"``,
    ``"WHOLEFDS MKT 10234 SAN FRANCISCO CA"`` → ``"WHOLEFDS MKT"``,
    ``"UBER *TRIP HELP.UBER.COM CA"`` → ``"UBER"``: processor prefixes go, and the text stops at
    the first store number, phone number or web address after the first word. An Amex CSV
    description stops where its padding starts: ``"BLUE BOTTLE COFFEE      SAN FRANCISCO CA"``
    → ``"BLUE BOTTLE COFFEE"`` (the city would keep the rule from matching Plaid's merchant
    names).
    """
    head = _PADDING.split(clean_text(text).strip(), maxsplit=1)[0]
    collapsed = " ".join(head.split())
    if not collapsed:
        return ""
    body = collapsed
    if "*" in body:
        before, after = (part.strip() for part in body.split("*", 1))
        if before.upper() in PROCESSOR_PREFIXES and after:
            body = after
        elif before:
            body = before
        else:
            body = after
    words: list[str] = []
    for word in body.split():
        if words and (
            any(ch.isdigit() for ch in word) or word.startswith("#") or _DOMAIN_TOKEN.search(word)
        ):
            break
        words.append(word)
    if len(words) > 1 and _STATE_CODE.match(words[-1]):
        words.pop()
    cleaned = " ".join(words).strip(" -*#,.")
    return cleaned or collapsed


@dataclass(frozen=True, slots=True)
class RuleDraft:
    """Prefilled values for "Always categorize like this"."""

    transaction_id: int
    merchant_pattern: str
    category_id: int | None
    priority: int
    #: The rule the new one is placed before (it matched this transaction first), if any.
    runs_before: str | None
    #: Card payments are fixed (step 0); a rule never changes them.
    is_payment: bool


def _first_matching_rule(session: Session, txn: Transaction) -> Rule | None:
    for rule in categorize.Categorizer.load(session).rules:
        if categorize.rule_matches(rule, txn):
            return rule
    return None


def _one_click_priority(session: Session, txn: Transaction) -> tuple[int, Rule | None]:
    """Just before the first active rule matching ``txn`` (so the new rule wins), else 100."""
    first = _first_matching_rule(session, txn)
    if first is None:
        return DEFAULT_RULE_PRIORITY, None
    return first.priority - 1, first


def one_click_draft(session: Session, transaction_id: int) -> RuleDraft:
    """The one-click rule's defaults: the merchant (or the cleaned description) and the row's
    category (none when it is Uncategorized)."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise TransactionNotFound(transaction_id)
    pattern = (txn.merchant_name or "").strip() or clean_description(txn.name)
    uncategorized = categorize.system_category_id(session, "uncategorized")
    priority, first = _one_click_priority(session, txn)
    runs_before = _rule_ref(first).label if first is not None else None
    return RuleDraft(
        transaction_id=txn.id,
        merchant_pattern=pattern,
        category_id=None if txn.category_id == uncategorized else txn.category_id,
        priority=priority,
        runs_before=runs_before,
        is_payment=txn.category_source == "system" or categorize.is_payment(txn),
    )


@dataclass(frozen=True, slots=True)
class RulePreview:
    """What a one-click rule would do to past transactions (every row, as applying it does).

    ``csv_only`` is True when every match came from an Amex CSV file although Plaid rows exist:
    new charges arrive from Plaid and may name the merchant differently, so the rule might never
    match them. ``plaid_merchant`` is then the name Plaid gives the Plaid rows whose description
    contains the text (``"SFMTA"`` for ``"SFMTA PAYBYPHONE"``), when there are any.
    """

    matches: int
    will_change: int
    user_edited: int
    payments: int
    csv_only: bool = False
    plaid_merchant: str | None = None


def _clean_pattern(value: Any) -> str:
    return " ".join(clean_text(value).split())


def preview_one_click_rule(
    session: Session, transaction_id: int, *, merchant_pattern: str, category_id: Any
) -> RulePreview | None:
    """Count the past transactions "merchant contains ``merchant_pattern``" matches and how many
    would move to ``category_id`` if applied (user-edited rows never change). ``None`` when the
    pattern or category isn't usable yet."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise TransactionNotFound(transaction_id)
    pattern = _clean_pattern(merchant_pattern)
    errors: dict[str, str] = {}
    target = _category_id(session, category_id, errors, "category_id")
    if not pattern or target is None or len(pattern) > categorize.MAX_PATTERN_LENGTH:
        return None
    priority, _ = _one_click_priority(session, txn)
    draft = Rule(merchant_pattern=pattern, match_type="contains", category_id=target)
    draft.priority = priority
    categorizer = categorize.Categorizer.load(session)
    position = next(
        (i for i, r in enumerate(categorizer.rules) if r.priority > priority),
        len(categorizer.rules),
    )
    categorizer.rules.insert(position, draft)
    columns = (
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
    )
    matches = will_change = user_edited = payments = from_plaid = 0
    any_plaid = False
    plaid_names: Counter[str] = Counter()
    for row in session.execute(select(*columns)):
        any_plaid = any_plaid or row.source == "plaid"
        if not categorize.rule_matches(draft, row):
            # A Plaid row the rule misses although its description has the text: Plaid names
            # the merchant differently (the rule reads merchant_name when there is one).
            if (
                row.source == "plaid"
                and row.merchant_name
                and (
                    categorize.text_matches(pattern, row.name, "contains")
                    or categorize.text_matches(pattern, row.original_description, "contains")
                )
            ):
                plaid_names[row.merchant_name] += 1
            continue
        matches += 1
        if row.source == "plaid":
            from_plaid += 1
        if row.category_source == "user":
            user_edited += 1
            continue
        decision = categorizer.decide(row)
        if decision.source == "system":
            payments += 1
        if decision.category_id != row.category_id:
            will_change += 1
    csv_only = bool(matches) and not from_plaid and any_plaid
    plaid_merchant = plaid_names.most_common(1)[0][0] if csv_only and plaid_names else None
    return RulePreview(matches, will_change, user_edited, payments, csv_only, plaid_merchant)


@dataclass(frozen=True, slots=True)
class OneClickResult:
    """``changed`` and ``skipped_user`` count past transactions (only when ``applied``);
    ``source_changed`` says whether the transaction the rule was made from changed category."""

    rule_id: int
    category_id: int
    applied: bool
    changed: int
    skipped_user: int
    source_changed: bool = False


def create_one_click_rule(
    session: Session,
    transaction_id: int,
    *,
    merchant_pattern: str,
    category_id: Any,
    apply_to_past: bool,
) -> OneClickResult:
    """ "Always categorize like this" (CAT-3): a rule "merchant contains ``merchant_pattern``" →
    ``category_id`` made with :func:`categorize.create_rule`, placed just before the first rule
    that matched this transaction so it takes effect, and remembering this transaction as its
    origin.

    The transaction it was made from follows the rule at once (when the rule matches it and the
    user did not categorize it by hand). With ``apply_to_past`` every other past transaction it
    matches is re-categorized too, again except the ones the user categorized by hand. Raises
    :class:`categorize.RuleValidationError`.
    """
    txn = _get_for_update(session, transaction_id)
    errors: dict[str, str] = {}
    target = _category_id(session, category_id, errors, "category_id")
    if target is None:
        raise categorize.RuleValidationError(errors)
    priority, _ = _one_click_priority(session, txn)
    category_before = txn.category_id
    result = categorize.create_rule(
        session,
        category_id=target,
        merchant_pattern=_clean_pattern(merchant_pattern) or None,
        match_type="contains",
        priority=priority,
        source_transaction_id=txn.id,
        apply_to_existing=apply_to_past,
    )
    applied = result.applied
    if (
        applied is None
        and txn.category_source != "user"
        and categorize.rule_matches(result.rule, txn)
    ):
        categorize.recategorize(session, transaction_ids=[txn.id])
    return OneClickResult(
        rule_id=result.rule.id,
        category_id=target,
        applied=applied is not None,
        changed=applied.changed if applied is not None else 0,
        skipped_user=applied.skipped_user if applied is not None else 0,
        source_changed=txn.category_id != category_before,
    )


# --------------------------------------------------------------------------- details


@dataclass(frozen=True, slots=True)
class RuleRef:
    id: int
    label: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class PendingLink:
    """The pending charge a posted row replaced (its tombstone, when still kept).

    ``carried_edits`` is True only when the tombstone shows the pending charge had user edits
    (they moved to this row); ``None`` when the tombstone is gone (pruned after 90 days).
    """

    plaid_transaction_id: str
    amount: Decimal | None
    date: dt.date | None
    name: str | None
    carried_edits: bool | None = None


@dataclass(frozen=True, slots=True)
class InheritedFrom:
    """A removed transaction whose edits this row inherited (a matched tombstone)."""

    plaid_transaction_id: str
    amount: Decimal
    date: dt.date
    name: str | None
    removed_at: dt.datetime


@dataclass(frozen=True, slots=True)
class TransactionDetail:
    """Every stored field of a transaction, plus where its category and edits came from."""

    row: TransactionRow
    account_name: str
    account_is_manual: bool
    plaid_transaction_id: str | None
    pending_transaction_id: str | None
    iso_currency_code: str | None
    pfc_primary: str | None
    pfc_detailed: str | None
    pfc_confidence: str | None
    pfc_version: str | None
    merchant_category_code: str | None
    merchant_entity_id: str | None
    external_category: str | None
    payment_channel: str | None
    logo_url: str | None
    website: str | None
    location: dict[str, Any] | None
    csv_reference: str | None
    csv_import_filename: str | None
    csv_imported_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime
    rule: RuleRef | None
    replaced_pending: PendingLink | None
    inherited_from: tuple[InheritedFrom, ...]
    rules_made_here: tuple[RuleRef, ...]

    @property
    def location_text(self) -> str | None:
        """``"1 Ferry Building, San Francisco, CA 94111"`` (``None`` for online merchants)."""
        loc = self.location or {}
        region = " ".join(str(v) for v in (loc.get("region"), loc.get("postal_code")) if v)
        parts = [str(v) for v in (loc.get("address"), loc.get("city")) if v]
        if region:
            parts.append(region)
        if loc.get("country") and loc.get("country") != "US":
            parts.append(str(loc["country"]))
        return ", ".join(parts) or None


def _rule_ref(rule: Rule) -> RuleRef:
    return RuleRef(
        rule.id,
        rule_label(
            rule.id,
            rule.name,
            rule.merchant_pattern,
            rule.description_pattern,
            rule.match_type,
            rule.amount_min,
            rule.amount_max,
        ),
        rule.is_active,
    )


def detail(session: Session, transaction_id: int) -> TransactionDetail:
    """Everything about one transaction (the details dialog and page)."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise TransactionNotFound(transaction_id)
    row = get_row(session, transaction_id)
    account = session.get(Account, txn.account_id)
    csv_import = session.get(CsvImport, txn.csv_import_id) if txn.csv_import_id else None
    rule = session.get(Rule, txn.rule_id) if txn.rule_id else None
    replaced = None
    if txn.pending_transaction_id:
        tomb = session.get(TransactionTombstone, txn.pending_transaction_id)
        replaced = PendingLink(
            txn.pending_transaction_id,
            to_money(tomb.amount) if tomb else None,
            tomb.date if tomb else None,
            tomb.name if tomb else None,
            bool(tomb.user_state) if tomb else None,
        )
    inherited = tuple(
        InheritedFrom(t.plaid_transaction_id, to_money(t.amount), t.date, t.name, t.removed_at)
        for t in session.scalars(
            select(TransactionTombstone)
            .where(TransactionTombstone.consumed_by_transaction_id == txn.id)
            .order_by(TransactionTombstone.removed_at)
        )
    )
    made_here = tuple(
        _rule_ref(r)
        for r in session.scalars(
            select(Rule).where(Rule.source_transaction_id == txn.id).order_by(Rule.id)
        )
    )
    return TransactionDetail(
        row=row,
        account_name=account.display_name if account else "Unknown account",
        account_is_manual=bool(account and account.is_manual),
        plaid_transaction_id=txn.plaid_transaction_id,
        pending_transaction_id=txn.pending_transaction_id,
        iso_currency_code=txn.iso_currency_code,
        pfc_primary=txn.pfc_primary,
        pfc_detailed=txn.pfc_detailed,
        pfc_confidence=txn.pfc_confidence,
        pfc_version=txn.pfc_version,
        merchant_category_code=txn.merchant_category_code,
        merchant_entity_id=txn.merchant_entity_id,
        external_category=txn.external_category,
        payment_channel=txn.payment_channel,
        logo_url=txn.logo_url,
        website=txn.website,
        location=dict(txn.location) if txn.location else None,
        csv_reference=txn.csv_reference,
        csv_import_filename=csv_import.filename if csv_import else None,
        csv_imported_at=csv_import.imported_at if csv_import else None,
        created_at=txn.created_at,
        updated_at=txn.updated_at,
        rule=_rule_ref(rule) if rule else None,
        replaced_pending=replaced,
        inherited_from=inherited,
        rules_made_here=made_here,
    )


# --------------------------------------------------------------------------- CSV export (TXN-5)


def _cell(value: str | None) -> str:
    """Text for a CSV cell, with a leading apostrophe where a spreadsheet would see a formula."""
    if not value:
        return ""
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def _category_cell(
    category_id: int,
    splits: Sequence[tuple[int, Decimal]],
    amount: Decimal,
    categories: Mapping[int, CategoryRef],
) -> str:
    def name(cid: int) -> str:
        ref = categories.get(cid)
        return ref.name if ref else "Unknown category"

    if splits and to_money(sum((a for _, a in splits), ZERO)) == amount:
        return "; ".join(f"{name(cid)} {to_money(a)}" for cid, a in splits)
    return name(category_id)


def export_csv(
    session: Session,
    filt: TransactionFilter,
    *,
    sort: str = DEFAULT_SORT,
    today: dt.date | None = None,
    batch_size: int = 1000,
) -> Iterator[str]:
    """The rows matching ``filt`` as CSV text chunks (TXN-5), header first.

    Columns: :data:`EXPORT_COLUMNS`. Dates are ISO (``2026-10-27``); the Date column is the date
    the app shows (``COALESCE(authorized_date, date)``). Amounts are plain decimals with the
    app's sign (charges positive, credits negative), never rounded or reformatted. A split row
    lists its parts in Category (``Groceries 150.00; Home 63.77``). Any other cell starting with
    ``= + - @``, a tab or a carriage return gets a leading apostrophe (CSV injection). Suspected
    duplicates are left out unless the filter shows only them. The text starts with a UTF-8 byte
    order mark so spreadsheet apps read accented names correctly.

    Every query has started by the time the header is yielded, so a caller that takes the first
    chunk before sending anything (the export route) reports a failure as an error page rather
    than as a cut-off download.
    """
    sort = parse_sort(sort)
    today = today or clock.today()
    conditions = filter_conditions(session, filt, today=today)
    categories = load_categories(session)
    accounts = {a.id: a.display_name for a in session.scalars(select(Account))}
    splits: dict[int, list[tuple[int, Decimal]]] = {}
    for r in session.execute(
        select(
            TransactionSplit.transaction_id, TransactionSplit.category_id, TransactionSplit.amount
        )
        .where(TransactionSplit.transaction_id.in_(select(Transaction.id).where(*conditions)))
        .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
    ):
        splits.setdefault(r.transaction_id, []).append((r.category_id, to_money(r.amount)))

    def chunk(rows: Iterable[Sequence[str]]) -> str:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\r\n").writerows(rows)
        return buffer.getvalue()

    query = (
        select(
            Transaction.id,
            spend_date_expr().label("spend_date"),
            Transaction.authorized_date,
            Transaction.posted_date,
            Transaction.name,
            Transaction.merchant_name,
            Transaction.category_id,
            Transaction.amount,
            Transaction.pending,
            Transaction.account_id,
            Transaction.account_owner,
            Transaction.notes,
            Transaction.tags,
            Transaction.source,
        )
        .where(*conditions)
        .order_by(*_order_by(sort))
        .execution_options(yield_per=batch_size)
    )
    result = session.execute(query)
    yield "﻿" + chunk([EXPORT_COLUMNS])
    for partition in result.partitions():
        lines = []
        for r in partition:
            amount = to_money(r.amount)
            lines.append(
                (
                    r.spend_date.isoformat(),
                    r.authorized_date.isoformat() if r.authorized_date else "",
                    r.posted_date.isoformat() if r.posted_date else "",
                    _cell(r.name),
                    _cell(r.merchant_name),
                    _cell(_category_cell(r.category_id, splits.get(r.id, ()), amount, categories)),
                    str(amount),
                    "yes" if r.pending else "no",
                    _cell(accounts.get(r.account_id, "")),
                    _cell(r.account_owner),
                    _cell(r.notes),
                    _cell(", ".join(r.tags or ())),
                    "Amex CSV" if r.source == "csv" else "Plaid",
                )
            )
        yield chunk(lines)


def export_filename(filt: TransactionFilter, dates: DateRange, today: dt.date) -> str:
    """``transactions-2026-10-01-to-2026-10-31.csv`` (or ``transactions-<today>.csv``)."""
    if dates.start and dates.end:
        return f"transactions-{dates.start.isoformat()}-to-{dates.end.isoformat()}.csv"
    if dates.start:
        return f"transactions-since-{dates.start.isoformat()}.csv"
    return f"transactions-{today.isoformat()}.csv"
