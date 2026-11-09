"""Wave-3 seams between BANK, MONEY and INCOME, pinned by the integrator (W3-PLAN §4).

Each package was built and tested on its own branch; these tests check what only holds once the
three are merged: one rule for "a linked Amex card" wherever Amex CSV history may go (BANK-8,
MON-3), the owner's v1 Amex and a bank syncing side by side through their own Keychain keys with
MONEY's categories (C3, C6), INCOME's switch and figures on MONEY's categories (C4, C7), the
Accounts card and the accounts BANK marks as no longer shared (INC-3, BANK-5), and a duplicate
link refused before the exchange once the owner has upgraded (BANK-3).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Account, Category, PlaidItem, Transaction
from budgetapp.periods import month_of
from budgetapp.services import csv_import, insights, linking, reconcile, status
from budgetapp.services import sync as sync_service
from budgetapp.services.categorize import Categorizer, apply_decision
from budgetapp.services.linking import LinkRefused
from budgetapp.services.spending import period_income, period_spending
from tests.bank_factories import BANK_INSTITUTION_ID, BANK_NAME, make_bank_account, make_bank_item
from tests.factories import get_category, make_account, make_item, make_transaction, plaid_account
from tests.fake_plaid import FakePlaidGateway

D = Decimal
NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)


def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def production(tmp_path):
    """Production settings for fakes only: every folder is a temp folder, the database is the
    test connection (``session_factory``), Plaid is a ``FakePlaidGateway``."""
    return load_settings(
        "production",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        install_dir=tmp_path / "venv",
    )


@pytest.fixture
def prod_plaid() -> FakePlaidGateway:
    return FakePlaidGateway(plaid_env="production")


# --------------------------------------------------------------------------- Amex CSV history


def test_one_rule_says_where_amex_csv_history_may_go(db):
    """W3 integration (2d, 2j): "a linked Amex card" is a credit account of an American Express
    Item (``status.is_amex_card``) for the import targets and attach offers (MON-3), attaching
    after linking (BANK-8) and the server-side attach check; the id is defined once."""
    assert status.AMEX_INSTITUTION_ID == "ins_10"
    assert not hasattr(csv_import, "AMEX_INSTITUTION_ID")
    assert not hasattr(linking, "AMEX_INSTITUTION_ID")
    amex = make_account(db, name="Gold Card", mask="1001")  # make_item's default is ins_10
    hidden = make_account(db, amex.item, name="Old Card", mask="1002", is_hidden=True)
    # An Item saved without its institution id (never by this app) counts by its name.
    unnamed = make_item(db, institution_id=None, institution_name="American Express")
    legacy = make_account(db, unnamed, name="Blue Cash", mask="1003")
    amex_savings = make_bank_account(db, amex.item, subtype="savings")
    checking = make_bank_account(db)
    other = make_bank_item(db, institution_id="ins_demo_other", institution_name="Other Bank")
    other_card = make_account(db, other, name="Cash Rewards", mask="2002")
    manual = make_account(db, manual=True)
    row = make_transaction(db, manual, source="csv")

    everything = db.scalars(select(Account).order_by(Account.id))
    assert [a for a in everything if status.is_amex_card(a)] == [amex, hidden, legacy]
    assert linking.amex_cards(db) == [amex, hidden, legacy]
    targets = csv_import.import_targets(db)
    assert [o.value for o in targets.options] == [str(amex.id), str(legacy.id), str(manual.id)]
    (offer,) = csv_import.attach_offers(db)
    assert [account_id for account_id, _label in offer.targets] == [amex.id, legacy.id]
    for account in (checking, amex_savings, other_card):
        with pytest.raises(reconcile.AttachError, match="American Express card"):
            reconcile.attach_manual_account(db, manual.id, account.id)
        with pytest.raises(LinkRefused):
            linking.attach_manual(db, manual.id, account.id)
    assert row.account_id == manual.id
    assert reconcile.attach_manual_account(db, manual.id, legacy.id).moved_transactions == 1
    assert row.account_id == legacy.id


def test_only_an_amex_connection_keeps_csv_files_off_the_csv_only_account(db):
    """MON-3 with BANK's banks: a bank connection never blocks the CSV-only account in production;
    an American Express one does, also when it was saved without its institution id."""
    make_bank_account(db)
    assert not csv_import.linked_only(db, production=True)
    make_item(db, institution_id=None, institution_name="American Express")
    assert csv_import.linked_only(db, production=True)
    assert not csv_import.linked_only(db, production=False)


# --------------------------------------------------------------------------- the owner, wave 3


class Owner:
    """The owner's production state before wave 3 (one Amex Item, its token under the v1
    Keychain name) after the upgrade; :meth:`link_bank` links Harbor Bank through BANK's real
    flow (Link token, then the exchange), with fakes only."""

    def __init__(self, db, session_factory, plaid, store, settings, notifier) -> None:
        self.db, self.session_factory, self.plaid = db, session_factory, plaid
        self.store, self.settings, self.notifier = store, settings, notifier
        self.fake_amex = plaid.add_item(item_id="item-amex")
        self.amex = make_item(db, plaid_item_id="item-amex", last_sync_success_at=None)
        store.set(app_secrets.LEGACY_PRODUCTION_TOKEN, self.fake_amex.access_token)
        self.fake_bank = plaid.add_item(
            item_id="item-harbor",
            institution_id=BANK_INSTITUTION_ID,
            institution_name=BANK_NAME,
            consented_products=("transactions",),
            accounts=[
                plaid_account(account_id="chk", name="Everyday Checking", official_name=None,
                              mask="4417", type="depository", subtype="checking",
                              current="5200.00", available="5100.00"),
                plaid_account(account_id="sav", name="High Yield Savings", official_name=None,
                              mask="7702", type="depository", subtype="savings",
                              current="18000.00", available="18000.00"),
            ],
        )  # fmt: skip
        self.bank: PlaidItem | None = None

    def link_bank(self) -> PlaidItem:
        """**Link a bank or card**: the Link token moves the v1 token first (one Item), then the
        exchange stores Harbor Bank's token under its own key."""
        link = linking.new_link_token(self.db, self.plaid, self.settings, store=self.store)
        self.db.commit()
        meta = {
            "institution": {"name": BANK_NAME, "institution_id": BANK_INSTITUTION_ID},
            "accounts": [
                {"id": "chk", "name": "Everyday Checking", "mask": "4417", "type": "depository"},
                {"id": "sav", "name": "High Yield Savings", "mask": "7702", "type": "depository"},
            ],
        }
        linking.exchange(
            self.session_factory,
            self.plaid,
            self.store,
            self.settings,
            self.plaid.public_token_for(self.fake_bank),
            meta,
            attempt=link.attempt,
            sleep=no_sleep,
        )
        self.db.expire_all()
        self.bank = self.db.scalars(
            select(PlaidItem).where(PlaidItem.plaid_item_id == "item-harbor")
        ).one()
        return self.bank

    def sync(self, item: PlaidItem) -> str:
        outcome = sync_service.sync_item(
            self.session_factory,
            self.plaid,
            self.store,
            self.settings,
            item.id,
            trigger="schedule",
            notifier=self.notifier,
            sleep=no_sleep,
        )
        self.db.expire_all()
        return outcome.status

    def category(self, plaid_id: str) -> str:
        row = self.db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == plaid_id)
        ).one()
        return self.db.get(Category, row.category_id).name


