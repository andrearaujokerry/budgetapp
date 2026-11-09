"""Bank money through the real sync engine and FakePlaidGateway (W3-PLAN MON-1, MON-2, C3, C5).

A bank Item with a checking and a savings account syncs: each row lands in its bank category,
an edit made to a pending transfer survives its posting (step 0 never drops it on a bank), and
large-charge alerts announce spending only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.models import Category, Transaction
from budgetapp.periods import month_of
from budgetapp.services import sync
from budgetapp.services.events import SyncOutcome
from budgetapp.services.spending import period_spending
from tests.bank_factories import BANK_INSTITUTION_ID, BANK_NAME
from tests.factories import get_category, make_item, plaid_account, set_app_settings

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
CHECKING, SAVINGS = "harbor-chk", "harbor-sav"


class Bank:
    """A fake Harbor Bank Item linked in the test database, its token saved."""

    def __init__(self, db, session_factory, fake, store, settings, notifier) -> None:
        self.db, self.session_factory, self.fake = db, session_factory, fake
        self.store, self.settings, self.notifier = store, settings, notifier
        accounts = [
            plaid_account(
                account_id=CHECKING,
                name="Everyday Checking",
                official_name=None,
                mask="4417",
                type="depository",
                subtype="checking",
                current="5200.00",
                available="5100.00",
            ),
            plaid_account(
                account_id=SAVINGS,
                name="High Yield Savings",
                official_name=None,
                mask="7702",
                type="depository",
                subtype="savings",
                current="18000.00",
                available="18000.00",
            ),
        ]
        self.fake_item = fake.add_item(
            item_id="item-harbor",
            institution_id=BANK_INSTITUTION_ID,
            institution_name=BANK_NAME,
            accounts=accounts,
            consented_products=("transactions",),
        )
        app_secrets.store_access_token(
            settings, "item-harbor", self.fake_item.access_token, store=store
        )
        self.item = make_item(
            db,
            plaid_item_id="item-harbor",
            institution_id=BANK_INSTITUTION_ID,
            institution_name=BANK_NAME,
            last_sync_success_at=None,
            consented_products=["transactions"],
        )

    def add(self, account: str = CHECKING, **fields) -> dict:
        fields.setdefault("merchant_name", None)
        fields.setdefault("payment_channel", "other")
        return self.fake.add_transaction(self.fake_item, account_id=account, **fields)

    def sync(self) -> SyncOutcome:
        outcome = sync.sync_item(
            self.session_factory,
            self.fake,
            self.store,
            self.settings,
            self.item.id,
            trigger="schedule",
            notifier=self.notifier,
            sleep=lambda _s: None,
        )
        self.db.expire_all()
        return outcome

    def row(self, plaid_id: str) -> Transaction:
        self.db.expire_all()
        return self.db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == plaid_id)
        ).one()

    def category(self, plaid_id: str) -> tuple[str, str]:
        row = self.row(plaid_id)
        return self.db.get(Category, row.category_id).name, row.category_source


@pytest.fixture
def bank(db, session_factory, fake_plaid, secrets, settings, notifier, freeze_today) -> Bank:
    freeze_today(now=NOW)
    return Bank(db, session_factory, fake_plaid, secrets, settings, notifier)


def today() -> dt.date:
    return clock.today()


def test_each_bank_row_lands_in_its_bank_category(bank, db):
    day = today() - dt.timedelta(days=3)
    rows = {
        "pay": bank.add(
            transaction_id="pay",
            amount="-3412.18",
            date=day,
            name="ACME ROBOTICS PAYROLL",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_SALARY",
        ),
        "to-sav": bank.add(
            transaction_id="to-sav",
            amount="500.00",
            date=day,
            name="ONLINE TRANSFER TO SAV ...7702",
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_SAVINGS",
        ),
        "from-chk": bank.add(
            SAVINGS,
            transaction_id="from-chk",
            amount="-500.00",
            date=day,
            name="ONLINE TRANSFER FROM CHK ...4417 THANK YOU",  # reads like a card payment
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_SAVINGS",
        ),
        "interest": bank.add(
            SAVINGS,
            transaction_id="interest",
            amount="-61.50",
            date=day,
            name="INTEREST PAYMENT",
            pfc_primary="INCOME",
            pfc_detailed="INCOME_INTEREST_EARNED",
        ),
        "atm": bank.add(
            transaction_id="atm",
            amount="100.00",
            date=day,
            name="ATM WITHDRAWAL 24TH ST",
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_WITHDRAWAL",
        ),
        "amex": bank.add(
            transaction_id="amex",
            amount="2512.37",
            date=day,
            name="AMERICAN EXPRESS ACH PMT",
            pfc_primary="LOAN_PAYMENTS",
            pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        ),
        "rent": bank.add(
            transaction_id="rent",
            amount="2450.00",
            date=day,
            name="BAYVIEW PROPERTY MGMT",
            merchant_name="Bayview Property Management",
            pfc_primary="RENT_AND_UTILITIES",
            pfc_detailed="RENT_AND_UTILITIES_RENT",
        ),
    }
    assert bank.sync().status == "success"
    assert {key: bank.category(key) for key in rows} == {
        "pay": ("Paycheck", "plaid"),
        "to-sav": ("Transfers", "plaid"),
        "from-chk": ("Transfers", "plaid"),
        "interest": ("Interest", "plaid"),
        "atm": ("Cash & ATM", "plaid"),
        "amex": ("Card Payments", "plaid"),
        "rent": ("Home", "plaid"),
    }
    spent = period_spending(db, month_of(day))
    assert spent.total == Decimal("2550.00")  # rent and the ATM: never the transfers or the card


def test_an_edit_to_a_pending_transfer_survives_its_posting(bank, db):
    """Step 0 is for cards: the posted row keeps the category the owner gave the pending one."""
    day = today() - dt.timedelta(days=1)
    bank.fake.add_pending(
        bank.fake_item,
        account_id=SAVINGS,
        transaction_id="zelle",
        amount="-75.00",
        date=day,
        name="ZELLE FROM SAM RIVERA",
        merchant_name=None,
        pfc_primary="TRANSFER_IN",
        pfc_detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
    )
    bank.sync()
    other_income = get_category(db, "Other Income")
    pending = bank.row("zelle")
    pending.category_id, pending.category_source = other_income.id, "user"
    pending.user_edited_at = NOW
    db.commit()
    bank.fake.post_pending(bank.fake_item, "zelle", new_id="zelle-posted")
    assert bank.sync().status == "success"
    posted = bank.row("zelle-posted")
    assert (posted.category_id, posted.category_source) == (other_income.id, "user")
    assert not posted.needs_review


def test_large_charge_alerts_announce_spending_only(bank, db, notifier):
    set_app_settings(db, large_charge_threshold=Decimal("1000.00"))
    db.commit()
    assert bank.sync().initial_history_load  # nothing yet: the history load never notifies
    day = today() - dt.timedelta(days=1)
    bank.add(
        transaction_id="amex",
        amount="2512.37",
        date=day,
        name="AMERICAN EXPRESS ACH PMT",
        pfc_primary="LOAN_PAYMENTS",
        pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    bank.add(
        transaction_id="to-sav",
        amount="3000.00",
        date=day,
        name="ONLINE TRANSFER TO SAV ...7702",
        pfc_primary="TRANSFER_OUT",
        pfc_detailed="TRANSFER_OUT_SAVINGS",
    )
    bank.add(
        transaction_id="rent",
        amount="2450.00",
        date=day,
        name="BAYVIEW PROPERTY MGMT",
        merchant_name="Bayview Property Management",
        pfc_primary="RENT_AND_UTILITIES",
        pfc_detailed="RENT_AND_UTILITIES_RENT",
    )
    assert bank.sync().status == "success"
    assert notifier.titles == ["$2,450.00 at Bayview Property Management"]
