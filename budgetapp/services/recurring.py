"""Recurring charges and subscriptions (INS-3).

* **Charges looked at:** posted, positive allocations in ``expense`` categories, read from
  ``v_allocations`` (so splits and suspected duplicates are already handled), one per
  transaction, dated by ``spend_date`` (the charge date, steadier than the posting date),
  grouped by :func:`merchant_key`. Weekly to quarterly series come from the last 400 days;
  yearly ones from the last three years (two renewals have to be in view).
* **A series:** at least 3 charges; a median interval of 7±2, 14±3, 30±5 or 91±10 days with
  at least 70% of the intervals in that band (a lunch place visited every 12 days on average is
  a habit, not a subscription); and every amount within 20% of the median, or one price change
  between two FLAT price levels (each within 2% of its own median, the old price charged at
  least twice: a single odd charge is not a price change), or, for a monthly bill on a fixed
  day of the month (at least 6 charges, 80% of them within 2 days of that day), every amount
  within 40% (utilities). A monthly series whose amounts vary by more than 5% (no price
  change) must charge on a fixed day of the month: filling the tank about monthly is a habit.
  A merchant that bills several subscriptions under one id (Apple: iCloud and Apple Music)
  fails the amount test or looks weekly/every two weeks; its charges of one exact amount (at
  least 3) are then tried on their own, and such series that follow one another at the same
  cadence (on the same billing day first) are one subscription whose price changed.
* **Yearly** (merchants without a recent series): every charge of the merchant in the last three
  years, a year apart (365±20 days): two within 10% of their median, or three or more as above.
  Merchants whose every charge has the same statement descriptor count as one merchant here.
* **One subscription under two keys** (CSV rows carry no merchant id; Plaid may start or stop
  sending one): a series that starts within two cadences after another one's last charge, at
  the same cadence, with an amount within 20% (40% with the same statement descriptor: a
  bill's seasonal step) and a name or statement descriptor that starts with the same word,
  continues it. Yearly renewals split across keys (one charge each) are
  joined when every charge of those merchants has the same statement descriptor (Plaid's
  ``original_description`` is the CSV's "Appears On Your Statement As").
* **For each series:** cadence; typical amount (the new price after a fixed-price change, else
  the median of the last three at the current level; yearly: the latest renewal); last amount
  and date; next expected date
  (last + median interval; a monthly, quarterly or yearly series whose last charge fell on its
  billing day takes the billing day nearest to that); monthly cost (``amount × 30.44 /
  cadence length``: weekly × 30.44/7, every 2 weeks × 30.44/14, monthly × 1, quarterly ÷ 3,
  yearly ÷ 12); category; active (charged within 1.5 × the cadence and at most 45 days
  overdue) or lapsed; late. A pending charge of about the usual amount after the last one is a
  renewal on its way: it counts for the next date and for Late.
* **Overrides** (``recurring_overrides``): ``ignored`` moves a series to the Ignored list;
  ``confirmed`` keeps it listed (a confirmed merchant even when detection fails). A series is
  listed under its merchant key, or ``<merchant key>|<amount>`` when it is one of a merchant's
  several exact-amount series (then the merchant key also applies), plus the keys of a series
  it continues; :func:`set_series_override` writes the user's choice under all of them, so
  ignoring one Apple subscription leaves the others alone and a choice made on CSV-era charges
  carries on to the Plaid ones. Choices whose series is gone are listed as orphans.

Services never commit; the routes do.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
#: Yearly series are looked for over three years: two renewals stay in view while the series is
#: active (until 45 days overdue) and for a while after it lapsed, like a monthly one.
YEARLY_LOOKBACK_DAYS = 3 * 365
#: Pending charges this recent can be a renewal on its way.
PENDING_LOOKBACK_DAYS = 45
#: Every amount within this share of the median (per price level).
AMOUNT_TOLERANCE = Decimal("0.20")
#: A price level is flat when every charge is within this share of its median.
FLAT_TOLERANCE = Decimal("0.02")
#: A monthly series whose amounts vary more than this must charge on a fixed day of the month
#: (a bill), else it is a habit (filling the tank about monthly).
STEADY_TOLERANCE = Decimal("0.05")
#: Two yearly charges must both be within this share of their median.
YEARLY_PAIR_TOLERANCE = Decimal("0.10")
#: A monthly bill on a fixed day (utilities) may vary this much around its median ...
VARIABLE_TOLERANCE = Decimal("0.40")
#: ... with at least this many charges, this share of them within the slack of one day.
VARIABLE_MIN_COUNT = 6
VARIABLE_ON_DAY = Decimal("0.80")
#: Share of the intervals that must be in the cadence's band (and of recent charges on the
#: billing day for calendar stepping).
MIN_IN_BAND = Decimal("0.70")
#: Exact-amount series covering this share of a merchant's charges replace a weekly or
#: every-two-weeks fit of the whole merchant (two monthly subscriptions half a month apart).
SPLIT_COVERAGE = Decimal("0.90")
#: Fewest charges of one exact amount tried as a price level; a level that doesn't continue or
#: precede another one needs MIN_EXACT_ALONE (three equal purchases a month apart happen).
MIN_EXACT_COUNT = 3
MIN_EXACT_ALONE = 4
#: A charge this many days from the billing day still counts as on it (weekends).
ANCHOR_SLACK_DAYS = 2
ANCHOR_SAMPLE = 6
DAYS_PER_MONTH = Decimal("30.44")
LAPSE_FACTOR = Decimal("1.5")
#: A series also lapses once its next charge is this many days overdue (yearly: ~13 months).
MAX_OVERDUE_DAYS = 45
#: The typical amount is the median of this many latest charges at the current price.
TYPICAL_OF_LAST = 3
MAX_KEY_LENGTH = 300
#: At most this many keys in one choice (a series' own keys plus the ones it inherits).
MAX_KEYS_PER_CHOICE = 24
#: ``<merchant key>|<amount>``: the key of one of a merchant's several exact-amount series.
AMOUNT_KEY_SEPARATOR = "|"

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

    @property
    def lapse_days(self) -> Decimal:
        """Days since the last charge after which the series has lapsed."""
        return min(LAPSE_FACTOR * self.days, Decimal(self.days + MAX_OVERDUE_DAYS))


WEEKLY = Cadence("weekly", "Weekly", 7, 2, Decimal(7), 3)
BIWEEKLY = Cadence("biweekly", "Every 2 weeks", 14, 3, Decimal(14), 3)
MONTHLY = Cadence("monthly", "Monthly", 30, 5, DAYS_PER_MONTH, 3, 1)
QUARTERLY = Cadence("quarterly", "Quarterly", 91, 10, DAYS_PER_MONTH * 3, 3, 3)
ANNUAL = Cadence("annual", "Yearly", 365, 20, DAYS_PER_MONTH * 12, 2, 12)
CADENCES = (WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY, ANNUAL)
_SHORT = (WEEKLY, BIWEEKLY)


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
    #: The statement descriptor (Plaid ``original_description``; the CSV's "Appears On Your
    #: Statement As"): the same text in both, so it links a CSV-era charge to a Plaid one.
    descriptor: str | None = None


@dataclass(frozen=True, slots=True)
class RecurringSeries:
    """A detected (or user-confirmed) series of charges from one merchant."""

    #: The series' own override key (``keys[0]``).
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
    #: Keys Ignore / Always list it write (this series, and any series it continues).
    keys: tuple[str, ...] = ()
    #: Other keys the status may come from; "Back to automatic" clears them too.
    also: tuple[str, ...] = ()
    #: One of several series the merchant bills (each of one exact amount).
    partial: bool = False
    #: A bill whose amount varies (a utility): the typical amount is the recent median.
    variable: bool = False
    #: The date of a pending renewal, counted for the next date and Late.
    pending_date: dt.date | None = None

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
class OrphanChoice:
    """An Ignore / Always-list choice that no current series uses (so it can be undone)."""

    key: str
    status: str
    #: The merchant's name, when the app still has a charge of it; else the key.
    name: str
    #: The amount of an exact-amount series' choice (``<merchant key>|<amount>``).
    amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RecurringOverview:
    """Everything the Recurring page shows."""

    today: dt.date
    #: Start of the window for weekly to quarterly series (yearly ones: ``yearly_since``).
    since: dt.date
    #: Active series, next expected charge first.
    active: tuple[RecurringSeries, ...]
    #: Series not seen for longer than their cadence allows, most recent first.
    lapsed: tuple[RecurringSeries, ...]
    ignored: tuple[RecurringSeries, ...]
    #: The monthly cost of the active series.
    monthly_total: Decimal
    orphans: tuple[OrphanChoice, ...] = ()
    yearly_since: dt.date | None = None

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
        return not (self.active or self.lapsed or self.ignored or self.orphans)


# --------------------------------------------------------------------------- merchant keys

_PROCESSOR_PREFIX = re.compile(
    r"^(?:sq|sqc|tst|dd|py|sp|pp|paypal|pos|ck|bt|ic|in|sms|wpy|eb|toast)\s*\*\s*", re.IGNORECASE
)
_STORE_NUMBER = re.compile(r"#\s*\S*")
_DIGITS = re.compile(r"\d+")
_PUNCTUATION = re.compile(r"[^\w&']+")
_SEARCH_CUT = re.compile(r"[*#\d]")
_ARTICLES = frozenset({"the", "a", "an"})


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


def amount_key(key: str, amount: Decimal) -> str:
    """The override key of one exact-amount series of a merchant: ``<key>|<amount>``."""
    return f"{key}{AMOUNT_KEY_SEPARATOR}{to_money(amount)}"


def split_amount_key(key: str) -> tuple[str, Decimal | None]:
    """``(merchant key, amount)`` of an :func:`amount_key` (amount None for a plain key)."""
    base, separator, tail = key.rpartition(AMOUNT_KEY_SEPARATOR)
    if separator and base:
        try:
            return base, to_money(Decimal(tail))
        except InvalidOperation, ValueError:
            pass
    return key, None


def search_text(merchant_name: str | None, name: str) -> str:
    """What to search the Transactions page for: the merchant, else the description's head."""
    if merchant_name and merchant_name.strip():
        return merchant_name.strip()
    text = _PROCESSOR_PREFIX.sub("", name.strip())
    head = _SEARCH_CUT.split(text, 1)[0].strip()
    return head or name.strip()


