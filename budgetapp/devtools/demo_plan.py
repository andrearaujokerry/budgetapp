"""The demo data, planned in memory (no database): deterministic for ``(seed, today, now)``.

The planner simulates one card's ledger over about 24 months, then decides how each transaction
reaches the app:

* **Ledger.** Daily habits, monthly bills and subscriptions, yearly charges (card fee, renters
  insurance, car registration), trips, refunds, Amex Offer credits and Reward Dollars, all with
  Plaid-style posting lags (authorized 0–3 days before posting). Seasons show (summer evenings
  out, holiday shopping and gifts, PG&E's winter bills). Nothing is bought in person at home
  while the persona is away, and a charge exists only once it reached Plaid by the last sync
  before ``now`` (bills arrive overnight, everyday charges during the day). Statement closing
  dates drift by a day or two around the 27th. AUTOPAY pays each statement balance on its due
  date, except one month paid in part (interest on the next closing date) and one month with an
  extra mobile payment.
* **Plaid or CSV.** Rows posted on or after the Plaid history start (about 18 months back) came
  from Plaid; older rows came from an extended Amex CSV download that ran three weeks into the
  Plaid history. In that overlap every CSV row paired with its Plaid row (which carries the
  Reference), except two hotel/car-rental rows whose CSV date is the check-in/pick-up day, five
  days before Plaid's authorized date: they stayed as ``suspect`` duplicates.
* **Statements.** The last 12 closed cycles, with every reported value computed from the ledger.
  One Lisbon charge is a cent higher on the statement than in Plaid's data, so exactly one cycle
  (and its Gate D check) is off by $0.01.

The current month is shaped (on any day of the month) so that Health & Fitness is at about 85 % of
its budget, Entertainment is over budget and the other budgeted categories stay under 75 %.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import math
import random
import string
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from budgetapp.devtools import demo_catalog as cat
from budgetapp.devtools.demo_catalog import Merchant
from budgetapp.money import CENT, ZERO
from budgetapp.periods import add_months, clamp_day

DAY = dt.timedelta(days=1)
#: Amex supplied about 18 months of history through Plaid.
HISTORY_DAYS = 548
#: Linked 340 days ago: Amex consent (12 months) is due in 25 days, inside the 30-day reminder.
LINKED_DAYS_AGO = 340
#: The CSV download runs this many days into Plaid's history (that overlap was reconciled).
CSV_OVERLAP_DAYS = 21
STATEMENT_COUNT = 12
PAYMENT_DUE_DAYS = 25
SETTLE_DAYS = 3
SYNC_DAYS = 4
SYNC_SLOTS = ((0, 15), (6, 15), (12, 15), (18, 15))
ID_ALPHABET = string.ascii_letters + string.digits
#: PFC v2 codes Plaid gives the rows that have no merchant.
PAYMENT_PFC = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
INTEREST_PFC = ("BANK_FEES", "BANK_FEES_INTEREST_CHARGE")
ANNUAL_FEE_PFC = ("BANK_FEES", "BANK_FEES_OTHER_BANK_FEES")
FX_FEE_PFC = ("BANK_FEES", "BANK_FEES_FOREIGN_TRANSACTION_FEES")
CREDIT_PFC = ("INCOME", "INCOME_OTHER")
#: Plaid labels one REI return as a transfer in; the refund rule puts it back with REI's category.
ODD_REFUND_PFC = ("TRANSFER_IN", "TRANSFER_IN_OTHER_TRANSFER_IN")
SYNC_FAILURE = (
    "INSTITUTION_NOT_RESPONDING",
    "This financial institution is not currently responding to requests. We apologize for the "
    "inconvenience.",
)


def local_time(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    """``day hour:minute`` in the Mac's time zone, as an aware UTC datetime."""
    return dt.datetime.combine(day, dt.time(hour, minute)).astimezone().astimezone(dt.UTC)


def month_start(day: dt.date, back: int = 0) -> dt.date:
    """The first day of the month ``back`` months before ``day``'s month."""
    year, month = add_months(day.year, day.month, -back)
    return dt.date(year, month, 1)


def _cents(value: int) -> Decimal:
    return (Decimal(value) / 100).quantize(CENT)


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _long_date(moment: dt.datetime) -> str:
    local = moment.astimezone()
    return f"{local:%b} {local.day}, {local.year}"


def entity_id(key: str) -> str:
    """A stable Plaid-style ``merchant_entity_id`` for a catalog merchant (same for every seed)."""
    digest = hashlib.sha256(f"budgetapp-demo-merchant:{key}".encode()).digest()
    return "".join(ID_ALPHABET[b % len(ID_ALPHABET)] for b in digest[:22])


# --------------------------------------------------------------------------- plan types


@dataclass(slots=True)
class Row:
    """One transaction of the true ledger (and, after planning, how it is stored)."""

    seq: int
    kind: str  # purchase | refund | credit | payment | interest | annual_fee | fx_fee
    auth: dt.date
    amount: Decimal
    name: str
    raw: str
    category: str  # expected app category (planning only; the writer asks the Categorizer)
    merchant: Merchant | None = None
    merchant_name: str | None = None
    pfc: tuple[str, str] | None = None
    confidence: str | None = None
    code: str | None = None
    amex: str | None = None
    channel: str = "other"
    lag: int = 0
    scheduled: bool = False
    trip: str | None = None
    foreign: bool = False
    #: Amex's statement amount minus Plaid's (one Lisbon charge: +$0.01).
    statement_extra: Decimal = ZERO
    posted: dt.date | None = None
    source: str = "plaid"
    plaid_id: str | None = None
    pending_id: str | None = None
    csv_reference: str | None = None
    csv_date: dt.date | None = None
    paired: bool = False
    suspect: bool = False
    account_owner: str | None = None
    arrival_hour: int = 12
    # user state
    user_category: str | None = None
    notes: str | None = None
    tags: list[str] = field(default_factory=list)
    splits: list[tuple[str, Decimal, str | None]] = field(default_factory=list)
    split_amount: Decimal | None = None  # splits were made for this amount (needs_review row)
    edited_at: dt.datetime | None = None

    @property
    def pending(self) -> bool:
        return self.posted is None

    @property
    def true_amount(self) -> Decimal:
        return self.amount + self.statement_extra

    @property
    def bucket(self) -> str:
        """Statement bucket (ARCHITECTURE §6): payments_credits, interest, fees or charges."""
        if self.amount < 0:
            return "payments_credits"
        if self.kind == "interest":
            return "interest"
        if self.kind in ("annual_fee", "fx_fee"):
            return "fees"
        return "charges"

    @property
    def location(self) -> dict[str, Any] | None:
        """Plaid's ``location`` object (all fields null for online merchants)."""
        if self.source != "plaid":
            return None
        place = self.merchant.place if self.merchant else None
        if place is None:
            return dict.fromkeys(
                ("address", "city", "region", "postal_code", "country", "lat", "lon"),
                None,
            ) | {"store_number": None}
        return {
            "address": place.address,
            "city": place.city,
            "region": place.region,
            "postal_code": place.postal_code,
            "country": place.country,
            "lat": Decimal(place.lat),
            "lon": Decimal(place.lon),
            "store_number": place.store_number,
        }

    @property
    def csv_description(self) -> str:
        """The Amex CSV ``Description``: the merchant text, then the city in a padded column."""
        tail = self.raw[len(self.name) :].strip() if self.raw.startswith(self.name) else ""
        return f"{self.name:<26}{tail}" if tail else self.raw


