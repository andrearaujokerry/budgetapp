"""Recurring charges and subscriptions (INS-3).

* **Charges looked at:** posted, positive allocations in ``expense`` categories from the last
  400 days, read from ``v_allocations`` (so splits and suspected duplicates are already handled),
  one per transaction, dated by ``spend_date`` (the charge date, steadier than the posting
  date), grouped by :func:`merchant_key`.
* **A series:** at least 3 charges (2 for yearly); a median interval of 7±2, 14±3, 30±5,
  91±10 or 365±20 days with at least 70% of the intervals in that band (a lunch place visited
  every 12 days on average is a habit, not a subscription); and every amount within 20% of the
  median, allowing one price change (the charges before and after it each within 20% of their
  own median). A merchant that bills several subscriptions under one id (Apple: iCloud and Apple
  Music) fails the amount test; its charges of one exact amount are then tried on their own, and
  such series that follow one another at the same cadence are one subscription whose price
  changed more than once.
* **For each series:** cadence; typical amount (the new price after a fixed-price change, else
  the median of the last three at the current level); last amount and date; next expected date
  (last + median interval; a monthly, quarterly or yearly series that charges on a fixed day of
  the month steps to that day instead, so it never drifts); monthly cost (``amount × 30.44 /
  cadence length``: weekly × 30.44/7, every 2 weeks × 30.44/14, monthly × 1, quarterly ÷ 3,
  yearly ÷ 12); category; and active (seen within 1.5 × the cadence) or lapsed.
* **Overrides** (``recurring_overrides``, keyed by merchant): ``ignored`` moves the merchant's
  series to the Ignored list; ``confirmed`` keeps the merchant listed even when detection fails.

Services never commit; the routes do.
"""

from __future__ import annotations

import calendar
import datetime as dt
import itertools
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp import clock, periods
from budgetapp.models import (
    RECURRING_STATUSES,
    Category,
    RecurringOverride,
    Transaction,
    v_allocations,
)
from budgetapp.money import ZERO, to_money

LOOKBACK_DAYS = 400
#: Every amount within this share of the median (per price segment).
AMOUNT_TOLERANCE = Decimal("0.20")
#: Share of the intervals that must be in the cadence's band (and of recent charges on the
#: billing day for calendar stepping).
MIN_IN_BAND = Decimal("0.70")
#: A charge this many days from the billing day still counts as on it (weekends).
ANCHOR_SLACK_DAYS = 2
ANCHOR_SAMPLE = 6
DAYS_PER_MONTH = Decimal("30.44")
LAPSE_FACTOR = Decimal("1.5")
#: The typical amount is the median of this many latest charges at the current price.
TYPICAL_OF_LAST = 3
MAX_KEY_LENGTH = 300

IGNORED = "ignored"
CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class Cadence:
    key: str
    label: str
    #: The band's centre and half-width in days.
    days: int
    tolerance: int
    #: The cadence's length for the monthly cost (``amount × 30.44 / length``).
    length: Decimal
    min_count: int
    #: Calendar months per step (monthly, quarterly, yearly), else None.
    months: int | None = None

    def fits(self, interval: Decimal | int) -> bool:
        return abs(Decimal(interval) - self.days) <= self.tolerance


WEEKLY = Cadence("weekly", "Weekly", 7, 2, Decimal(7), 3)
BIWEEKLY = Cadence("biweekly", "Every 2 weeks", 14, 3, Decimal(14), 3)
MONTHLY = Cadence("monthly", "Monthly", 30, 5, DAYS_PER_MONTH, 3, 1)
QUARTERLY = Cadence("quarterly", "Quarterly", 91, 10, DAYS_PER_MONTH * 3, 3, 3)
ANNUAL = Cadence("annual", "Yearly", 365, 20, DAYS_PER_MONTH * 12, 2, 12)
CADENCES = (WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY, ANNUAL)


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True, slots=True)
class CategoryRef:
    """What ``ui.category_badge`` reads."""

    id: int
    name: str
    color: str | None
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class Charge:
    """One transaction's positive expense allocations (its amount for detection)."""

    transaction_id: int
    date: dt.date
    amount: Decimal
    category_id: int
    merchant_name: str | None
    name: str


