"""Bank money through the real sync engine and FakePlaidGateway (W3-PLAN MON-1, MON-2, C3, C5).

A bank Item with a checking and a savings account syncs: each row lands in its bank category,
an edit made to a pending transfer survives its posting (step 0 never drops it on a bank), and
large-charge alerts announce spending only. Every test runs twice (wave-3 integration): in the
Sandbox and with production settings, where the Item's token is BANK's per-Item Keychain key
(W3-PLAN C6), so bank money always syncs through the token of its own connection.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import clock
from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Category, Transaction
from budgetapp.periods import month_of
from budgetapp.services import alerts, categorize, sync
from budgetapp.services import transactions as txns
from budgetapp.services.events import SyncOutcome
from budgetapp.services.spending import period_income, period_spending
from tests.bank_factories import BANK_INSTITUTION_ID, BANK_NAME
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_item,
    make_rule,
    make_transaction,
    plaid_account,
    set_app_settings,
)
from tests.fake_plaid import FakePlaidGateway

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


@pytest.fixture(params=["sandbox", "production"])
def bank(
    request, db, session_factory, fake_plaid, secrets, settings, notifier, freeze_today, tmp_path
) -> Bank:
    freeze_today(now=NOW)
    if request.param == "production":  # fakes and a settings override only: nothing real
        settings = load_settings(
            "production",
            state_dir=tmp_path / "state",
            logs_dir=tmp_path / "logs",
            backups_dir=tmp_path / "backups",
            install_dir=tmp_path / "venv",
        )
        fake_plaid = FakePlaidGateway(plaid_env="production")
    return Bank(db, session_factory, fake_plaid, secrets, settings, notifier)


def test_the_bank_syncs_with_its_own_connections_token(bank):
    """Wave-3 integration (C6 × MON-1): the token sits under BANK's per-Item key
    ``plaid.access_token.<env>.item-harbor`` and is the only one the bank's sync uses."""
    env = bank.settings.plaid_env
    key = app_secrets.access_token_key(env, "item-harbor")
    assert bank.store.values[key] == bank.fake_item.access_token
    assert app_secrets.item_ids(env, store=bank.store) == ["item-harbor"]
    assert app_secrets.LEGACY_PRODUCTION_TOKEN not in bank.store.values
    bank.add(transaction_id="pay", amount="-3412.18", date=today(), name="ACME ROBOTICS PAYROLL",
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")  # fmt: skip
    assert bank.sync().status == "success"
    used = {call["access_token"] for call in bank.fake.calls_to("transactions_sync")}
    assert used == {bank.fake_item.access_token}
    assert bank.category("pay") == ("Paycheck", "plaid")


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


def test_loan_payments_are_spending_and_loan_money_is_a_transfer(bank, db):
    """W3-MONEY review (major): on checking, the cards' loan mappings (Card Payments for a loan
    payment, Other for a cash advance) made a loan payment vanish from spending and a cash
    advance paid in lower it."""
    day = today() - dt.timedelta(days=2)
    payments, deposits = "LOAN_PAYMENTS", "LOAN_DISBURSEMENTS"
    rows = {
        "loan": ("450.00", "LOAN CO PMT", payments, "LOAN_PAYMENTS_OTHER_PAYMENT"),
        "car": ("480.00", "AUTO FINANCE PMT", payments, "LOAN_PAYMENTS_CAR_PAYMENT"),
        "advance": ("-500.00", "PAYDAY ADVANCE", deposits, "LOAN_DISBURSEMENTS_CASH_ADVANCES"),
        "funding": ("-5000.00", "LOAN CO FUNDING", deposits, "LOAN_DISBURSEMENTS_PERSONAL"),
    }
    for key, (amount, name, primary, detailed) in rows.items():
        bank.add(
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            pfc_primary=primary,
            pfc_detailed=detailed,
        )
    assert bank.sync().status == "success"
    assert {key: bank.category(key) for key in rows} == {
        "loan": ("Other", "plaid"),
        "car": ("Transportation", "plaid"),
        "advance": ("Transfers", "plaid"),
        "funding": ("Transfers", "plaid"),
    }
    assert period_spending(db, month_of(day)).total == Decimal("930.00")


def test_a_card_rule_never_makes_the_payment_from_checking_spending(bank, db, notifier):
    """W3-MONEY review: a rule written for card rows that matches the ACH text leaves the card's
    payment from checking in Card Payments (no large-charge alert, never counted twice) and still
    categorizes the other rows it matches."""
    set_app_settings(db, large_charge_threshold=Decimal("1000.00"))
    make_rule(db, get_category(db, "Travel"), description_pattern="AMERICAN EXPRESS")
    db.commit()
    assert bank.sync().initial_history_load
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
        transaction_id="trip",
        amount="1100.00",
        date=day,
        name="AMERICAN EXPRESS TRAVEL",
        pfc_primary="TRAVEL",
        pfc_detailed="TRAVEL_FLIGHTS",
    )
    assert bank.sync().status == "success"
    assert bank.category("amex") == ("Card Payments", "plaid")
    assert bank.category("trip") == ("Travel", "rule")
    assert notifier.titles == ["$1,100.00 at AMERICAN EXPRESS TRAVEL"]
    assert period_spending(db, month_of(day)).total == Decimal("1100.00")


