"""Harbor Bank: the demo persona's checking and savings accounts (W3-PLAN MON-5).

Made up like everything in a demo (no real institution). A second Plaid Item, linked
:data:`BANK_LINKED_DAYS_AGO` days ago with 730 days of history, so its rows cover the whole demo
next to the card: Everyday Checking ··4417 and High Yield Savings ··7702. Amounts keep Plaid's
sign on bank accounts too: positive is money leaving the account, negative is money coming in
(ARCHITECTURE §5), so a paycheck is negative.

* **Income.** A paycheck every other Friday (``INCOME_SALARY``, a raise some months ago), savings
  interest on the last day of each month (``INCOME_INTEREST_EARNED``) and a tax refund each April
  (``INCOME_TAX_REFUND``): Paycheck, Interest and Other Income.
* **Spending from checking.** Rent on the 1st (the persona's rule puts it in Rent), the water bill
  around the 20th (Utilities & Bills), a few debit-card groceries and one or two ATM withdrawals a
  month (``TRANSFER_OUT_WITHDRAWAL``: Cash & ATM). ACH debits and credits land on business days.
* **Moving money.** ``SAVINGS_TRANSFER`` ($1,500) to savings on the 2nd: a positive
  ``TRANSFER_OUT_SAVINGS`` row on checking and a negative ``TRANSFER_IN_SAVINGS`` row on savings
  (Transfers: neither spending nor income). Every Amex payment is mirrored on checking on the
  same day by a positive ``LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`` row (Card Payments): paid once,
  counted nowhere twice.
* **Like a US bank through Plaid:** ``transaction_code`` is empty (W3-PLAN §1), ACH rows have no
  merchant, debit purchases post a day or two after the purchase (yesterday's still pending),
  and nothing exists before the sync that would have brought it. Checking never drops below
  :data:`CHECKING_FLOOR`: the opening balance is chosen so.

The planner has its own random stream, so the card's random draws are the same with or without
it. Its ledger is planned before the card shapes the current month, which counts checking's
spending in budgeted categories as fixed (``shape_current_month``); the card payments it mirrors
and the savings interest come after the card's ledger.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random
import string
from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from budgetapp.devtools import demo_catalog as cat
from budgetapp.devtools.demo_plan import (
    DAY,
    ID_ALPHABET,
    Row,
    SyncRunPlan,
    _Planner,
    local_time,
    month_start,
)
from budgetapp.money import CENT, ZERO
from budgetapp.periods import clamp_day

#: Linked six weeks ago, after the card: wave 3 added bank accounts to a running app.
BANK_LINKED_DAYS_AGO = 45
#: A Friday: paydays are this date plus a multiple of 14 days.
PAYDAY_ANCHOR = dt.date(2024, 1, 5)
TAX_REFUND_DAY = (4, 18)
SALARY_PFC = ("INCOME", "INCOME_SALARY")
INTEREST_PFC = ("INCOME", "INCOME_INTEREST_EARNED")
TAX_REFUND_PFC = ("INCOME", "INCOME_TAX_REFUND")
TO_SAVINGS_PFC = ("TRANSFER_OUT", "TRANSFER_OUT_SAVINGS")
FROM_CHECKING_PFC = ("TRANSFER_IN", "TRANSFER_IN_SAVINGS")
ATM_PFC = ("TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL")
CARD_PAYMENT_PFC = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
CHECKING, SAVINGS = "checking", "savings"
#: Never let checking dip below this (the opening balance is chosen so it doesn't).
CHECKING_FLOOR = Decimal("1500.00")
SAVINGS_OPENING = Decimal("6500.00")


def _dollars(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def business_day(day: dt.date) -> dt.date:
    """An ACH row on a weekend lands on the Monday after."""
    while day.weekday() >= 5:
        day += DAY
    return day


class BankPlanner:
    """Plans Harbor Bank's rows next to a card planner (see the module docstring)."""

    def __init__(self, card: _Planner) -> None:
        self.card = card
        self.rng = random.Random(f"budgetapp-demo-bank:{card.seed}")
        self.today = card.today
        self.start = card.start
        self.rows: list[Row] = []
        self._seq = itertools.count(1_000_000)  # apart from the card's rows
        self.link_day = self.today - dt.timedelta(days=BANK_LINKED_DAYS_AGO)
        self.linked_at = local_time(self.link_day, 19, 12)
        self.balances: dict[str, tuple[Decimal, Decimal]] = {}

    # ------------------------------------------------------------------ helpers

    def code(self, length: int = 10) -> str:
        return "".join(self.rng.choice(string.digits) for _ in range(length))

    def plaid_id(self) -> str:
        return "".join(self.rng.choice(ID_ALPHABET) for _ in range(37))

    def add(
        self,
        account: str,
        kind: str,
        day: dt.date,
        amount: Decimal,
        name: str,
        raw: str,
        category: str,
        pfc: tuple[str, str],
        *,
        merchant_key: str | None = None,
        lag: int = 0,
        arrival_hour: int = 3,
        scheduled: bool = True,
    ) -> Row | None:
        """A row on checking or savings authorized on ``day``; ``None`` outside the window or
        when it reached Plaid after the last sync (an ACH row arrives overnight, before the 06:15
        sync; a purchase a few hours after it was made)."""
        if day < self.start or day > self.today:
            return None
        if local_time(day, arrival_hour) > self.card.last_sync:
            return None
        merchant = cat.BANK_MERCHANTS[merchant_key] if merchant_key else None
        row = Row(
            seq=next(self._seq),
            kind=kind,
            auth=day,
            amount=amount,
            name=name,
            raw=raw,
            category=category,
            merchant=merchant,
            merchant_name=merchant.merchant_name if merchant else None,
            pfc=pfc,
            confidence="VERY_HIGH" if merchant is None else merchant.confidence,
            code=None,  # US banks rarely send transaction codes (W3-PLAN §1)
            amex=None,
            channel=merchant.channel if merchant else "other",
            lag=lag,
            scheduled=scheduled,
            arrival_hour=arrival_hour,
            account=account,
        )
        posted = day + dt.timedelta(days=lag)
        row.posted = posted if posted <= self.today else None
        self.rows.append(row)
        return row

    def _ach(self, day: dt.date, kind: str, amount: Decimal, **fields: Any) -> Row | None:
        return self.add(CHECKING, kind, business_day(day), amount, **fields)

    def _months(self) -> Iterable[int]:
        """``back`` for every month from the window's first to this one."""
        back = 0
        while month_start(self.today, back) >= month_start(self.start):
            yield back
            back += 1

    def _monthly(self, back: int, day_of_month: int) -> dt.date:
        first = month_start(self.today, back)
        return clamp_day(first.year, first.month, day_of_month)

    # ------------------------------------------------------------------ the ledger

    def ledger(self) -> None:
        """Everything except interest and the card payments (they need the card's ledger and
        the savings balance), before the card shapes its current month."""
        self.paychecks()
        for back in self._months():
            self.rent(back)
            self.water(back)
            self.to_savings(back)
        self.debit_purchases()
        self.recent_pending()
        self.atm()
        self.tax_refunds()

    def paychecks(self) -> None:
        raise_from = month_start(self.today, cat.RAISE_MONTHS_AGO)
        day = PAYDAY_ANCHOR
        while day <= self.today:
            if day >= self.start:
                amount = cat.PAYCHECK[1] if day >= raise_from else cat.PAYCHECK[0]
                self.add(
                    CHECKING,
                    "paycheck",
                    day,
                    -amount,
                    f"{cat.EMPLOYER} PAYROLL",
                    f"{cat.EMPLOYER} INC DES:PAYROLL ID:{self.code()} INDN:RIVERA ALEX "
                    "CO ID:1943327650 PPD",
                    "Paycheck",
                    SALARY_PFC,
                    arrival_hour=2,
                )
            day += dt.timedelta(days=14)

    def rent(self, back: int) -> None:
        renewal = month_start(self.today, cat.RENEWAL_MONTHS_AGO)
        first = month_start(self.today, back)
        amount = cat.RENT[1] if first >= renewal else cat.RENT[0]
        self._ach(
            first,
            "purchase",
            amount,
            name="BAYVIEW PROPERTY MGMT",
            raw=f"BAYVIEW PROPERTY MGMT DES:RENT ID:{self.code()} INDN:ALEX RIVERA "
            "CO ID:4770112630 WEB",
            category="Rent",
            pfc=cat.BANK_MERCHANTS["bayview"].pfc,
            merchant_key="bayview",
        )

    def water(self, back: int) -> None:
        day = self._monthly(back, 20)
        summer = day.month in (6, 7, 8, 9)
        merchant = cat.BANK_MERCHANTS["sfpuc"]
        cents = self.rng.randint(merchant.lo, merchant.hi)
        if not summer:
            cents = merchant.lo + (cents - merchant.lo) // 3  # less watering outside summer
        self._ach(
            day,
            "purchase",
            _dollars(cents),
            name=merchant.name,
            raw=f"SF PUBLIC UTILITIES COMM DES:WATER ID:{self.code()} INDN:ALEX RIVERA WEB",
            category=merchant.category,
            pfc=merchant.pfc,
            merchant_key="sfpuc",
        )

    def to_savings(self, back: int) -> None:
        day = business_day(self._monthly(back, 2))
        out = self.add(
            CHECKING,
            "transfer",
            day,
            cat.SAVINGS_TRANSFER,
            "ONLINE TRANSFER TO SAV ...7702",
            f"ONLINE BANKING TRANSFER TO SAV 7702 CONF# {self.code(8)}",
            "Transfers",
            TO_SAVINGS_PFC,
        )
        if out is not None:
            self.add(
                SAVINGS,
                "transfer",
                day,
                -cat.SAVINGS_TRANSFER,
                "ONLINE TRANSFER FROM CHK ...4417",
                f"ONLINE BANKING TRANSFER FROM CHK 4417 CONF# {self.code(8)}",
                "Transfers",
                FROM_CHECKING_PFC,
            )

    def debit_purchases(self) -> None:
        """Groceries with the debit card: the Saturday farmers market and Rainbow now and then.
        Never while the persona is away (they are in-person purchases at home)."""
        day = self.start
        while day <= self.today:
            if day not in self.card.away:
                key = None
                if day.weekday() == 5 and self.rng.random() < 0.45:
                    key = "farmers_market"
                elif day.weekday() < 5 and self.rng.random() < 0.06:
                    key = "rainbow"
                if key is not None:
                    merchant = cat.BANK_MERCHANTS[key]
                    amount = _dollars(self.rng.randint(merchant.lo, merchant.hi))
                    self.add(
                        CHECKING,
                        "purchase",
                        day,
                        amount,
                        merchant.name,
                        merchant.raw,
                        merchant.category,
                        merchant.pfc,
                        merchant_key=key,
                        lag=self.rng.choice((1, 1, 2)),
                        arrival_hour=self.rng.randint(9, 18),
                        scheduled=False,
                    )
            day += DAY

    def recent_pending(self) -> None:
        """Yesterday's debit purchase hasn't posted yet (Plaid shows it pending)."""
        day = self.today - DAY
        if day in self.card.away or day < self.start:
            return
        merchant = cat.BANK_MERCHANTS["rainbow"]
        self.add(
            CHECKING,
            "purchase",
            day,
            _dollars(self.rng.randint(merchant.lo, merchant.hi)),
            merchant.name,
            merchant.raw,
            merchant.category,
            merchant.pfc,
            merchant_key="rainbow",
            lag=2,
            arrival_hour=17,
            scheduled=False,
        )

    def atm(self) -> None:
        for back in self._months():
            for _ in range(self.rng.choice((1, 1, 2))):
                day = self._monthly(back, self.rng.randint(1, 28))
                if day in self.card.away:
                    continue
                self.add(
                    CHECKING,
                    "atm",
                    day,
                    Decimal(self.rng.choice((60, 80, 100, 100, 120, 200))),
                    "ATM WITHDRAWAL 24TH ST & MISSION",
                    f"ATM WITHDRAWAL {self.code(6)} 24TH ST & MISSION SAN FRANCISCO CA",
                    "Cash & ATM",
                    ATM_PFC,
                    arrival_hour=self.rng.randint(10, 20),
                    scheduled=False,
                )

    def tax_refunds(self) -> None:
        for year in range(self.start.year, self.today.year + 1):
            amount = _dollars(self.rng.randint(40000, 140000))
            self._ach(
                dt.date(year, *TAX_REFUND_DAY),
                "income",
                -amount,
                name="IRS TREAS 310 TAX REF",
                raw=f"IRS TREAS 310 DES:TAX REF ID:{self.code(12)} INDN:RIVERA ALEX "
                "CO ID:9111036036 PPD",
                category="Other Income",
                pfc=TAX_REFUND_PFC,
            )

    # ------------------------------------------------------------------ after the card's ledger

    def card_payments(self, card_rows: Iterable[Row]) -> None:
        """Mirror every Amex payment on checking, the same day and amount (positive here)."""
        for payment in sorted(
            (r for r in card_rows if r.kind == "payment" and r.posted is not None),
            key=lambda r: (r.posted, r.seq),
        ):
            self.add(
                CHECKING,
                "card_payment",
                payment.posted,
                -payment.amount,
                "AMERICAN EXPRESS ACH PMT",
                f"AMERICAN EXPRESS DES:ACH PMT ID:W{self.code(9)} INDN:ALEX RIVERA "
                "CO ID:1134992250 WEB",
                "Card Payments",
                CARD_PAYMENT_PFC,
            )

    def interest(self) -> None:
        """Interest on the savings balance, paid on the last day of each month (this month's
        only once that day has come)."""
        moves = [r for r in self.rows if r.account == SAVINGS and r.posted is not None]
        paid = ZERO
        for back in sorted(self._months(), reverse=True):  # oldest month first
            last = month_start(self.today, back - 1) - DAY  # the month's last day
            if last < self.start:
                continue
            balance = (
                SAVINGS_OPENING + paid - sum((r.amount for r in moves if r.posted <= last), ZERO)
            )
            earned = (balance * cat.SAVINGS_APY / 12).quantize(CENT, ROUND_HALF_UP)
            row = self.add(
                SAVINGS,
                "interest",
                last,
                -earned,
                "INTEREST PAYMENT",
                "INTEREST PAYMENT",
                "Interest",
                INTEREST_PFC,
            )
            if row is not None:
                paid += earned

    def finish(self) -> None:
        """Plaid ids, and the opening and current balances (checking never below the floor)."""
        for row in sorted(self.rows, key=lambda r: (r.auth, r.seq)):
            row.plaid_id = self.plaid_id()
        running, lowest = ZERO, ZERO
        for row in sorted(
            (r for r in self.rows if r.account == CHECKING and r.posted), key=lambda r: r.posted
        ):
            running -= row.amount
            lowest = min(lowest, running)
        needed = ((CHECKING_FLOOR - lowest) / 100).to_integral_value(ROUND_CEILING) * 100
        opening = max(Decimal("2400.00"), needed).quantize(CENT)
        for account, start in ((CHECKING, opening), (SAVINGS, SAVINGS_OPENING)):
            mine = [r for r in self.rows if r.account == account]
            current = start - sum((r.amount for r in mine if r.posted is not None), ZERO)
            held = sum((r.amount for r in mine if r.posted is None and r.amount > 0), ZERO)
            self.balances[account] = (current, current - held)

    # ------------------------------------------------------------------ sync history, Item

    def sync_runs(self) -> list[SyncRunPlan]:
        """The bank syncs right after the card in every scheduled slot; all of them succeed."""
        runs: list[SyncRunPlan] = []
        slots = self.card.sync_slots()
        previous = slots[0][0] - dt.timedelta(hours=6)
        arrivals = sorted(local_time(r.auth, r.arrival_hour) for r in self.rows)
        for started, trigger in slots:
            started += dt.timedelta(seconds=self.rng.randint(45, 70))
            finished = started + dt.timedelta(seconds=self.rng.randint(2, 6))
            added = sum(1 for a in arrivals if previous < a <= started)
            runs.append(SyncRunPlan(trigger, "success", started, finished, added, 0, 0))
            previous = started
        return runs

    def item_and_accounts(
        self, runs: list[SyncRunPlan]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        last = runs[-1]
        item_id = self.plaid_id()
        update = last.started_at - dt.timedelta(hours=2, minutes=11)
        z = lambda t: t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        item = {
            "plaid_item_id": item_id,
            "institution_id": cat.BANK_INSTITUTION_ID,
            "institution_name": cat.BANK_INSTITUTION_NAME,
            "status": "healthy",
            "transactions_cursor": "CAESJ"
            + "".join(self.rng.choice(ID_ALPHABET) for _ in range(96)),
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "history_complete_at": self.linked_at + dt.timedelta(minutes=2, seconds=41),
            "days_requested": 730,
            "consented_products": ["transactions"],
            "liabilities_status": None,
            "linked_at": self.linked_at,
            "last_consented_at": self.linked_at,
            "consent_expires_at": None,
            "plaid_last_update_at": update,
            "plaid_last_failed_update_at": None,
            "last_sync_attempt_at": last.started_at,
            "last_sync_success_at": last.finished_at,
            "item_snapshot": {
                "item": {
                    "item_id": item_id,
                    "institution_id": cat.BANK_INSTITUTION_ID,
                    "institution_name": cat.BANK_INSTITUTION_NAME,
                    "available_products": ["balance", "signal"],
                    "billed_products": ["transactions"],
                    "products": ["transactions"],
                    "consented_products": ["transactions"],
                    "consent_expiration_time": None,
                    "update_type": "background",
                    "webhook": "",
                    "error": None,
                    "auth_method": None,
                },
                "status": {
                    "transactions": {
                        "last_successful_update": z(update),
                        "last_failed_update": None,
                    },
                    "last_webhook": None,
                },
                "request_id": "".join(self.rng.choice(string.ascii_letters) for _ in range(15)),
            },
        }
        accounts: dict[str, dict[str, Any]] = {}
        for key, name, official, mask, subtype in cat.BANK_ACCOUNTS:
            current, available = self.balances[key]
            mine = [r.posted for r in self.rows if r.account == key and r.posted]
            accounts[key] = {
                "plaid_account_id": self.plaid_id(),
                "name": name,
                "official_name": official,
                "mask": mask,
                "type": "depository",
                "subtype": subtype,
                "iso_currency_code": "USD",
                "current_balance": current,
                "available_balance": available,
                "credit_limit": None,
                "balances_as_of": last.finished_at,
                "plaid_history_start": min(mine) if mine else None,
            }
        return item, accounts