@dataclass(frozen=True, slots=True)
class StatementPlan:
    closing_date: dt.date
    previous_balance: Decimal
    payments_credits: Decimal  # a positive amount: New = Previous − this + charges + fees + int.
    new_charges: Decimal
    fees: Decimal
    interest: Decimal
    new_balance: Decimal
    checked_at: dt.datetime | None
    notes: str | None
    #: Only on the earliest statement: its cycle start (the previous closing date isn't stored).
    period_start_override: dt.date | None = None


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    category: str | None  # None = the overall target
    period: str
    amount: Decimal | None
    effective_from: dt.date
    rollover: bool = False


@dataclass(frozen=True, slots=True)
class SyncRunPlan:
    trigger: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime
    added: int
    modified: int
    removed: int
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class AlertPlan:
    kind: str
    dedupe_key: str
    title: str
    message: str
    created_at: dt.datetime


@dataclass(slots=True)
class Plan:
    """Everything the writer stores (see :func:`build_plan`)."""

    seed: int
    today: dt.date
    now: dt.datetime
    rows: list[Row]
    closings: list[dt.date]
    statements: list[StatementPlan]
    budgets: list[BudgetPlan]
    sync_runs: list[SyncRunPlan]
    alerts: list[AlertPlan]
    item: dict[str, Any]
    account: dict[str, Any]
    csv_import: dict[str, Any]
    settings: dict[str, Any]
    recurring_ignored: list[str]
    plaid_history_start: dt.date
    linked_at: dt.datetime
    #: Trip name → (first, last) day away from home.
    trips: dict[str, tuple[dt.date, dt.date]] = field(default_factory=dict)


# --------------------------------------------------------------------------- the planner