@dataclass(frozen=True, slots=True)
class RecurringSeries:
    """A detected (or user-confirmed) series of charges from one merchant."""

    key: str
    merchant: str
    #: Text for ``/transactions?q=…`` that finds this merchant's charges.
    search_text: str
    cadence: str | None
    cadence_label: str
    #: The median interval between charges, in days.
    interval_days: int | None
    typical_amount: Decimal
    #: The price before a fixed-price change (``None`` without one).
    previous_amount: Decimal | None
    last_amount: Decimal
    last_date: dt.date
    first_date: dt.date
    count: int
    next_expected: dt.date | None
    monthly_cost: Decimal | None
    category: CategoryRef | None
    active: bool
    #: Active, but the expected charge hasn't arrived yet.
    late: bool
    #: The user's override: ``ignored``, ``confirmed`` or ``None``.
    status: str | None
    #: False for a confirmed merchant kept although detection found no series.
    detected: bool = True

    @property
    def is_ignored(self) -> bool:
        return self.status == IGNORED

    @property
    def is_confirmed(self) -> bool:
        return self.status == CONFIRMED

    @property
    def price_changed(self) -> bool:
        return self.previous_amount is not None


@dataclass(frozen=True, slots=True)
class RecurringOverview:
    """Everything the Recurring page shows."""

    today: dt.date
    since: dt.date
    #: Active series, next expected charge first.
    active: tuple[RecurringSeries, ...]
    #: Series not seen for more than 1.5 × their cadence, most recent first.
    lapsed: tuple[RecurringSeries, ...]
    ignored: tuple[RecurringSeries, ...]
    #: The monthly cost of the active series.
    monthly_total: Decimal

    @property
    def yearly_total(self) -> Decimal:
        return to_money(self.monthly_total * 12)

    @property
    def next_up(self) -> RecurringSeries | None:
        """The next expected charge from today on."""
        upcoming = [s for s in self.active if s.next_expected and s.next_expected >= self.today]
        return upcoming[0] if upcoming else None

    @property
    def is_empty(self) -> bool:
        return not (self.active or self.lapsed or self.ignored)


# --------------------------------------------------------------------------- merchant keys

_PROCESSOR_PREFIX = re.compile(
    r"^(?:sq|sqc|tst|dd|py|sp|pp|paypal|pos|ck|bt|ic|in|sms|wpy|eb|toast)\s*\*\s*", re.IGNORECASE
)
_STORE_NUMBER = re.compile(r"#\s*\S*")
_DIGITS = re.compile(r"\d+")
_PUNCTUATION = re.compile(r"[^\w&']+")
_SEARCH_CUT = re.compile(r"[*#\d]")


def merchant_key(
    *, merchant_entity_id: str | None = None, merchant_name: str | None = None, name: str = ""
) -> str:
    """The key a merchant's charges are grouped (and overridden) by.

    Plaid's ``merchant_entity_id`` when there is one; otherwise ``merchant_name`` (else the
    description) lower-cased, with payment-processor prefixes (``SQ *``, ``TST*``), ``*xxxx``
    suffixes, store numbers (``#236``), digits and punctuation removed:
    ``"AMAZON MKTPL*JA3YVME"`` → ``"amazon mktpl"``, ``"TRADER JOE S #236"`` → ``"trader joe s"``.
    """
    if merchant_entity_id and merchant_entity_id.strip():
        return merchant_entity_id.strip()
    raw = " ".join((merchant_name or name or "").lower().split())
    text = _PROCESSOR_PREFIX.sub("", raw)
    if "*" in text:
        head = text.split("*", 1)[0].strip()
        text = head or text.replace("*", " ")
    text = _STORE_NUMBER.sub(" ", text)
    text = _DIGITS.sub(" ", text)
    text = _PUNCTUATION.sub(" ", text).replace("_", " ")
    return " ".join(text.split()) or raw