def test_large_charges_follow_rules_and_raises(bank, db, notifier):
    """C5 (from the W3-MONEY review's checks): spending a rule makes alerts, a transfer never
    does, also when a pending one posts higher; a pending charge that posts above the amount
    alerts once."""
    set_app_settings(db, large_charge_threshold=Decimal("400.00"))
    make_rule(db, get_category(db, "Gifts & Donations"), description_pattern="ZELLE TO MOM")
    db.commit()
    assert bank.sync().initial_history_load
    day = today() - dt.timedelta(days=1)
    for key, amount, name, detailed in (
        ("atm", "500.00", "ATM WITHDRAWAL", "TRANSFER_OUT_WITHDRAWAL"),
        ("zelle", "600.00", "ZELLE TO MOM", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
        ("venmo", "700.00", "VENMO PAYMENT", "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
    ):
        bank.add(
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            pfc_primary="TRANSFER_OUT",
            pfc_detailed=detailed,
        )
    bank.add(
        SAVINGS,
        transaction_id="interest",
        amount="-900.00",
        date=day,
        name="INTEREST",
        pfc_primary="INCOME",
        pfc_detailed="INCOME_INTEREST_EARNED",
    )
    for key, amount, name, merchant, primary, detailed in (
        ("hotel", "350.00", "HOTEL", "Hotel", "TRAVEL", "TRAVEL_LODGING"),
        ("to-sav", "380.00", "TRANSFER TO SAV", None, "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
    ):
        bank.fake.add_pending(
            bank.fake_item,
            account_id=CHECKING,
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            merchant_name=merchant,
            pfc_primary=primary,
            pfc_detailed=detailed,
        )
    assert bank.sync().status == "success"
    first = sorted(notifier.titles)
    assert first == ["$500.00 at ATM WITHDRAWAL", "$600.00 at ZELLE TO MOM"]
    bank.fake.post_pending(bank.fake_item, "hotel", amount="455.00", new_id="hotel-posted")
    bank.fake.post_pending(bank.fake_item, "to-sav", amount="1380.00", new_id="to-sav-posted")
    assert bank.sync().status == "success"
    assert sorted(notifier.titles) == sorted([*first, "$455.00 at Hotel"])


def test_the_first_bank_sync_checks_budgets_but_announces_no_large_charge(bank, db, notifier):
    """Final review reqs-2, as going-live's "After your first bank account syncs" says: budgets
    and an overall target sized for the card count checking's spending from the bank's first sync
    on (W3-PLAN §0), loan payments in Transportation and Education, and the budget alerts come at
    once (ALR-1), while large-charge alerts skip the history Plaid sends when it is linked."""
    set_app_settings(
        db,
        budget_alerts_enabled=True,
        alert_thresholds=[80, 100],
        large_charge_threshold=Decimal("500.00"),
    )
    card = make_account(db)  # the owner's American Express card, synced before
    home, groceries = get_category(db, "Home"), get_category(db, "Groceries")
    make_budget(db, home, "300.00")
    make_budget(db, None, "2000.00")  # the overall target, sized for the card
    day = today() - dt.timedelta(days=5)
    make_transaction(db, card, amount="120.00", date=day, category=home)
    make_transaction(db, card, amount="900.00", date=day, category=groceries)
    db.commit()
    assert alerts.evaluate_budgets(db, notifier) == [] and notifier.titles == []
    loans = "LOAN_PAYMENTS"
    rows = {
        "rent": ("1800.00", "OAKWOOD PROPERTY MGMT", "RENT_AND_UTILITIES", "RENT", "Home"),
        "car": ("420.00", "AUTO LOAN PMT", loans, "CAR_PAYMENT", "Transportation"),
        "student": ("310.00", "STUDENT LOAN PMT", loans, "STUDENT_LOAN_PAYMENT", "Education"),
    }
    for key, (amount, name, primary, detailed, _category) in rows.items():
        bank.add(
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            pfc_primary=primary,
            pfc_detailed=f"{primary}_{detailed}",
        )
    outcome = bank.sync()
    assert outcome.ok and outcome.initial_history_load
    assert {key: bank.category(key)[0] for key in rows} == {
        "rent": "Home",
        "car": "Transportation",
        "student": "Education",
    }
    assert "Home: over budget" in notifier.titles
    assert "Over your overall budget" in notifier.titles
    assert not [t for t in notifier.titles if t.startswith("$")]  # no large charge, not even rent


def test_payments_to_people_are_transfers_until_a_rule_moves_them(bank, db):
    """Final review money-1 (W3-PLAN C3), as using-the-app and going-live now say: Plaid's
    TRANSFER_* codes also cover payments to and from other people, apps and wires, so those rows
    land in Transfers, neither spending nor income, until a rule of the owner's moves them."""
    make_rule(db, get_category(db, "Services"), description_pattern="ZELLE PAYMENT TO JORDAN")
    db.commit()
    day = today() - dt.timedelta(days=2)
    apps = "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"
    rows = {
        "zelle": ("300.00", "ZELLE PAYMENT TO JORDAN LEE", "TRANSFER_OUT", apps),
        "venmo": ("45.00", "VENMO PAYMENT 1023", "TRANSFER_OUT", apps),
        "wire": ("900.00", "OUTGOING WIRE BAY BUILDERS", "TRANSFER_OUT", "TRANSFER_OUT_WIRE"),
        "check": ("-500.00", "MOBILE CHECK DEPOSIT", "TRANSFER_IN", "TRANSFER_IN_DEPOSIT"),
    }
    for key, (amount, name, primary, detailed) in rows.items():
        bank.add(
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            pfc_primary=primary,
            pfc_detailed=detailed,
        )
    assert bank.sync().status == "success"
    assert {key: bank.category(key) for key in rows} == {
        "zelle": ("Services", "rule"),
        "venmo": ("Transfers", "plaid"),
        "wire": ("Transfers", "plaid"),
        "check": ("Transfers", "plaid"),
    }
    month = month_of(day)
    assert period_spending(db, month).total == Decimal("300.00")  # only what the rule moved
    assert period_income(db, month).total == Decimal("0.00")


def test_the_owner_files_a_miscoded_card_payment_from_checking_once(bank, db, notifier):
    """Final review money-2, the remedy going-live's checklist gives: a payment to the card that
    Plaid codes as spending (here LOAN_PAYMENTS_OTHER_PAYMENT, which maps to Other) counts the
    card's charges a second time; set to Card Payments and turned into a rule with "Always
    categorize like this", it and every later payment are card payments, so the card's charges
    count once and no payment sets off a large-charge alert."""
    set_app_settings(db, large_charge_threshold=Decimal("1000.00"))
    card = make_account(db)
    day = today() - dt.timedelta(days=12)
    shopping, card_payments = get_category(db, "Shopping"), get_category(db, "Card Payments")
    make_transaction(db, card, amount="1500.00", date=day, name="BEST BUY", category=shopping)
    make_transaction(
        db,
        card,
        amount="-1500.00",
        date=day,
        name="PAYMENT RECEIVED - THANK YOU",
        category=card_payments,
        category_source="system",
    )
    db.commit()
    assert bank.sync().initial_history_load
    payment = {"name": "AMEX EPAYMENT ACH PMT", "pfc_primary": "LOAN_PAYMENTS",
               "pfc_detailed": "LOAN_PAYMENTS_OTHER_PAYMENT"}  # fmt: skip
    bank.add(transaction_id="pmt-1", amount="1500.00", date=day, **payment)
    assert bank.sync().status == "success"
    assert bank.category("pmt-1") == ("Other", "plaid")  # spending: counted twice
    month = month_of(day)
    assert period_spending(db, month).total == Decimal("3000.00")
    first = bank.row("pmt-1")
    txns.update(db, first.id, category_id=card_payments.id)
    draft = txns.one_click_draft(db, first.id)
    assert not draft.is_payment  # an ordinary rule: rules apply to this row
    txns.create_one_click_rule(
        db,
        first.id,
        merchant_pattern=draft.merchant_pattern,
        category_id=draft.category_id,
        apply_to_past=True,
    )
    db.commit()
    assert period_spending(db, month).total == Decimal("1500.00")  # the purchase, once
    notifier.sent.clear()
    bank.add(transaction_id="pmt-2", amount="1180.00", date=today(), **payment)
    assert bank.sync().status == "success"
    assert bank.category("pmt-2") == ("Card Payments", "rule")
    assert [t for t in notifier.titles if t.startswith("$")] == []


def test_money_in_plaid_did_not_classify_is_income_and_needs_review(bank, db, notifier):
    """Final review money-3: a deposit Plaid sends without a category (nullable, RESEARCH T1.5)
    or as OTHER took Uncategorized or Other, so it lowered spending like a refund and never
    counted as income. It is now other income, marked for review when it arrives; money out and
    the deposit Plaid did classify are unchanged, and re-applying categories agrees."""
    day = today() - dt.timedelta(days=3)
    rows = {
        "rent": (
            "2450.00",
            "BAYVIEW PROPERTY MGMT",
            "RENT_AND_UTILITIES",
            "RENT_AND_UTILITIES_RENT",
        ),
        "dep": ("-1800.00", "MOBILE DEPOSIT", None, None),
        "other": ("-600.00", "DEPOSIT 0423", "OTHER", "OTHER_OTHER"),
        "check": ("40.00", "CHECK 1043", None, None),
    }
    for key, (amount, name, primary, detailed) in rows.items():
        bank.add(
            transaction_id=key,
            amount=amount,
            date=day,
            name=name,
            pfc_primary=primary,
            pfc_detailed=detailed,
        )
    assert bank.sync().status == "success"
    assert {key: bank.category(key) for key in rows} == {
        "rent": ("Home", "plaid"),
        "dep": ("Other Income", "default"),
        "other": ("Other Income", "default"),
        "check": ("Uncategorized", "default"),
    }
    for key in ("dep", "other"):
        row = bank.row(key)
        assert row.needs_review and row.review_reason == categorize.UNCLASSIFIED_MONEY_IN_REVIEW
        assert not txns.get_row(db, row.id).is_refund
    assert not bank.row("rent").needs_review and not bank.row("check").needs_review
    month = month_of(day)
    assert period_spending(db, month).total == Decimal("2490.00")  # rent and the check
    assert period_income(db, month).total == Decimal("2400.00")
    assert categorize.recategorize(db).changed == 0