def _first_word(text: str | None) -> str | None:
    """The first word of a merchant's name or description, cleaned like a key."""
    if not text or not text.strip():
        return None
    words = [w for w in merchant_key(name=text).split() if w not in _ARTICLES]
    return words[0] if words else None


def _first_words(charges: Iterable[Charge]) -> frozenset[str]:
    words = set()
    for charge in charges:
        for text in (charge.merchant_name, charge.name, charge.descriptor):
            word = _first_word(text)
            if word:
                words.add(word)
    return frozenset(words)


# --------------------------------------------------------------------------- loading


def _charges(
    session: Session, since: dt.date, until: dt.date, *, pending: bool = False
) -> dict[str, list[Charge]]:
    """Positive expense allocations per transaction (posted, or pending), grouped by merchant
    key, oldest first."""
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
            Transaction.original_description,
        )
        .select_from(v_allocations)
        .join(Category, Category.id == a.category_id)
        .join(Transaction, Transaction.id == a.transaction_id)
        .where(
            Category.kind == "expense",
            a.pending.is_(pending),
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
                descriptor=row.original_description,
            )
        )
    for charges in groups.values():
        charges.sort(key=lambda c: (c.date, c.transaction_id))
    return groups


# --------------------------------------------------------------------------- amounts


def _median(values: Sequence[Decimal | int]) -> Decimal:
    ordered = sorted(Decimal(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _within(amounts: Sequence[Decimal], share: Decimal) -> bool:
    """Every amount within ``share`` of the median."""
    median = _median(amounts)
    return all(abs(x - median) <= share * median for x in amounts)


def _stable(amounts: Sequence[Decimal]) -> bool:
    return _within(amounts, AMOUNT_TOLERANCE)


def _flat(amounts: Sequence[Decimal]) -> bool:
    return _within(amounts, FLAT_TOLERANCE)


def _flat_split(amounts: Sequence[Decimal]) -> int | None:
    """``k`` when the amounts are two flat price levels split at ``k`` (the old price charged
    at least twice): one price change."""
    for k in range(2, len(amounts)):
        if _flat(amounts[:k]) and _flat(amounts[k:]):
            return k
    return None


def _price_split(amounts: Sequence[Decimal], *, allow_change: bool) -> int | None:
    """Where the current price starts: 0 when every amount is within 20% of the median; ``k``
    after one price change between two flat levels; else None."""
    if _stable(amounts):
        return 0
    return _flat_split(amounts) if allow_change else None


def _steady(amounts: Sequence[Decimal]) -> bool:
    """Within 5% of the median, or one price change between two flat levels."""
    return _within(amounts, STEADY_TOLERANCE) or _flat_split(amounts) is not None


def _close(a: Decimal, b: Decimal, share: Decimal) -> bool:
    return abs(a - b) <= share * max(a, b)


# --------------------------------------------------------------------------- billing days


def _anchor_dates(day: dt.date, anchor: int) -> list[dt.date]:
    """The billing day (clamped to the month's length) in ``day``'s month and either side."""
    out = []
    for delta in (-1, 0, 1):
        year, month = periods.add_months(day.year, day.month, delta)
        out.append(periods.clamp_day(year, month, anchor))
    return out


def _anchor_distance(day: dt.date, anchor: int) -> int:
    """Days from ``day`` to the nearest billing day (Oct 1 is 1 day from Sep 30)."""
    return min(abs((d - day).days) for d in _anchor_dates(day, anchor))


def _anchor_day(
    charges: Sequence[Charge], *, sample: int = ANCHOR_SAMPLE, share: Decimal = MIN_IN_BAND
) -> int | None:
    """The day of the month the last ``sample`` charges fall on (within a couple of days), if
    at least ``share`` of them do."""
    recent = charges[-sample:]
    counts = Counter(c.date.day for c in recent)
    anchor = max(counts, key=lambda day: (counts[day], day))
    hits = sum(1 for c in recent if _anchor_distance(c.date, anchor) <= ANCHOR_SLACK_DAYS)
    return anchor if hits >= share * len(recent) else None


def _variable_bill(charges: Sequence[Charge]) -> bool:
    """A bill on a fixed day of the month whose amount varies (a utility)."""
    if len(charges) < VARIABLE_MIN_COUNT:
        return False
    if _anchor_day(charges, sample=len(charges), share=VARIABLE_ON_DAY) is None:
        return False
    return _within([c.amount for c in charges], VARIABLE_TOLERANCE)


# --------------------------------------------------------------------------- detection


@dataclass(frozen=True, slots=True)
class _Fit:
    charges: tuple[Charge, ...]
    cadence: Cadence | None
    median_interval: Decimal | None
    split: int
    detected: bool = True
    #: An exact-amount series within a merchant that bills several amounts.
    partial: bool = False
    variable: bool = False


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
    amounts = [c.amount for c in charges]
    if cadence is ANNUAL and len(charges) == 2:
        if _within(amounts, YEARLY_PAIR_TOLERANCE):
            return _Fit(tuple(charges), cadence, median, 0)
        return None
    split = _price_split(amounts, allow_change=allow_price_change)
    if split is not None:
        if cadence is MONTHLY and not _steady(amounts) and _anchor_day(charges) is None:
            return None  # varying amounts on no fixed day: a habit, not a bill
        return _Fit(tuple(charges), cadence, median, split)
    if allow_price_change and cadence is MONTHLY and _variable_bill(charges):
        return _Fit(tuple(charges), cadence, median, 0, variable=True)
    return None


def _exact_amount_fits(charges: Sequence[Charge]) -> list[_Fit]:
    """Series of one exact amount each (a merchant that bills several subscriptions), with
    the price levels of one subscription joined."""
    by_amount: dict[Decimal, list[Charge]] = defaultdict(list)
    for charge in charges:
        by_amount[charge.amount].append(charge)
    fits = []
    for members in by_amount.values():
        if len(members) == len(charges) or len(members) < MIN_EXACT_COUNT:
            continue
        fit = fit_series(members, allow_price_change=False)
        if fit is not None and fit.cadence is not ANNUAL:
            fits.append(replace(fit, partial=True))
    chained = _chain_levels(fits)
    alone = {id(f) for f in fits}
    return [f for f in chained if id(f) not in alone or len(f.charges) >= MIN_EXACT_ALONE]


def _detect_recent(charges: Sequence[Charge]) -> list[_Fit]:
    """Weekly to quarterly series in one merchant's charges of the last 400 days."""
    whole = fit_series(charges)
    if whole is not None and whole.cadence is ANNUAL:
        whole = None
    if whole is not None and whole.cadence not in _SHORT:
        return [whole]
    exact = _exact_amount_fits(charges)
    if whole is None:
        return exact
    covered = sum(len(f.charges) for f in exact)
    longer = all(f.cadence is not None and f.cadence.days > whole.cadence.days for f in exact)
    if len(exact) >= 2 and longer and covered >= SPLIT_COVERAGE * len(charges):
        return exact
    return [whole]


def _successor_rank(level: _Fit, follower: _Fit) -> tuple[bool, Decimal, int]:
    """How well ``follower`` continues ``level``: on its billing day first, then the closest
    amount, then the shortest gap."""
    anchor = _anchor_day(level.charges) if level.cadence and level.cadence.months else None
    first = follower.charges[0]
    off_day = anchor is None or _anchor_distance(first.date, anchor) > ANCHOR_SLACK_DAYS
    a, b = level.charges[-1].amount, first.amount
    return off_day, max(a, b) / min(a, b), (first.date - level.charges[-1].date).days


def _chain_levels(fits: Sequence[_Fit]) -> list[_Fit]:
    """Join exact-amount series that follow one another into one series: a subscription whose
    price changed. A level continues the one before it when it has the same cadence and starts
    within two cadences of that level's last charge (one skipped charge)."""
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
            j = min(options, key=lambda j: _successor_rank(fit, order[j]))
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


# --------------------------------------------------------------------------- one subscription


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A series with the keys it is listed under (several when it continues another)."""

    fit: _Fit
    #: Merchant keys of its charges, newest first.
    merchants: tuple[str, ...]
    keys: tuple[str, ...]
    also: tuple[str, ...]
    words: frozenset[str]
    #: Statement descriptors of its charges (cleaned like keys).
    descriptors: frozenset[str] = frozenset()


def _descriptors(charges: Iterable[Charge]) -> frozenset[str]:
    return frozenset(merchant_key(name=c.descriptor) for c in charges if c.descriptor)


def _candidate(merchant: str, fit: _Fit) -> _Candidate:
    if fit.partial:
        levels = list(dict.fromkeys(c.amount for c in reversed(fit.charges)))
        keys = (amount_key(merchant, levels[0]),)
        also = (*(amount_key(merchant, a) for a in levels[1:]), merchant)
    else:
        keys, also = (merchant,), ()
    charges = fit.charges
    return _Candidate(fit, (merchant,), keys, also, _first_words(charges), _descriptors(charges))


def _typical_at(charges: Sequence[Charge]) -> Decimal:
    return _median([c.amount for c in charges])


def _continues(earlier: _Candidate, later: _Candidate) -> bool:
    """``later`` is ``earlier`` under another key (CSV era → Plaid era, say)."""
    a, b = earlier.fit, later.fit
    if a.cadence is None or a.cadence != b.cadence or not (a.detected and b.detected):
        return False
    if set(earlier.merchants) & set(later.merchants) or not earlier.words & later.words:
        return False
    gap = (b.charges[0].date - a.charges[-1].date).days
    if not 0 < gap <= 2 * a.cadence.days + a.cadence.tolerance:
        return False
    # The same statement descriptor (or a varying bill): a seasonal step in a bill is no reason
    # to keep the halves apart.
    same_descriptor = bool(earlier.descriptors & later.descriptors)
    varies = a.variable or b.variable or same_descriptor
    share = VARIABLE_TOLERANCE if varies else AMOUNT_TOLERANCE
    before = _typical_at(a.charges[-TYPICAL_OF_LAST:])
    after = _typical_at(b.charges[:TYPICAL_OF_LAST])
    return _close(before, after, share)


def _join_keys(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Join series that continue one another under different merchant keys."""
    order = sorted(candidates, key=lambda c: (c.fit.charges[0].date, c.fit.charges[-1].date))
    successor: dict[int, int] = {}
    continued: set[int] = set()
    for i, earlier in enumerate(order):
        options = [
            j
            for j, later in enumerate(order)
            if j != i and j not in continued and _continues(earlier, later)
        ]
        if options:
            last = earlier.fit.charges[-1].date
            j = min(options, key=lambda j: (order[j].fit.charges[0].date - last).days)
            successor[i] = j
            continued.add(j)
    out = []
    for i, first in enumerate(order):
        if i in continued:
            continue
        chain = [first]
        k = i
        while k in successor:
            k = successor[k]
            chain.append(order[k])
        out.append(first if len(chain) == 1 else _joined(chain))
    return out


def _yearly(groups: Mapping[str, Sequence[Charge]]) -> list[_Candidate]:
    """Yearly series over three years among merchants without a recent series. Merchants whose
    every charge has the same statement descriptor are one merchant here: a renewal from the
    CSV era and one from Plaid join up, and a museum visited in both eras is not "yearly"."""
    families: dict[tuple[str, str], list[str]] = defaultdict(list)
    for merchant, charges in groups.items():
        described = {merchant_key(name=c.descriptor) if c.descriptor else "" for c in charges}
        family = described.pop() if len(described) == 1 else ""
        families[("descriptor", family) if family else ("merchant", merchant)].append(merchant)
    out = []
    for family in sorted(families):
        members = families[family]
        charges = sorted(
            (c for m in members for c in groups[m]), key=lambda c: (c.date, c.transaction_id)
        )
        fit = fit_series(charges)
        if fit is None or fit.cadence is not ANNUAL:
            continue
        merchants = tuple(sorted(members, key=lambda m: (groups[m][-1].date, m), reverse=True))
        out.append(
            _Candidate(fit, merchants, merchants, (), _first_words(charges), _descriptors(charges))
        )
    return out


def _joined(chain: Sequence[_Candidate]) -> _Candidate:
    newest = chain[-1].fit
    charges = tuple(c for cand in chain for c in cand.fit.charges)
    split = _price_split([c.amount for c in charges], allow_change=True)
    if split is None:
        split = len(charges) - len(newest.charges) + newest.split
    fit = _Fit(
        charges,
        newest.cadence,
        _median(_intervals(charges)),
        split,
        partial=any(c.fit.partial for c in chain),
        variable=any(c.fit.variable for c in chain),
    )
    newest_first = list(reversed(chain))
    keys = tuple(dict.fromkeys(k for cand in newest_first for k in cand.keys))
    also = tuple(dict.fromkeys(k for cand in newest_first for k in cand.also if k not in keys))
    merchants = tuple(dict.fromkeys(m for cand in newest_first for m in cand.merchants))
    return _Candidate(
        fit,
        merchants,
        keys,
        also,
        frozenset().union(*(c.words for c in chain)),
        frozenset().union(*(c.descriptors for c in chain)),
    )


# --------------------------------------------------------------------------- series


def _round_days(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _amounts(
    charges: Sequence[Charge], split: int, cadence: Cadence | None = None
) -> tuple[Decimal, Decimal | None]:
    """``(typical, previous)``: after a fixed-price change (the old price held for at least two
    charges) the new price and the old one; otherwise the median of the last three charges at
    the current level, and no previous price. A yearly series renews at its latest price (the
    one before, when different, is the previous price)."""
    amounts = [c.amount for c in charges]
    last = amounts[-1]
    if cadence is ANNUAL:
        before = amounts[-2] if len(amounts) >= 2 and amounts[-2] != last else None
        return last, before
    run = next((i for i, a in enumerate(reversed(amounts)) if a != last), len(amounts))
    prior = amounts[: len(amounts) - run]
    if len(prior) >= 2 and prior[-1] == prior[-2]:
        return last, prior[-1]
    return to_money(_median(amounts[split:][-TYPICAL_OF_LAST:])), None


def next_expected(
    charges: Sequence[Charge], cadence: Cadence | None, interval: int | None
) -> dt.date | None:
    """The last charge + the median interval. A monthly, quarterly or yearly series that
    charges on a fixed day of the month, and whose last charge was on it, takes the billing day
    nearest to that instead (so it never drifts, and a charge that slipped past a month end or
    a billing day that moved doesn't push it a month out)."""
    if cadence is None or not interval:
        return None
    last = charges[-1].date
    step = last + dt.timedelta(days=interval)
    if cadence.months:
        anchor = _anchor_day(charges)
        if anchor is not None and _anchor_distance(last, anchor) <= ANCHOR_SLACK_DAYS:
            return min(_anchor_dates(step, anchor), key=lambda d: (abs((d - step).days), d))
    return step


def _pending_renewal(
    candidate: _Candidate,
    typical: Decimal,
    pending: Mapping[str, Sequence[Charge]],
) -> Charge | None:
    """The latest pending charge of the merchant after the last one, of about the usual
    amount: the renewal on its way."""
    fit = candidate.fit
    last = fit.charges[-1].date
    share = VARIABLE_TOLERANCE if fit.variable else AMOUNT_TOLERANCE
    found = [
        charge
        for merchant in candidate.merchants
        for charge in pending.get(merchant, ())
        if charge.date > last and _close(charge.amount, typical, share)
    ]
    return max(found, key=lambda c: (c.date, c.transaction_id)) if found else None


def _series(
    candidate: _Candidate,
    status: str | None,
    today: dt.date,
    categories: Mapping[int, CategoryRef],
    pending: Mapping[str, Sequence[Charge]],
) -> RecurringSeries:
    fit = candidate.fit
    charges = fit.charges
    typical, previous = _amounts(charges, fit.split, fit.cadence)
    last = charges[-1]
    cadence = fit.cadence
    interval = _round_days(fit.median_interval) if fit.median_interval is not None else None
    renewal = _pending_renewal(candidate, typical, pending) if cadence else None
    seen = (*charges, renewal) if renewal else charges
    expected = next_expected(seen, cadence, interval)
    since_last = (today - seen[-1].date).days
    # A confirmed one-off charge counts as active while it is in the 13-month window.
    limit = cadence.lapse_days if cadence else LOOKBACK_DAYS
    active = since_last <= limit
    monthly = to_money(typical * DAYS_PER_MONTH / cadence.length) if cadence else None
    return RecurringSeries(
        key=candidate.keys[0],
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
        keys=candidate.keys,
        also=candidate.also,
        partial=fit.partial,
        variable=fit.variable,
        pending_date=renewal.date if renewal else None,
    )


def _status(candidate: _Candidate, overrides: Mapping[str, str]) -> str | None:
    """The user's choice: the series' own keys first, then the keys it inherits."""
    for key in (*candidate.keys, *candidate.also):
        if key in overrides:
            return overrides[key]
    return None


def _orphans(
    session: Session,
    overrides: Mapping[str, str],
    used: set[str],
    groups: Mapping[str, Sequence[Charge]],
) -> tuple[OrphanChoice, ...]:
    out = []
    for key, status in overrides.items():
        if key in used:
            continue
        merchant, amount = split_amount_key(key)
        charges = groups.get(merchant)
        if charges:
            latest = charges[-1]
            name = (latest.merchant_name or latest.name).strip()
        else:
            row = session.execute(
                select(Transaction.merchant_name, Transaction.name)
                .where(Transaction.merchant_entity_id == merchant)
                .order_by(Transaction.date.desc(), Transaction.id.desc())
                .limit(1)
            ).first()
            name = (row.merchant_name or row.name).strip() if row else merchant
        out.append(OrphanChoice(key, status, name, amount))
    return tuple(sorted(out, key=lambda o: (o.name.casefold(), o.amount or ZERO, o.key)))


def detect(session: Session, *, today: dt.date | None = None) -> RecurringOverview:
    """Every recurring series (module docstring), split into active, lapsed and ignored."""
    today = today or clock.today()
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    yearly_since = today - dt.timedelta(days=YEARLY_LOOKBACK_DAYS)
    groups = _charges(session, yearly_since, today)
    pending = _charges(
        session, today - dt.timedelta(days=PENDING_LOOKBACK_DAYS), today, pending=True
    )
    overrides = {o.merchant_key: o.status for o in session.scalars(select(RecurringOverride))}
    categories = {
        c.id: CategoryRef(c.id, c.name, c.color, c.is_hidden)
        for c in session.scalars(select(Category))
    }
    candidates: list[_Candidate] = []
    without: dict[str, list[Charge]] = {}
    for merchant, history in groups.items():
        fits = _detect_recent([c for c in history if c.date >= since])
        if fits:
            candidates.extend(_candidate(merchant, fit) for fit in fits)
        else:
            without[merchant] = history
    yearly = _yearly(without)
    candidates.extend(yearly)
    in_yearly = {m for candidate in yearly for m in candidate.merchants}
    for merchant, history in without.items():
        if merchant not in in_yearly and overrides.get(merchant) == CONFIRMED:
            recent = [c for c in history if c.date >= since]
            fit = _forced(recent if len(recent) >= 2 else history)
            candidates.append(_candidate(merchant, fit))

    found: list[RecurringSeries] = []
    used: set[str] = set()
    for candidate in _join_keys(candidates):
        used.update(candidate.keys, candidate.also)
        status = _status(candidate, overrides)
        found.append(_series(candidate, status, today, categories, pending))

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
    return RecurringOverview(
        today,
        since,
        tuple(active),
        tuple(lapsed),
        tuple(ignored),
        total,
        _orphans(session, overrides, used, groups),
        yearly_since,
    )


# --------------------------------------------------------------------------- overrides


def _clean_key(key: str) -> str:
    key = (key or "").strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError("That recurring charge isn't valid.")
    return key


def set_override(session: Session, key: str, status: str | None) -> RecurringOverride | None:
    """Ignore (``ignored``) or keep (``confirmed``) the series listed under ``key``; ``None``
    resets it."""
    key = _clean_key(key)
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


def set_series_override(
    session: Session, keys: Sequence[str], status: str | None, *, also: Sequence[str] = ()
) -> None:
    """The page's Ignore / Always list it / Back to automatic for one series.

    ``keys`` are the series' own keys (``RecurringSeries.keys``): the choice is written under
    each. ``None`` resets the series: ``keys`` and ``also`` (the keys its status may come from)
    are all cleared. Raises ``ValueError`` on bad input, before changing anything.
    """
    own = list(dict.fromkeys(_clean_key(k) for k in keys))
    extra = [k for k in dict.fromkeys(_clean_key(k) for k in also) if k not in own]
    if not own or len(own) + len(extra) > MAX_KEYS_PER_CHOICE:
        raise ValueError("That recurring charge isn't valid.")
    if status is not None and status not in RECURRING_STATUSES:
        raise ValueError(f"unknown recurring status {status!r}")
    for key in own:
        set_override(session, key, status)
    if status is None:
        for key in extra:
            set_override(session, key, None)