def search_text(merchant_name: str | None, name: str) -> str:
    """What to search the Transactions page for: the merchant, else the description's head."""
    if merchant_name and merchant_name.strip():
        return merchant_name.strip()
    text = _PROCESSOR_PREFIX.sub("", name.strip())
    head = _SEARCH_CUT.split(text, 1)[0].strip()
    return head or name.strip()


# --------------------------------------------------------------------------- loading


def _charges(session: Session, since: dt.date, until: dt.date) -> dict[str, list[Charge]]:
    """Posted positive expense allocations per transaction, grouped by merchant key."""
    a = v_allocations.c
    query = (
        select(
            a.transaction_id,
            a.spend_date,
            a.amount,
            a.category_id,
            Transaction.merchant_entity_id,
            Transaction.merchant_name,
            Transaction.name,
        )
        .select_from(v_allocations)
        .join(Category, Category.id == a.category_id)
        .join(Transaction, Transaction.id == a.transaction_id)
        .where(
            Category.kind == "expense",
            a.pending.is_(False),
            a.amount > 0,
            a.spend_date >= since,
            a.spend_date <= until,
        )
    )
    per_txn: dict[int, dict] = {}
    for row in session.execute(query):
        entry = per_txn.get(row.transaction_id)
        if entry is None:
            per_txn[row.transaction_id] = {
                "row": row,
                "amount": to_money(row.amount),
                "largest": (to_money(row.amount), row.category_id),
            }
            continue
        entry["amount"] = to_money(entry["amount"] + row.amount)
        if to_money(row.amount) > entry["largest"][0]:
            entry["largest"] = (to_money(row.amount), row.category_id)
    groups: dict[str, list[Charge]] = defaultdict(list)
    for txn_id, entry in per_txn.items():
        row = entry["row"]
        key = merchant_key(
            merchant_entity_id=row.merchant_entity_id,
            merchant_name=row.merchant_name,
            name=row.name,
        )
        groups[key].append(
            Charge(
                transaction_id=txn_id,
                date=row.spend_date,
                amount=entry["amount"],
                category_id=entry["largest"][1],
                merchant_name=row.merchant_name,
                name=row.name,
            )
        )
    for charges in groups.values():
        charges.sort(key=lambda c: (c.date, c.transaction_id))
    return groups


# --------------------------------------------------------------------------- detection