@pytest.fixture
def owner(db, session_factory, prod_plaid, secrets, production, notifier, freeze_today) -> Owner:
    freeze_today(now=NOW)
    return Owner(db, session_factory, prod_plaid, secrets, production, notifier)


def test_the_card_and_the_bank_sync_side_by_side_and_count_each_charge_once(owner, db):
    """C3 × C6: linking the bank moves the v1 Amex token to its own key (once) and saves the
    bank's under its own; each connection then syncs with its own token. The Amex payment from
    checking is a Card Payment like the card's side of it, so the card's charge counts once, and
    the paycheck is income."""
    owner.link_bank()
    assert owner.store.values == {
        app_secrets.PRODUCTION_ITEM_INDEX: '["item-amex", "item-harbor"]',
        "plaid.access_token.production.item-amex": owner.fake_amex.access_token,
        "plaid.access_token.production.item-harbor": owner.fake_bank.access_token,
    }
    day = NOW.date() - dt.timedelta(days=5)
    card = owner.fake_amex.default_account_id
    add = owner.plaid.add_transaction
    add(
        owner.fake_amex,
        transaction_id="groceries",
        account_id=card,
        amount="120.00",
        date=day,
        name="WHOLE FOODS",
        pfc_primary="FOOD_AND_DRINK",
        pfc_detailed="FOOD_AND_DRINK_GROCERIES",
    )
    add(
        owner.fake_amex,
        transaction_id="card-side",
        account_id=card,
        amount="-120.00",
        date=day,
        name="PAYMENT RECEIVED - THANK YOU",
        merchant_name=None,
        transaction_code="payment",
        pfc_primary="LOAN_PAYMENTS",
        pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    add(
        owner.fake_bank,
        transaction_id="bank-side",
        account_id="chk",
        amount="120.00",
        date=day,
        name="AMERICAN EXPRESS ACH PMT",
        merchant_name=None,
        pfc_primary="LOAN_PAYMENTS",
        pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
    )
    add(
        owner.fake_bank,
        transaction_id="pay",
        account_id="chk",
        amount="-3000.00",
        date=day,
        name="ACME ROBOTICS PAYROLL",
        merchant_name=None,
        pfc_primary="INCOME",
        pfc_detailed="INCOME_SALARY",
    )
    add(owner.fake_bank, transaction_id="to-sav", account_id="chk", amount="500.00", date=day,
        name="ONLINE TRANSFER TO SAV ...7702", merchant_name=None, pfc_primary="TRANSFER_OUT",
        pfc_detailed="TRANSFER_OUT_SAVINGS")  # fmt: skip

    assert owner.sync(owner.amex) == "success"
    assert owner.sync(owner.bank) == "success"
    used = [c["access_token"] for c in owner.plaid.calls_to("transactions_sync")]
    assert used == [owner.fake_amex.access_token, owner.fake_bank.access_token]
    keys = ("groceries", "card-side", "bank-side", "pay", "to-sav")
    assert {k: owner.category(k) for k in keys} == {
        "groceries": "Groceries",
        "card-side": "Card Payments",
        "bank-side": "Card Payments",
        "pay": "Paycheck",
        "to-sav": "Transfers",
    }
    month = month_of(day)
    assert period_spending(db, month).total == D("120.00")  # the charge, once
    assert period_income(db, month).total == D("3000.00")
    assert app_secrets.LEGACY_PRODUCTION_TOKEN not in owner.store.values


@pytest.mark.parametrize(
    ("institution_id", "name"),
    [("ins_10", "American Express"), (BANK_INSTITUTION_ID, BANK_NAME)],
    ids=["amex-again", "bank-again"],
)
def test_after_the_upgrade_a_second_link_to_either_bank_is_refused_before_the_exchange(
    owner, db, session_factory, institution_id, name
):
    """BANK-3 on the upgraded owner with the Amex and Harbor Bank: linking either again is
    refused before Plaid's exchange, so none of the Trial plan's 10 connections is used and
    Keychain keeps exactly what it had."""
    owner.link_bank()
    link = linking.new_link_token(db, owner.plaid, owner.settings, store=owner.store)
    db.commit()
    again = owner.plaid.add_item(item_id="again", institution_id=institution_id,
                                 institution_name=name)  # fmt: skip
    before = {k: v for k, v in owner.store.values.items() if "link_in_progress" not in k}
    exchanged = len(owner.plaid.calls_to("exchange_public_token"))  # Harbor Bank's own link
    meta = {
        "institution": {"name": name, "institution_id": institution_id},
        "accounts": [{"id": "x", "name": "Account", "mask": "0000", "type": "credit"}],
    }
    with pytest.raises(LinkRefused, match="already linked"):
        linking.exchange(
            session_factory,
            owner.plaid,
            owner.store,
            owner.settings,
            owner.plaid.public_token_for(again),
            meta,
            attempt=link.attempt,
            sleep=no_sleep,
        )
    assert len(owner.plaid.calls_to("exchange_public_token")) == exchanged == 1
    assert {k: v for k, v in owner.store.values.items() if "link_in_progress" not in k} == before
    assert app_secrets.production_item_ids(store=owner.store) == ["item-amex", "item-harbor"]


# --------------------------------------------------------------------------- income and MONEY


def test_the_income_switch_and_hint_follow_moneys_categories(db, freeze_today):
    """C2 × C4/C7: MONEY's categories exist in every database (0005), but categories alone are
    no income; card credits make the switch appear with the card-only hint; a bank account ends
    the hint, and MONEY's categorizer books its paycheck as income while the payment of the card
    and a transfer to savings stay out of both income and spending."""
    freeze_today("2026-10-20")
    kinds = {name: get_category(db, name).kind for name in
             ("Paycheck", "Interest", "Other Income", "Transfers", "Cash & ATM")}  # fmt: skip
    assert kinds == {"Paycheck": "income", "Interest": "income", "Other Income": "income",
                     "Transfers": "transfer", "Cash & ATM": "expense"}  # fmt: skip
    assert insights.income_switch(db) == insights.IncomeSwitch(available=False, on=True)
    card = make_account(db)
    make_transaction(db, card, amount="120.00", date="2026-10-05",
                     category=get_category(db, "Groceries"))  # fmt: skip
    make_transaction(db, card, amount="-120.00", date="2026-10-15",
                     category=get_category(db, "Card Payments"))  # fmt: skip
    assert not insights.income_switch(db).available  # a card payment is no income
    make_transaction(db, card, amount="-25.00", date="2026-10-06",
                     category=get_category(db, "Credits & Rewards"))  # fmt: skip
    first = insights.dashboard(db)
    assert first.income_switch.showing and first.income.cards_only
    assert (first.income.total, first.income.spent, first.income.net) == (
        D("25.00"),
        D("120.00"),
        D("-95.00"),
    )

    checking = make_bank_account(db)
    rows = {
        "pay": ("-3000.00", "ACME PAYROLL", "INCOME", "INCOME_SALARY"),
        "amex": ("120.00", "AMERICAN EXPRESS ACH PMT", "LOAN_PAYMENTS",
                 "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
        "save": ("500.00", "TRANSFER TO SAVINGS", "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
        "atm": ("60.00", "ATM WITHDRAWAL", "TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL"),
    }  # fmt: skip
    made = {
        key: make_transaction(db, checking, amount=amount, date="2026-10-16", name=name,
                              pfc_primary=primary, pfc_detailed=detailed)
        for key, (amount, name, primary, detailed) in rows.items()
    }  # fmt: skip
    categorizer = Categorizer.load(db)
    for row in made.values():
        apply_decision(row, categorizer.decide(row))
    db.flush()
    assert {k: db.get(Category, r.category_id).name for k, r in made.items()} == {
        "pay": "Paycheck",
        "amex": "Card Payments",
        "save": "Transfers",
        "atm": "Cash & ATM",
    }
    after = insights.dashboard(db)
    assert not after.income.cards_only
    assert after.income.largest.name == "Paycheck"
    assert (after.income.total, after.income.spent, after.income.net) == (
        D("3025.00"),
        D("180.00"),  # the card's groceries and the ATM withdrawal
        D("2845.00"),
    )
    insights.set_show_income(db, False)
    assert insights.dashboard(db).income is None


# --------------------------------------------------------------------------- the Accounts card


def test_the_accounts_card_leaves_out_accounts_plaid_no_longer_shares(db):
    """INC-3 × BANK-5: an account Plaid stopped listing keeps its last balance on the card,
    marked, but adds nothing to Cash or Owed on cards and has no say in "as of"."""
    checking = make_bank_account(
        db, current_balance=D("5200.00"), available_balance=D("5100.00"), balances_as_of=NOW
    )
    savings = make_bank_account(
        db,
        checking.item,
        subtype="savings",
        current_balance=D("18000.00"),
        balances_as_of=NOW - dt.timedelta(days=30),
    )
    card = make_account(db, current_balance=D("1234.56"), balances_as_of=NOW)
    old_card = make_account(db, card.item, name="Old Card", mask="9999",
                            current_balance=D("300.00"),
                            balances_as_of=NOW - dt.timedelta(days=40))  # fmt: skip
    for account in (savings, old_card):
        assert sync_service.mark_not_shared(account, NOW)
    db.flush()
    cardview = insights.account_balances(db)
    shown = {a.name: a.no_longer_shared for a in (*cardview.banks, *cardview.cards)}
    assert shown == {
        "Everyday Checking": False,
        "High Yield Savings": True,
        "Platinum Card": False,
        "Old Card": True,
    }
    assert (cardview.cash, cardview.owed, cardview.as_of) == (D("5200.00"), D("1234.56"), NOW)