class _Planner:
    def __init__(self, seed: int, today: dt.date, now: dt.datetime) -> None:
        self.seed = seed
        self.rng = random.Random(f"budgetapp-demo:{seed}")
        self.today = today
        self.now = now
        self.month = today.replace(day=1)
        self.rows: list[Row] = []
        self._seq = itertools.count()
        self.away: set[dt.date] = set()
        self.closings = self._closings()
        self.start = self.closings[0] + DAY
        self.cutover = today - dt.timedelta(days=HISTORY_DAYS)
        self.link_day = today - dt.timedelta(days=LINKED_DAYS_AGO)
        self.linked_at = local_time(self.link_day, 20, 41)
        self.trips: dict[str, tuple[dt.date, dt.date]] = {
            "seattle": (today - dt.timedelta(days=640), today - dt.timedelta(days=637)),
            "la": (self.cutover + dt.timedelta(days=7), self.cutover + dt.timedelta(days=12)),
            "tahoe": (today - dt.timedelta(days=300), today - dt.timedelta(days=298)),
            "nyc": (today - dt.timedelta(days=230), today - dt.timedelta(days=226)),
            "lisbon": (today - dt.timedelta(days=125), today - dt.timedelta(days=117)),
        }
        for first, last in self.trips.values():
            day = first
            while day <= last:
                self.away.add(day)
                day += DAY
        #: The last scheduled sync before ``now``: a charge that reached Plaid later isn't stored.
        self.last_sync = self.sync_slots()[-1][0]

    # ------------------------------------------------------------------ helpers

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def pick(self, *weighted: tuple[str, int]) -> str:
        keys = [k for k, _ in weighted]
        return self.rng.choices(keys, weights=[w for _, w in weighted])[0]

    def amount(self, lo: int, hi: int) -> Decimal:
        """A random amount in cents between ``lo`` and ``hi``, skewed towards the low end."""
        if lo >= hi:
            return _cents(lo)
        return _cents(int(self.rng.triangular(lo, hi, lo + (hi - lo) * 0.3)))

    def code(self, length: int = 9) -> str:
        return "".join(
            self.rng.choice(string.ascii_uppercase + string.digits) for _ in range(length)
        )

    def plaid_id(self) -> str:
        return "".join(self.rng.choice(ID_ALPHABET) for _ in range(37))

    def lag(self, day: dt.date) -> int:
        """Days from authorization to posting (Friday charges post on Monday)."""
        weekday = day.weekday()
        if weekday == 4:
            return 3
        if weekday == 5:
            return 2
        if weekday == 6:
            return 1
        return self.rng.choice((0, 1, 1, 1, 1, 2, 2, 2, 3))

    def add(self, row: Row) -> Row:
        self.rows.append(row)
        return row

    def purchase(
        self,
        day: dt.date,
        key: str,
        amount: Decimal | None = None,
        *,
        scheduled: bool = False,
        billed: bool = False,
        trip: str | None = None,
        lag: int | None = None,
    ) -> Row | None:
        """A charge at merchant ``key`` authorized on ``day``.

        ``None`` outside the window, and for an everyday charge that reached Plaid after the last
        sync (it arrives with the next one). ``scheduled``: a bill or a planned visit, always kept.
        ``billed``: a subscription or bill, charged overnight wherever the persona is; any other
        in-person charge at home that falls on a trip day moves to the day after the trip.
        """
        m = cat.MERCHANTS[key]
        if trip is None and not billed and day in self.away and cat.is_home_store(m):
            day = self._after_trip(day)
        if day < self.start or day > self.today:
            return None
        variant = self.rng.choice(m.variants) if m.variants else ""
        code = self.code(10) if "{code}" in m.name or "{code}" in m.raw else ""
        name = m.name.replace("{v}", variant).replace("{code}", code)
        raw = m.raw.replace("{v}", variant).replace("{code}", code)
        value = amount if amount is not None else self.amount(m.lo, m.hi)
        lag = self.lag(day) if lag is None else lag
        hour = self.rng.randint(8, 21)
        arrival = 1 + hour % 5 if billed else hour  # bills: 1–5 am, before the 06:15 sync
        if not scheduled and local_time(day, arrival) > self.last_sync:
            return None
        return self.add(
            Row(
                seq=next(self._seq),
                kind="purchase",
                auth=day,
                amount=value,
                name=name,
                raw=raw,
                category=m.category,
                merchant=m,
                merchant_name=m.merchant_name,
                pfc=m.pfc,
                confidence=m.confidence if m.pfc else None,
                code="purchase",
                amex=m.amex,
                channel=m.channel,
                lag=lag,
                scheduled=scheduled,
                trip=trip,
                foreign=m.foreign,
                arrival_hour=arrival,
            )
        )

    def _after_trip(self, day: dt.date) -> dt.date:
        """The first day home after the trip that ``day`` falls in."""
        for first, last in self.trips.values():
            if first <= day <= last:
                return last + DAY
        return day

    def special(
        self,
        kind: str,
        day: dt.date,
        amount: Decimal,
        name: str,
        raw: str,
        category: str,
        pfc: tuple[str, str],
        code: str,
        amex: str | None,
    ) -> Row:
        """A row without a merchant (payments, fees, interest, statement credits)."""
        return self.add(
            Row(
                seq=next(self._seq),
                kind=kind,
                auth=day,
                amount=amount,
                name=name,
                raw=raw,
                category=category,
                pfc=pfc,
                confidence="VERY_HIGH",
                code=code,
                amex=amex,
                channel="other",
                lag=0,
            )
        )

    # ------------------------------------------------------------------ calendar

    def _closings(self) -> list[dt.date]:
        """True closing dates: around the 27th, drifting a day or two, from ~24 months back."""
        out: list[dt.date] = []
        for offset in range(-24, 2):
            year, month = add_months(self.today.year, self.today.month, offset)
            base = clamp_day(year, month, cat.CLOSING_DAY)
            closing = base + dt.timedelta(days=self.rng.choice((0, 0, 0, 1, -1, 1, -1, 2, -2)))
            if closing.month != month or (out and not 27 <= (closing - out[-1]).days <= 33):
                closing = base  # same month, and cycles well inside 25–35 days (never flagged)
            out.append(closing)
        return out

    def days(self) -> Iterable[dt.date]:
        day = self.start
        while day <= self.today:
            yield day
            day += DAY

    def monthly(self, back: int, day_of_month: int) -> dt.date:
        year, month = add_months(self.today.year, self.today.month, -back)
        return clamp_day(year, month, day_of_month)

    # ------------------------------------------------------------------ the ledger

    def daily(self, day: dt.date) -> None:
        if day in self.away:
            return
        weekend = day.weekday() >= 5
        friday_saturday = day.weekday() in (4, 5)
        # Seasons: long summer evenings out; holiday shopping from Thanksgiving week to Dec 20.
        out = 1.3 if day.month in (6, 7, 8) else 1.0
        holiday = (day.month == 11 and day.day >= 22) or (day.month == 12 and day.day <= 20)
        p = self.purchase
        if self.chance(0.55 if weekend else 0.75):
            p(day, self.pick(("starbucks", 3), ("blue_bottle", 4), ("philz", 3)))
        if self.chance(0.12):
            p(day, self.pick(("philz", 2), ("starbucks", 1)))
        if self.chance(0.16):
            p(day, "arizmendi")
        if not weekend and self.chance(0.3):
            p(day, self.pick(("sweetgreen", 4), ("chipotle", 3), ("la_taqueria", 2), ("souvla", 2)))
        if weekend and self.chance(0.3):
            p(day, self.pick(("tartine", 3), ("souvla", 1)))
        if self.chance((0.14 if friday_saturday else 0.035) * out):
            p(day, self.pick(("nopa", 2), ("zuni", 1), ("delfina", 2), ("la_taqueria", 3)))
        if self.chance((0.1 if friday_saturday else 0.015) * out):
            p(day, "zeitgeist")
        if self.chance(0.06):
            p(day, self.pick(("doordash", 3), ("uber_eats", 2)))
        if self.chance(0.42 if weekend else 0.3):
            p(
                day,
                self.pick(("whole_foods", 4), ("trader_joes", 4), ("safeway", 2), ("bi_rite", 3)),
                self.amount(1100, 9800),
            )
        if self.chance(0.14):
            p(day, "mission_market")
        if self.chance(0.17 if weekend else 0.11):
            p(day, self.pick(("uber", 3), ("lyft", 2)))
        if self.chance(0.14):
            p(day, "paybyphone")
        if self.chance(0.14):
            p(day, "bay_wheels")
        if self.chance(0.24 if holiday else 0.1):
            p(day, "amazon")
        boost = {"target": 3.0, "uniqlo": 3.0, "best_buy": 3.0, "sephora": 2.0, "green_apple": 2.0}
        for key, chance in (
            ("kindle", 0.05),
            ("target", 0.03),
            ("uniqlo", 0.01),
            ("rei", 0.012),
            ("green_apple", 0.03),
            ("best_buy", 0.006),
            ("ticketmaster", 0.01),
            ("sfmoma", 0.008),
            ("steam", 0.025),
            ("bowling", 0.008),
            ("sephora", 0.012),
            ("cleaners", 0.04),
            ("cole_hardware", 0.025),
            ("home_depot", 0.008),
            ("usps", 0.02),
            ("ups_store", 0.006),
            ("petco", 0.02),
            ("venmo", 0.005),
            ("popup", 0.006),
            ("udemy", 0.004),
        ):
            if self.chance(chance * boost.get(key, 1.0) if holiday else chance):
                p(day, key)
        if self.chance(0.035):
            p(day, self.pick(("cvs", 3), ("walgreens", 2)))
        if 4 <= day.month <= 9 and self.chance(0.018):
            p(day, "giants")
        if day.month == 12 and day.day <= 20 and self.chance(0.32):
            p(day, self.pick(("paper_source", 2), ("uncommon_goods", 3), ("etsy", 3)))
        elif self.chance(0.008):
            p(day, "paper_source")

    def schedules(self) -> None:
        """Monthly bills and subscriptions, periodic top-ups and yearly charges."""
        price = self._price
        for back in range(0, 26):
            for key, dom, amount in (
                ("equinox", 1, price(back, 3, "245.00", "255.00")),
                ("spotify", 3, price(back, 9, "11.99", "12.99")),
                ("comcast", 5, price(back, 12, "70.00", "75.00")),
                ("chewy", 6, price(back, 6, "52.48", "54.98")),
                ("openai", 8, Decimal("20.00")),
                ("kqed", 10, Decimal("10.00")),
                ("netflix", 12, price(back, 5, "17.99", "19.99")),
                ("pge", 14, None),
                ("prime", 16, Decimal("14.99")),
                ("verizon", 18, None),
                ("icloud", 21, Decimal("2.99")),
                ("nytimes", 22, Decimal("17.00")),
            ):
                day = self.monthly(back, dom)
                if key == "pge":
                    amount = self._pge_bill(day)
                lag = 1 if key != "equinox" else 0
                self.purchase(day, key, amount, scheduled=True, billed=True, lag=lag)
        for key, every in (
            ("barber", (34, 38)),
            ("costco", (24, 36)),
            ("clipper", (18, 24)),
            ("chevron", (20, 28)),
            ("fastrak", (40, 50)),
        ):
            day = self.start + dt.timedelta(days=self.rng.randint(0, every[0]))
            while day <= self.today:
                if day not in self.away:
                    row = self.purchase(day, key, scheduled=key == "barber")
                    if row is not None and key == "clipper":
                        row.amount = Decimal(self.rng.choice(("40.00", "50.00")))
                day += dt.timedelta(days=self.rng.randint(*every))
        yearly = (
            ("lemonade", (3, 15), 9, ("186.00", "174.00")),
            ("dmv", (6, 18), 17, ("212.00", "198.00")),
            ("one_medical", (10, 22), 2, ("199.00", "199.00")),
            ("masterclass", (13,), 20, ("120.00",)),
            ("dentist", (2, 8, 14, 20), 11, None),
            ("banfield", (4, 10, 16, 22), 23, None),
            ("ikea", (11, 19), 13, None),
            ("apple_store", (7, 21), 15, ("1299.00", "249.00")),
            ("coursera", (14, 15, 16), 7, ("49.00", "49.00", "49.00")),
        )
        for key, backs, dom, amounts in yearly:
            online = cat.MERCHANTS[key].channel == "online"  # renewals; the rest are visits
            for i, back in enumerate(backs):
                amount = Decimal(amounts[i]) if amounts else None
                self.purchase(self.monthly(back, dom), key, amount, scheduled=True, billed=online)
        for back in range(0, 26):
            day = self.monthly(back, 28)
            if day.month == 12:
                self.purchase(day, "wikimedia", scheduled=True, billed=True)
        # Black Friday: the day after the fourth Thursday of November.
        for year in range(self.start.year, self.today.year + 1):
            thursdays = [d for d in range(22, 29) if dt.date(year, 11, d).weekday() == 3]
            day = dt.date(year, 11, thursdays[0] + 1)
            if day not in self.away:  # ordinary purchases: this month's shaping may drop them
                self.purchase(day, "best_buy", self.amount(18000, 42000))
                self.purchase(day, self.pick(("uniqlo", 1), ("target", 1)))
        for back in (8, 20):
            day = self.monthly(back, 5)
            if self.start <= day <= self.today:
                self.special(
                    "annual_fee",
                    day,
                    cat.ANNUAL_FEE,
                    "ANNUAL MEMBERSHIP FEE",
                    "ANNUAL MEMBERSHIP FEE",
                    "Fees & Interest",
                    ANNUAL_FEE_PFC,
                    "membership fee",
                    cat.FEES_ADJ,
                )

    def _price(self, back: int, changed_back: int, old: str, new: str) -> Decimal:
        return Decimal(new if back < changed_back else old)

    def _pge_bill(self, day: dt.date) -> Decimal:
        """Gas and electricity: about a fifth higher in January, a fifth lower in July."""
        season = 1 + 0.2 * math.cos(2 * math.pi * (day.month - 1) / 12)
        return _q(self.amount(5200, 6300) * Decimal(str(round(season, 4))))

    def trip_rows(self) -> None:
        t = self.trips
        p = self.purchase

        def each(trip: str) -> Iterable[dt.date]:
            day, last = t[trip]
            while day <= last:
                yield day
                day += DAY

        # Seattle (CSV era): flight booked five weeks ahead, three hotel nights.
        first, last = t["seattle"]
        p(first - dt.timedelta(days=35), "alaska", trip="seattle")
        p(last, "andra", trip="seattle")
        self._tour(
            "seattle",
            (0.85, ("roastery", "storyville", "elm_coffee")),
            (0.8, ("pike_chowder", "piroshky")),
            (0.8, ("pink_door", "dtf_seattle")),
        )
        p(first + DAY, "seattle_art", trip="seattle")
        p(first + DAY * self.rng.randint(0, 3), "elliott_bay", trip="seattle")
        for day in each("seattle"):
            if self.chance(0.6):
                p(day, "uber", trip="seattle")
        # Los Angeles: a five-night work trip; the hotel and the car are paid at check-out.
        first, last = t["la"]
        p(first - dt.timedelta(days=25), "united", trip="la")
        p(last, "ace_hotel", trip="la", lag=1)
        p(last, "hertz", trip="la", lag=1)
        self._tour(
            "la",
            (0.8, ("starbucks_la", "intelligentsia_la", "verve_la")),
            (0.75, ("grand_central", "philippe", "sqirl")),
            (0.8, ("guisados", "howlin_rays", "bestia")),
        )
        # Lake Tahoe: a ski weekend.
        first, last = t["tahoe"]
        p(first - dt.timedelta(days=21), "airbnb", self.amount(58000, 69000), trip="tahoe")
        p(first, "shell_truckee", trip="tahoe")
        p(first, "raleys", trip="tahoe")
        p(first + DAY, "heavenly", trip="tahoe")
        p(first + DAY, "loft_tahoe", trip="tahoe")
        p(last, "shell_truckee", trip="tahoe")
        # New York: a conference (the hotel bill is split and later corrected by Plaid).
        first, last = t["nyc"]
        p(first - dt.timedelta(days=30), "united", Decimal("468.20"), trip="nyc")
        hotel = p(last, "marriott", trip="nyc", lag=1)
        assert hotel is not None
        hotel.split_amount = hotel.amount
        hotel.amount = Decimal("628.15")
        p(first + DAY, "carbone", trip="nyc")
        p(first + DAY * 2, "the_met", trip="nyc")
        self._tour(  # conference lunches are sometimes provided; Carbone is the client dinner
            "nyc",
            (0.7, ("levain", "russ_daughters", "joe_coffee")),
            (0.7, ("katz", "joes_pizza", "shake_shack", "los_tacos")),
            (0.35, ("via_carota",)),
        )
        for day in each("nyc"):
            p(day, "mta", trip="nyc", lag=1)
            if self.chance(0.7):
                p(day, "uber", trip="nyc")
        # Lisbon: flights and the flat booked weeks ahead, then everything abroad.
        first, last = t["lisbon"]
        p(first - dt.timedelta(days=58), "tap", trip="lisbon")
        p(first - dt.timedelta(days=44), "airbnb", self.amount(98000, 112000), trip="lisbon")
        self._tour(  # breakfast in the flat some days (Pingo Doce groceries)
            "lisbon",
            (0.7, ("pasteis", "manteigaria", "brasileira")),
            (0.85, ("time_out", "ze_mouraria", "taberna_flores", "manteigaria")),
            (0.9, ("cevicheria", "ramiro", "sao_bento", "taberna_flores", "time_out")),
        )
        for day in each("lisbon"):
            if self.chance(0.5):
                p(day, "uber_lisbon", trip="lisbon")
            if self.chance(0.4):
                p(day, "metro_lisboa", trip="lisbon")
            if self.chance(0.35):
                p(day, "pingo_doce", trip="lisbon")
        p(first + DAY * 2, "cp", trip="lisbon")
        p(first + DAY * 3, "gulbenkian", trip="lisbon")
        p(first + DAY * 5, "oceanario", trip="lisbon")
        p(last - DAY * 2, "cp", trip="lisbon")
        # Recent pop-up stalls Plaid has no category for (Uncategorized until the user picks one).
        for back in (9, 38, 67):
            p(self.today - dt.timedelta(days=back), "popup")
        # A flight home for the holidays, booked four days ago.
        p(self.today - dt.timedelta(days=4), "delta", self.amount(54000, 64000))
        # A cancelled trip, refunded (see refunds()).
        p(self.today - dt.timedelta(days=402), "united", Decimal("389.20"))

    def _tour(self, trip: str, *slots: tuple[float, tuple[str, ...]]) -> None:
        """Meals on a trip: each day, each slot (chance, merchants) may add one visit to a
        merchant not yet visited twice on this trip, nor earlier that day."""
        visits: Counter[str] = Counter()
        day, last = self.trips[trip]
        while day <= last:
            that_day: set[str] = set()
            for chance, pool in slots:
                options = [k for k in pool if visits[k] < 2 and k not in that_day]
                if options and self.chance(chance):
                    key = self.rng.choice(options)
                    visits[key] += 1
                    that_day.add(key)
                    self.purchase(day, key, trip=trip)
            day += DAY

    def recent_pending(self) -> None:
        """A few charges from the last three days that Amex has not posted yet."""
        today = self.today
        for key, lag in (
            ("blue_bottle", 1),
            ("tartine" if today.weekday() >= 5 else "sweetgreen", 2),
        ):
            if self.purchase(today, key, lag=lag) is None:  # not synced yet: yesterday's instead
                self.purchase(today - DAY, key, lag=lag + 1)
        self.purchase(today - DAY, "amazon", lag=2)
        self.purchase(today - DAY * 2, "uber", lag=3)

    def refunds(self) -> None:
        """Returns: full or partial refunds a week or three after the purchase."""
        cutoff = self.month - dt.timedelta(days=35)

        def candidates(key: str, *, plaid: bool | None = None) -> list[Row]:
            rows = [
                r
                for r in self.rows
                if r.merchant is not None
                and r.merchant.key == key
                and r.kind == "purchase"
                and r.auth < cutoff
                and r.trip is None
            ]
            if plaid is True:
                rows = [r for r in rows if r.auth > self.cutover + dt.timedelta(days=30)]
            elif plaid is False:
                rows = [r for r in rows if r.auth < self.cutover - dt.timedelta(days=30)]
            return rows

        plan: list[tuple[str, bool | None, str]] = [
            ("amazon", False, "full"),
            ("amazon", True, "full"),
            ("amazon", True, "full"),
            ("amazon", True, "partial"),
            ("uniqlo", True, "partial"),
            ("rei", True, "odd"),
            ("doordash", True, "partial"),
            ("united", True, "flight"),
        ]
        used: set[int] = set()
        for key, plaid, how in plan:
            pool = [r for r in candidates(key, plaid=plaid) if r.seq not in used]
            if how == "flight":
                pool = [r for r in pool if r.amount == Decimal("389.20")]
            if not pool:
                continue
            original = self.rng.choice(pool)
            used.add(original.seq)
            if how == "partial":
                amount = _q(original.amount * Decimal(self.rng.choice(("0.25", "0.4", "0.5"))))
            else:
                amount = original.amount
            day = original.auth + dt.timedelta(days=self.rng.randint(8, 21))
            m = original.merchant
            assert m is not None
            if day in self.away and cat.is_home_store(m):  # returned in person, once home
                day = self._after_trip(day)
            if day >= self.month:
                day = self.month - DAY * 2
            self.add(
                Row(
                    seq=next(self._seq),
                    kind="refund",
                    auth=day,
                    amount=-amount,
                    name=original.name,
                    raw=original.raw,
                    category=m.category,
                    merchant=m,
                    merchant_name=m.merchant_name,
                    pfc=ODD_REFUND_PFC if how == "odd" else m.pfc,
                    confidence="LOW" if how == "odd" else m.confidence,
                    code="refund",
                    amex=m.amex,
                    channel=m.channel,
                    lag=self.rng.randint(1, 3),
                    arrival_hour=self.rng.randint(8, 21),
                )
            )

    def credits(self) -> None:
        """Amex Offer statement credits and Reward Dollars redeemed as statement credits."""
        offers = {
            "sweetgreen": (5, "SWEETGREEN"),
            "uber_eats": (10, "UBER EATS"),
            "best_buy": (25, "BEST BUY"),
            "uniqlo": (15, "UNIQLO"),
            "doordash": (8, "DOORDASH"),
        }
        qualifying = [  # not this month: shape_current_month() may still drop those purchases
            r
            for r in self.rows
            if r.kind == "purchase"
            and r.merchant is not None
            and r.merchant.key in offers
            and r.auth < self.month
        ]
        qualifying.sort(key=lambda r: (r.auth, r.seq))
        for row in self.rng.sample(qualifying, min(11, len(qualifying))):
            assert row.merchant is not None
            day = row.auth + dt.timedelta(days=self.rng.randint(6, 20))
            if day > self.today:
                continue
            value, label = offers[row.merchant.key]
            amount = Decimal(value).quantize(CENT)
            self.special(
                "credit",
                day,
                -amount,
                f"AMEX OFFER CREDIT {label}",
                f"{label} AMEX OFFER CREDIT",
                "Credits & Rewards",
                CREDIT_PFC,
                "adjustment",
                cat.FEES_ADJ,
            )
        day = self.start + dt.timedelta(days=self.rng.randint(40, 70))
        while day <= self.today:
            amount = _cents(self.rng.randrange(5000, 15000, 500))
            self.special(
                "credit",
                day,
                -amount,
                "REWARD DOLLARS STATEMENT CREDIT",
                "REWARD DOLLARS STATEMENT CREDIT",
                "Credits & Rewards",
                CREDIT_PFC,
                "adjustment",
                cat.FEES_ADJ,
            )
            day += dt.timedelta(days=self.rng.randint(100, 135))

    def shape_current_month(self) -> None:
        """This month: Health & Fitness at about 85 %, Entertainment over, the rest under 75 %.

        Works on any day of the month: the gym bills on the 1st and is Health & Fitness's only
        charge so far; concert tickets bought in the first days push Entertainment over budget.
        Other budgeted categories lose random older purchases until they are under 75 %.
        """
        budgets = cat.MONTHLY_BUDGETS
        by_category: dict[str, list[Row]] = defaultdict(list)
        for row in sorted(self.rows, key=lambda r: (r.auth, r.seq)):
            if row.kind == "purchase" and row.auth >= self.month:
                by_category[row.category].append(row)
        recent = self.today - DAY * 2
        tickets = Decimal(self.rng.choice(("178.60", "186.40", "193.80")))
        for category, rows in by_category.items():
            if category not in budgets:
                continue
            removable = [r for r in rows if not r.scheduled]
            if category == cat.NEARLY_FULL:
                cap = ZERO
            elif category == cat.OVERSPENT:
                cap = budgets[category] * Decimal("1.24") - tickets
            else:
                cap = budgets[category] * Decimal("0.75")
            total = sum((r.amount for r in rows), ZERO)
            self.rng.shuffle(removable)
            removable.sort(key=lambda r: r.auth >= recent)  # keep the last days' activity
            for row in removable:
                if total <= cap:
                    break
                self.rows.remove(row)
                total -= row.amount
        day = min(self.today, self.month + DAY * self.rng.randint(0, 3))
        row = self.purchase(day, "ticketmaster", tickets, scheduled=True, lag=1)  # planned
        if row is not None:
            row.notes = "Concert tickets for two (Greek Theatre)"
            row.tags = ["concerts"]
            self._edited(row)

    def fx_fees(self) -> None:
        """Blue Cash Preferred charges 2.7 % on foreign purchases, as a separate fee row."""
        for row in [r for r in self.rows if r.foreign and r.kind == "purchase"]:
            fee = self.special(
                "fx_fee",
                row.auth,
                _q(row.amount * cat.FX_FEE_RATE),
                "FOREIGN TRANSACTION FEE",
                "FOREIGN TRANSACTION FEE",
                "Fees & Interest",
                FX_FEE_PFC,
                "bank charge",
                cat.FEES_ADJ,
            )
            fee.lag = row.lag
            fee.trip = row.trip
            fee.arrival_hour = row.arrival_hour

    def rounding_cent(self) -> Row:
        """One Lisbon meal that Amex's statement shows a cent higher than Plaid does."""
        pool = sorted(
            (
                r
                for r in self.rows
                if r.trip == "lisbon"
                and r.kind == "purchase"
                and r.merchant
                and r.merchant.category == "Dining & Drinks"
            ),
            key=lambda r: (r.auth, r.seq),
        )
        row = pool[len(pool) // 2]
        row.statement_extra = CENT
        return row

    def post(self) -> None:
        for row in self.rows:
            posted = row.auth + dt.timedelta(days=row.lag)
            row.posted = posted if posted <= self.today else None

    # ------------------------------------------------------------------ payments & statements

    def pay_and_close(self) -> dict[dt.date, Decimal]:
        """Run the card cycle by cycle: AUTOPAY on each due date, interest after a partial pay.

        Returns the (true, Amex) new balance at every closing date up to today.
        """
        closings = self.closings
        nyc_end, lisbon_end = self.trips["nyc"][1], self.trips["lisbon"][1]
        partial_at = next(c for c in closings if c >= nyc_end + DAY * 2)
        extra_at = next(c for c in closings if c >= lisbon_end + DAY * 2)
        balance: dict[dt.date, Decimal] = {closings[0]: _cents(self.rng.randint(160000, 240000))}
        carried: Decimal | None = None
        for previous, closing in itertools.pairwise(closings):
            if previous > self.today:
                break
            owed = balance[previous]
            due = previous + dt.timedelta(days=PAYMENT_DUE_DAYS)
            if owed > 0 and due <= self.today:
                autopay = owed
                if previous == partial_at:
                    autopay = max(
                        Decimal("500.00"), (owed / 2).quantize(Decimal("100"), ROUND_DOWN)
                    )
                    carried = owed - autopay
                elif previous == extra_at and owed > Decimal("1500.00"):
                    mobile = previous + dt.timedelta(days=8)
                    self._payment(mobile, Decimal("1000.00"), mobile=True)
                    autopay = owed - Decimal("1000.00")
                self._payment(due, autopay)
            if carried is not None and closing <= self.today:
                interest = _q(carried * cat.APR * 30 / 365)
                row = self.special(
                    "interest",
                    closing,
                    interest,
                    "INTEREST CHARGE ON PURCHASES",
                    "INTEREST CHARGE ON PURCHASES",
                    "Fees & Interest",
                    INTEREST_PFC,
                    "interest",
                    cat.FEES_ADJ,
                )
                row.posted = closing
                carried = None
            if closing > self.today:
                break
            posted = sorted(
                (r for r in self.rows if r.posted is not None), key=lambda r: (r.posted, r.seq)
            )
            activity = sum((r.true_amount for r in posted if previous < r.posted <= closing), ZERO)
            balance[closing] = balance[previous] + activity
        return balance

    def _payment(self, day: dt.date, amount: Decimal, *, mobile: bool = False) -> None:
        if day > self.today:
            return
        label = "MOBILE PAYMENT" if mobile else "AUTOPAY PAYMENT"
        row = self.special(
            "payment",
            day,
            -amount,
            f"{label} - THANK YOU",
            f"{label} RECEIVED - THANK YOU",
            "Card Payments",
            PAYMENT_PFC,
            "payment",
            None,
        )
        row.posted = day

    def statements(self, balance: dict[dt.date, Decimal], off: Row) -> list[StatementPlan]:
        closed = [c for c in self.closings if c <= self.today - DAY and c in balance]
        stored = closed[-STATEMENT_COUNT:]
        posted = [r for r in self.rows if r.posted is not None]
        out = []
        for closing in stored:
            previous = self.closings[self.closings.index(closing) - 1]
            sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for row in posted:
                if previous < row.posted <= closing:  # type: ignore[operator]
                    sums[row.bucket] += row.true_amount
            payments = -sums["payments_credits"]
            new = balance[previous] - payments + sums["charges"] + sums["fees"] + sums["interest"]
            assert new == balance[closing], (closing, new, balance[closing])
            final = closing + dt.timedelta(days=SETTLE_DAYS) < self.today
            mismatch = off.posted is not None and previous < off.posted <= closing
            checked = local_time(closing + dt.timedelta(days=6), 19, 30)
            notes = None
            if sums["interest"]:
                notes = "Paid only part after the New York trip, so interest was charged."
            out.append(
                StatementPlan(
                    closing_date=closing,
                    previous_balance=balance[previous],
                    payments_credits=payments,
                    new_charges=sums["charges"],
                    fees=sums["fees"],
                    interest=sums["interest"],
                    new_balance=balance[closing],
                    checked_at=checked if final and not mismatch and checked < self.now else None,
                    notes=notes,
                    period_start_override=previous + DAY if closing == stored[0] else None,
                )
            )
        return out

    # ------------------------------------------------------------------ how rows reach the app

    def represent(self) -> tuple[dt.date, dict[str, Any]]:
        """Plaid rows, CSV-only rows, paired rows and the two suspect duplicates."""
        for row in self.rows:
            row.source = "plaid" if row.posted is None or row.posted >= self.cutover else "csv"
        history_start = min(r.posted for r in self.rows if r.source == "plaid" and r.posted)
        csv_end = history_start + dt.timedelta(days=CSV_OVERLAP_DAYS)
        file_rows: list[Row] = []
        for row in sorted(self.rows, key=lambda r: (r.auth, r.seq)):
            row.csv_date = row.posted if row.kind == "payment" else row.auth
            if row.posted is not None and row.csv_date <= csv_end:
                file_rows.append(row)
        # The LA hotel and car rental: the CSV dates them at check-in/pick-up (5 days before
        # Plaid's authorized date), outside the 3-day pairing window: unpaired → suspect.
        suspects = []
        for row in file_rows:
            key = row.merchant.key if row.merchant is not None else None
            if row.source == "plaid" and row.trip == "la" and key in ("ace_hotel", "hertz"):
                twin = Row(
                    seq=next(self._seq),
                    kind="purchase",
                    auth=self.trips["la"][0],
                    amount=row.amount,
                    name=row.name,
                    raw=row.raw,
                    category=row.category,
                    merchant=row.merchant,
                    amex=row.amex,
                    lag=0,
                    posted=row.posted,
                    source="csv",
                    suspect=True,
                    trip="la",
                )
                twin.csv_date = twin.auth
                assert twin.csv_date >= history_start
                suspects.append(twin)
            elif row.source == "plaid":
                row.paired = True
        file_rows = [r for r in file_rows if r.source == "csv" or r.paired] + suspects
        self.rows.extend(suspects)
        references: set[str] = set()
        for row in sorted(file_rows, key=lambda r: (r.csv_date, r.seq)):
            assert row.posted is not None
            while True:
                ref = f"3{row.posted.year:04d}{row.posted.timetuple().tm_yday:03d}"
                ref += "".join(self.rng.choice(string.digits) for _ in range(10))
                if ref not in references:
                    break
            references.add(ref)
            row.csv_reference = ref
            if row.source == "csv":
                member = cat.CARD_MEMBER
                if row.category in ("Groceries", "Dining & Drinks") and self.chance(0.15):
                    member = cat.ADDITIONAL_CARD_MEMBER
                row.account_owner = member
        dates = [r.csv_date for r in file_rows if r.csv_date is not None]
        csv_only = sum(1 for r in file_rows if r.source == "csv")
        paired = sum(1 for r in file_rows if r.paired)
        digest = hashlib.sha256(f"demo-activity-{self.seed}-{self.today}".encode()).hexdigest()
        csv_import = {
            "filename": "activity.csv",
            "file_sha256": digest,
            "imported_at": local_time(self.link_day + DAY * 2, 21, 14),
            "row_count": csv_only + paired,
            "inserted_count": csv_only,
            "duplicate_count": 0,
            "matched_plaid_count": paired,
            "error_count": 0,
            "date_min": min(dates),
            "date_max": max(dates),
        }
        return history_start, csv_import

    def ids(self) -> None:
        for row in sorted(self.rows, key=lambda r: (r.auth, r.seq)):
            if row.source != "plaid":
                continue
            row.plaid_id = self.plaid_id()
            synced_live = row.auth > self.link_day
            if (
                row.kind == "purchase"
                and row.posted is not None
                and synced_live
                and self.chance(0.8)
            ):
                row.pending_id = self.plaid_id()

    # ------------------------------------------------------------------ user edits

    def _find(self, predicate: Callable[[Row], bool], *, last: bool = False) -> Row | None:
        pool = sorted((r for r in self.rows if predicate(r)), key=lambda r: (r.auth, r.seq))
        if not pool:
            return None
        return pool[-1] if last else self.rng.choice(pool)

    def _edited(self, row: Row) -> None:
        stamp = local_time(row.auth + dt.timedelta(days=self.rng.randint(1, 4)), 21, 5)
        row.edited_at = min(stamp, self.now - dt.timedelta(minutes=30))

    def user_edits(self) -> None:
        before = self.month - dt.timedelta(days=10)

        def plain(key: str, *, csv: bool = False) -> Callable[[Row], bool]:
            def test(r: Row) -> bool:
                return (
                    r.kind == "purchase"
                    and r.merchant is not None
                    and r.merchant.key == key
                    and r.trip is None
                    and r.posted is not None
                    and r.auth < before
                    and (r.source == "csv") == csv
                    and not r.suspect
                    and r.user_category is None
                    and not r.splits
                )

            return test

        def recategorize(
            row: Row | None, category: str, notes: str | None = None, tags: Iterable[str] = ()
        ) -> None:
            if row is None:
                return
            row.user_category = category
            row.notes = notes
            row.tags = list(tags)
            self._edited(row)

        recategorize(
            self._find(plain("target")), "Gifts & Donations", "Baby shower gift for Priya", ["gift"]
        )
        recategorize(self._find(plain("walgreens")), "Personal Care")
        recategorize(
            self._find(plain("popup")), "Dining & Drinks", "Food truck at the night market"
        )
        recategorize(self._find(plain("barber", csv=True)), "Personal Care")
        recategorize(self._find(plain("venmo")), "Entertainment", "Concert tickets from Jordan")
        etsy = self._find(lambda r: plain("etsy")(r) and r.auth.month == 12)
        recategorize(etsy, "Gifts & Donations", None, ["gift", "holidays"])
        for key, note, tags in (
            ("nopa", "Mom's birthday dinner", ["family"]),
            ("home_depot", "Paint and rollers for the bedroom", ["apartment"]),
            ("ikea", None, ["apartment"]),
            ("rei", "Tent for the Yosemite trip", []),
        ):
            row = self._find(plain(key))
            if row is not None:
                row.notes, row.tags = note, list(tags)
                self._edited(row)
        # Trips the user tagged (and the client dinner in New York).
        for trip, tag in (("nyc", "NYC conference"), ("tahoe", "Tahoe"), ("lisbon", None)):
            first = self.trips[trip][0]
            label = tag or f"Lisbon {first.year}"
            for row in self.rows:
                if row.trip == trip and row.source == "plaid":
                    row.tags = [*row.tags, label]
                    self._edited(row)
        carbone = self._find(lambda r: r.merchant is not None and r.merchant.key == "carbone")
        if carbone is not None:
            carbone.notes = "Client dinner — submitted for reimbursement"
            carbone.tags = [*carbone.tags, "reimbursable"]
        # Splits: a Costco run and a Target run; the New York hotel (amount changed later).
        costco = self._find(
            lambda r: (
                plain("costco")(r)
                and month_start(self.today, 4) <= r.auth < month_start(self.today, 1)
            )
        )
        if costco is not None:
            food = _q(costco.amount * Decimal("0.7"))
            costco.splits = [
                ("Groceries", food, "Food"),
                ("Home", costco.amount - food, "Paper towels and storage bins"),
            ]
            costco.notes = "Split: food vs household"
            self._edited(costco)
        target = None
        # A big Target run 5–9 months ago; with none, a smaller one over a wider stretch.
        for older, newer, smallest in ((9, 5, "60.00"), (14, 2, "45.00"), (17, 1, "30.00")):
            target = self._find(
                lambda r, older=older, newer=newer, smallest=smallest: (
                    plain("target")(r)
                    and r.amount >= Decimal(smallest)
                    and month_start(self.today, older) <= r.auth < month_start(self.today, newer)
                )
            )
            if target is not None:
                break
        if target is not None:
            groceries = _q(target.amount * Decimal("0.35"))
            home = _q(target.amount * Decimal("0.40"))
            target.splits = [
                ("Groceries", groceries, None),
                ("Home", home, "Shelf and hooks"),
                ("Personal Care", target.amount - groceries - home, None),
            ]
            self._edited(target)
        hotel = self._find(lambda r: r.merchant is not None and r.merchant.key == "marriott")
        if hotel is not None and hotel.split_amount is not None:
            hotel.splits = [
                ("Travel", Decimal("520.00"), None),
                ("Dining & Drinks", hotel.split_amount - Decimal("520.00"), "Room service"),
            ]
            hotel.notes = "Conference hotel — room service on the last night was personal"
            hotel.tags = [*hotel.tags, "reimbursable"]
            self._edited(hotel)

    # ------------------------------------------------------------------ budgets

    def budgets(self) -> list[BudgetPlan]:
        link_month = month_start(self.link_day)
        jan1 = dt.date(self.today.year, 1, 1)
        m = lambda back: month_start(self.today, back)  # noqa: E731
        out: list[BudgetPlan] = [
            BudgetPlan(None, "monthly", Decimal("3600.00"), link_month),
            BudgetPlan(None, "monthly", cat.OVERALL_TARGET, m(4)),
        ]
        history = {
            "Groceries": [(link_month, "650.00", False), (m(6), None, False)],
            "Dining & Drinks": [(link_month, "700.00", False), (m(3), None, False)],
            "Subscriptions & Streaming": [(link_month, "100.00", False), (m(8), None, False)],
            "Personal Care": [(link_month, None, False), (m(5), None, True)],
            "Pets": [(m(9), None, False)],
        }
        for category, amount in cat.MONTHLY_BUDGETS.items():
            for start, value, rollover in history.get(category, [(link_month, None, False)]):
                out.append(
                    BudgetPlan(
                        category,
                        "monthly",
                        Decimal(value) if value else amount,
                        start,
                        rollover,
                    )
                )
        for category, amount in cat.ANNUAL_BUDGETS.items():
            if category == "Travel" and link_month < jan1:
                out.append(BudgetPlan(category, "monthly", Decimal("300.00"), link_month))
            # Setting an annual budget ends any monthly one from the same date (ARCHITECTURE §5).
            out.append(BudgetPlan(category, "monthly", None, jan1))
            out.append(BudgetPlan(category, "annual", amount, jan1))
        return out

    # ------------------------------------------------------------------ sync history & alerts

    def sync_slots(self) -> list[tuple[dt.datetime, str]]:
        """When syncs ran: every 6 hours for the last days, one manual run; none after ``now``."""
        slots: list[tuple[dt.datetime, str]] = []
        for back in range(SYNC_DAYS, -1, -1):
            day = self.today - dt.timedelta(days=back)
            for hour, minute in SYNC_SLOTS:
                slots.append((local_time(day, hour, minute), "schedule"))
        slots.append((local_time(self.today - DAY, 15, 42), "manual"))
        return sorted(s for s in slots if s[0] <= self.now)

    def sync_runs(self) -> list[SyncRunPlan]:
        slots = self.sync_slots()
        failed_at = local_time(self.today - DAY * 2, 6, 15)
        last_run = slots[-1][0]
        events: list[tuple[dt.datetime, bool]] = []  # (arrival, removes a pending row)
        for row in self.rows:
            if row.source != "plaid":
                continue
            if row.pending or row.pending_id:
                # Everyday charges arrived by the last run (purchase() leaves out later ones);
                # only a planned charge made today after that run is counted with it.
                seen = local_time(row.auth, row.arrival_hour)
                events.append((min(seen, last_run - dt.timedelta(minutes=2)), False))
            if row.posted is not None:
                events.append((local_time(row.posted, 4), row.pending_id is not None))
        runs = []
        previous = slots[0][0] - dt.timedelta(hours=6)
        for started, trigger in slots:
            started = started + dt.timedelta(seconds=self.rng.randint(2, 40))
            finished = started + dt.timedelta(seconds=self.rng.randint(3, 11))
            if started.replace(second=0) == failed_at.replace(second=0) and trigger == "schedule":
                code, message = SYNC_FAILURE
                runs.append(
                    SyncRunPlan(trigger, "error", started, finished, 0, 0, 0, code, message)
                )
                continue
            window = [e for e in events if previous < e[0] <= started]
            added = len(window)
            removed = sum(1 for _, removes in window if removes)
            modified = 1 if self.chance(0.12) else 0
            runs.append(
                SyncRunPlan(trigger, "success", started, finished, added, modified, removed)
            )
            previous = started
        return runs

    def alerts(self, runs: list[SyncRunPlan], item_key: str) -> list[AlertPlan]:
        """The consent reminder and the failed sync (budget alerts are added by the writer)."""
        due = self.linked_at + dt.timedelta(days=365)
        reminder_day = due.astimezone().date() - dt.timedelta(days=30)
        failure = next(r for r in runs if r.status == "error")
        first_ok_that_day = local_time(reminder_day, 0, 15) + dt.timedelta(seconds=20)
        return [
            AlertPlan(
                "consent",
                f"consent:{item_key}:{due.astimezone().date().isoformat()}",
                "Renew your American Express connection",
                f"Plaid's access to American Express ends on {_long_date(due)}. "
                "Renew it on the Connection page so syncing keeps working.",
                first_ok_that_day,
            ),
            AlertPlan(
                "sync_error",
                f"sync-error:{item_key}:{failure.error_code}:"
                f"{failure.started_at.astimezone().date().isoformat()}",
                "American Express sync failed",
                "American Express isn't responding right now (INSTITUTION_NOT_RESPONDING). The "
                "next scheduled sync will try again.",
                failure.finished_at + dt.timedelta(seconds=1),
            ),
        ]

    # ------------------------------------------------------------------ the Item and the card

    def item_and_account(
        self,
        runs: list[SyncRunPlan],
        statements: list[StatementPlan],
        balance: dict[dt.date, Decimal],
        history_start: dt.date,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        last_ok = [r for r in runs if r.status == "success"][-1]
        failure = next(r for r in runs if r.status == "error")
        item_id = self.plaid_id()
        consent_expires = self.linked_at + dt.timedelta(days=365)
        plaid_update = last_ok.started_at - dt.timedelta(hours=1, minutes=37)
        plaid_failed = failure.started_at - dt.timedelta(minutes=24)
        z = lambda t: t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        item = {
            "plaid_item_id": item_id,
            "institution_id": cat.INSTITUTION_ID,
            "institution_name": cat.INSTITUTION_NAME,
            "status": "healthy",
            "transactions_cursor": self._cursor(),
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "history_complete_at": self.linked_at + dt.timedelta(minutes=3, seconds=12),
            "days_requested": 730,
            "consented_products": ["liabilities", "transactions"],
            "liabilities_status": "ok",
            "linked_at": self.linked_at,
            "last_consented_at": self.linked_at,
            "consent_expires_at": consent_expires,
            "plaid_last_update_at": plaid_update,
            "plaid_last_failed_update_at": plaid_failed,
            "last_sync_attempt_at": runs[-1].started_at,
            "last_sync_success_at": last_ok.finished_at,
            "item_snapshot": {
                "item": {
                    "item_id": item_id,
                    "institution_id": cat.INSTITUTION_ID,
                    "institution_name": cat.INSTITUTION_NAME,
                    "available_products": ["balance", "investments", "signal"],
                    "billed_products": ["liabilities", "transactions"],
                    "products": ["transactions"],
                    "consented_products": ["liabilities", "transactions"],
                    "consent_expiration_time": z(consent_expires),
                    "update_type": "background",
                    "webhook": "",
                    "error": None,
                    "auth_method": None,
                },
                "status": {
                    "transactions": {
                        "last_successful_update": z(plaid_update),
                        "last_failed_update": z(plaid_failed),
                    },
                    "last_webhook": None,
                },
                "request_id": self.code(15),
            },
        }
        latest = statements[-1]
        last_closing = latest.closing_date
        current = balance[last_closing] + sum(
            (
                r.true_amount
                for r in self.rows
                if r.posted is not None and not r.suspect and r.posted > last_closing
            ),
            ZERO,
        )
        pending = sum((r.amount for r in self.rows if r.pending and r.amount > 0), ZERO)
        payments = sorted(
            (r for r in self.rows if r.kind == "payment" and r.posted), key=lambda r: r.posted
        )
        minimum = min(
            latest.new_balance,
            max(Decimal("40.00"), _q(latest.new_balance * Decimal("0.01")) + latest.interest),
        )
        account = {
            "plaid_account_id": self.plaid_id(),
            "name": cat.CARD_NAME,
            "official_name": cat.CARD_OFFICIAL_NAME,
            "mask": cat.CARD_MASK,
            "type": "credit",
            "subtype": "credit card",
            "iso_currency_code": "USD",
            "current_balance": current,
            "available_balance": cat.CREDIT_LIMIT - current - pending,
            "credit_limit": cat.CREDIT_LIMIT,
            "balances_as_of": last_ok.finished_at,
            "statement_balance": latest.new_balance,
            "minimum_payment": max(minimum, ZERO),
            "last_payment_amount": -payments[-1].amount if payments else None,
            "statement_issue_date": last_closing,
            "payment_due_date": last_closing + dt.timedelta(days=PAYMENT_DUE_DAYS),
            "last_payment_date": payments[-1].posted if payments else None,
            "is_overdue": False,
            "liabilities_as_of": last_ok.finished_at,
            "statement_closing_day": cat.CLOSING_DAY,
            "plaid_history_start": history_start,
        }
        return item, account

    def _cursor(self) -> str:
        raw = "".join(self.rng.choice(ID_ALPHABET) for _ in range(96))
        return f"CAESJ{raw}"


def build_plan(seed: int, today: dt.date, now: dt.datetime) -> Plan:
    """Plan the whole demo database for ``(seed, today)``; ``now`` bounds the sync history."""
    planner = _Planner(seed, today, now)
    for day in planner.days():
        planner.daily(day)
    planner.schedules()
    planner.trip_rows()
    planner.recent_pending()
    planner.refunds()
    planner.credits()
    planner.shape_current_month()
    planner.fx_fees()
    off = planner.rounding_cent()
    planner.post()
    balance = planner.pay_and_close()
    statements = planner.statements(balance, off)
    history_start, csv_import = planner.represent()
    planner.ids()
    planner.user_edits()
    runs = planner.sync_runs()
    item, account = planner.item_and_account(runs, statements, balance, history_start)
    alerts = planner.alerts(runs, "{item_id}")
    rows = sorted(planner.rows, key=lambda r: (r.auth, r.seq))
    return Plan(
        seed=seed,
        today=today,
        now=now,
        rows=rows,
        closings=[c for c in planner.closings if c <= today],
        statements=statements,
        budgets=planner.budgets(),
        sync_runs=runs,
        alerts=alerts,
        item=item,
        account=account,
        csv_import=csv_import,
        settings={
            "budget_period_mode": "calendar",
            "include_pending_in_budgets": True,
            "budget_alerts_enabled": True,
            "alert_thresholds": [80, 100],
            "sync_alerts_enabled": True,
            "large_charge_threshold": None,
            "consent_reminder_days": 30,
            "statement_settle_days": SETTLE_DAYS,
        },
        recurring_ignored=["clipper"],
        plaid_history_start=history_start,
        linked_at=planner.linked_at,
        trips=dict(planner.trips),
    )