def _median(values: Sequence[Decimal | int]) -> Decimal:
    ordered = sorted(Decimal(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stable(amounts: Sequence[Decimal]) -> bool:
    """Every amount within :data:`AMOUNT_TOLERANCE` of the median."""
    median = _median(amounts)
    return all(abs(x - median) <= AMOUNT_TOLERANCE * median for x in amounts)


def _price_split(amounts: Sequence[Decimal], *, allow_change: bool) -> int | None:
    """Where the current price starts: 0 when stable, ``k`` after one price change, else None."""
    if _stable(amounts):
        return 0
    if not allow_change:
        return None
    for k in range(1, len(amounts)):
        if _stable(amounts[:k]) and _stable(amounts[k:]):
            return k
    return None


@dataclass(frozen=True, slots=True)
class _Fit:
    charges: tuple[Charge, ...]
    cadence: Cadence | None
    median_interval: Decimal | None
    split: int
    detected: bool = True
    #: An exact-amount series within a merchant that bills several amounts.
    partial: bool = False


def _intervals(charges: Sequence[Charge]) -> list[int]:
    return [(b.date - a.date).days for a, b in itertools.pairwise(charges)]


def fit_series(charges: Sequence[Charge], *, allow_price_change: bool = True) -> _Fit | None:
    """The series these charges (one merchant, by date) form, or ``None`` (module docstring)."""
    if len(charges) < 2:
        return None
    intervals = _intervals(charges)
    median = _median(intervals)
    cadence = next((c for c in CADENCES if c.fits(median)), None)
    if cadence is None or len(charges) < cadence.min_count:
        return None
    if sum(1 for i in intervals if cadence.fits(i)) < MIN_IN_BAND * len(intervals):
        return None
    split = _price_split([c.amount for c in charges], allow_change=allow_price_change)
    if split is None:
        return None
    return _Fit(tuple(charges), cadence, median, split)


def _detect(charges: Sequence[Charge]) -> list[_Fit]:
    whole = fit_series(charges)
    if whole is not None:
        return [whole]
    by_amount: dict[Decimal, list[Charge]] = defaultdict(list)
    for charge in charges:
        by_amount[charge.amount].append(charge)
    fits = []
    for members in by_amount.values():
        if len(members) == len(charges):
            continue
        fit = fit_series(members, allow_price_change=False)
        if fit is not None:
            fits.append(
                _Fit(fit.charges, fit.cadence, fit.median_interval, fit.split, partial=True)
            )
    return _chain_levels(fits)


def _chain_levels(fits: Sequence[_Fit]) -> list[_Fit]:
    """Join exact-amount series that follow one another into one series: a subscription whose
    price changed more than once. A level continues the one before it when it has the same
    cadence and starts within two cadences of that level's last charge (one skipped charge)."""
    order = sorted(fits, key=lambda f: (f.charges[0].date, f.charges[-1].date))
    successor: dict[int, int] = {}
    continued: set[int] = set()
    for i, fit in enumerate(order):
        if fit.cadence is None:
            continue
        reach = 2 * fit.cadence.days + fit.cadence.tolerance
        options = [
            j
            for j, other in enumerate(order)
            if j not in continued
            and other.cadence == fit.cadence
            and 0 < (other.charges[0].date - fit.charges[-1].date).days <= reach
        ]
        if options:
            j = min(options, key=lambda j: order[j].charges[0].date)
            successor[i] = j
            continued.add(j)
    out = []
    for i, fit in enumerate(order):
        if i in continued:
            continue
        chain = [fit]
        k = i
        while k in successor:
            k = successor[k]
            chain.append(order[k])
        if len(chain) == 1:
            out.append(fit)
            continue
        joined = tuple(c for level in chain for c in level.charges)
        split = len(joined) - len(chain[-1].charges)
        out.append(_Fit(joined, fit.cadence, _median(_intervals(joined)), split, partial=True))
    return out


def _forced(charges: Sequence[Charge]) -> _Fit:
    """A confirmed merchant that detection rejects: the nearest cadence, if any can be told."""
    cadence = median = None
    if len(charges) >= 2:
        median = _median(_intervals(charges))
        if median > 0:
            cadence = min(CADENCES, key=lambda c: abs(median - c.days) / c.days)
    return _Fit(tuple(charges), cadence, median, 0, detected=False)


def _round_days(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _amounts(charges: Sequence[Charge], split: int) -> tuple[Decimal, Decimal | None]:
    """``(typical, previous)``: after a fixed-price change (the old price held for at least two
    charges) the new price and the old one; otherwise the median of the last three charges at
    the current level, and no previous price."""
    amounts = [c.amount for c in charges]
    last = amounts[-1]
    run = next((i for i, a in enumerate(reversed(amounts)) if a != last), len(amounts))
    prior = amounts[: len(amounts) - run]
    if len(prior) >= 2 and prior[-1] == prior[-2]:
        return last, prior[-1]
    return to_money(_median(amounts[split:][-TYPICAL_OF_LAST:])), None


def _anchor_day(charges: Sequence[Charge]) -> int | None:
    """The day of the month recent charges fall on (within a couple of days), if they do."""
    recent = charges[-ANCHOR_SAMPLE:]
    counts = Counter(c.date.day for c in recent)
    anchor = max(counts, key=lambda day: (counts[day], day))

    def on_anchor(day: dt.date) -> bool:
        length = calendar.monthrange(day.year, day.month)[1]
        return abs(day.day - min(anchor, length)) <= ANCHOR_SLACK_DAYS

    hits = sum(1 for c in recent if on_anchor(c.date))
    return anchor if hits >= MIN_IN_BAND * len(recent) else None


def next_expected(
    charges: Sequence[Charge], cadence: Cadence | None, interval: int | None
) -> dt.date | None:
    """The last charge + the median interval; a monthly/quarterly/yearly series that charges on
    a fixed day of the month steps whole months to that day (clamped to the month's length)."""
    if cadence is None or not interval:
        return None
    last = charges[-1].date
    if cadence.months:
        anchor = _anchor_day(charges)
        if anchor is not None:
            year, month = periods.add_months(last.year, last.month, cadence.months)
            return periods.clamp_day(year, month, anchor)
    return last + dt.timedelta(days=interval)


def _series(
    key: str,
    fit: _Fit,
    status: str | None,
    today: dt.date,
    categories: dict[int, CategoryRef],
) -> RecurringSeries:
    charges = fit.charges
    typical, previous = _amounts(charges, fit.split)
    last = charges[-1]
    cadence = fit.cadence
    interval = _round_days(fit.median_interval) if fit.median_interval is not None else None
    expected = next_expected(charges, cadence, interval)
    monthly = to_money(typical * DAYS_PER_MONTH / cadence.length) if cadence else None
    since_last = (today - last.date).days
    active = cadence is None or since_last <= LAPSE_FACTOR * cadence.days
    return RecurringSeries(
        key=key,
        merchant=(last.merchant_name or last.name).strip(),
        search_text=search_text(last.merchant_name, last.name),
        cadence=cadence.key if cadence else None,
        cadence_label=cadence.label if cadence else "Irregular",
        interval_days=interval,
        typical_amount=typical,
        previous_amount=previous,
        last_amount=last.amount,
        last_date=last.date,
        first_date=charges[0].date,
        count=len(charges),
        next_expected=expected,
        monthly_cost=monthly,
        category=categories.get(last.category_id),
        active=active,
        late=active and expected is not None and expected < today,
        status=status,
        detected=fit.detected,
    )


def detect(session: Session, *, today: dt.date | None = None) -> RecurringOverview:
    """Every recurring series in the last 400 days, split into active, lapsed and ignored."""
    today = today or clock.today()
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    groups = _charges(session, since, today)
    overrides = {o.merchant_key: o.status for o in session.scalars(select(RecurringOverride))}
    categories = {
        c.id: CategoryRef(c.id, c.name, c.color, c.is_hidden)
        for c in session.scalars(select(Category))
    }
    found: list[RecurringSeries] = []
    for key, charges in groups.items():
        status = overrides.get(key)
        fits = _detect(charges)
        if not fits and status == CONFIRMED:
            fits = [_forced(charges)]
        found.extend(_series(key, fit, status, today, categories) for fit in fits)

    far = dt.date.max
    active = sorted(
        (s for s in found if not s.is_ignored and s.active),
        key=lambda s: (s.next_expected or far, s.merchant.casefold(), s.typical_amount),
    )
    lapsed = sorted(
        (s for s in found if not s.is_ignored and not s.active),
        key=lambda s: (-s.last_date.toordinal(), s.merchant.casefold()),
    )
    ignored = sorted(
        (s for s in found if s.is_ignored), key=lambda s: (s.merchant.casefold(), s.typical_amount)
    )
    total = to_money(sum((s.monthly_cost for s in active if s.monthly_cost is not None), ZERO))
    return RecurringOverview(today, since, tuple(active), tuple(lapsed), tuple(ignored), total)


# --------------------------------------------------------------------------- overrides


def set_override(session: Session, key: str, status: str | None) -> RecurringOverride | None:
    """Ignore (``ignored``) or keep (``confirmed``) a merchant's series; ``None`` resets it."""
    key = (key or "").strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError("That recurring charge isn't valid.")
    if status is not None and status not in RECURRING_STATUSES:
        raise ValueError(f"unknown recurring status {status!r}")
    row = session.scalars(
        select(RecurringOverride).where(RecurringOverride.merchant_key == key)
    ).one_or_none()
    if status is None:
        if row is not None:
            session.delete(row)
            session.flush()
        return None
    if row is None:
        row = RecurringOverride(merchant_key=key, status=status)
        session.add(row)
    else:
        row.status = status
    session.flush()
    return row
