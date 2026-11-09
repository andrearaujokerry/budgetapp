"""The owner's live upgrade to wave 3, rehearsed (W3-PLAN §0, C6, C8; integrator duty 2b).

The production database and Keychain may already hold a customized v1 install: one American
Express Item (``ins_10``) with its card, the access token under the v1 Keychain name
``plaid.access_token.production``, card payments, statement credits, a refund and a split, a
renamed and a hidden category, a category of the owner's own, an edited mapping, rules, budgets
and changed settings. On a scratch database at ``0003_f1_verification_fixes`` (the v1 head) this
test seeds exactly that, upgrades to the wave-3 head, and checks that

* the owner's categories, mappings, rules, budgets, settings and transactions are unchanged;
* MONEY's categories and bank mappings exist, and the "Show income" switch is on;
* every page renders (production settings over fakes: in-memory Keychain, fake Plaid);
* the card's figures on the Dashboard, Budgets and Trends are the figures the v1 data gave
  (independent SQL before the upgrade): no card row changed category;
* a sync through the fake gateway moves the v1 token to its per-Item key exactly once, syncs,
  and the owner's mapping and rule still categorize new card rows;

then downgrades to 0003 (the owner's rows survive, wave 3's additions go) and upgrades again.
"""

from __future__ import annotations

import base64
import datetime as dt
import warnings
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.db import current_revision, downgrade_migrations, make_engine, run_migrations
from budgetapp.periods import Period, month_of
from budgetapp.services import budgets, insights, spending
from budgetapp.services import sync as sync_service
from budgetapp.web.app import create_app
from tests.conftest import create_scratch_database, drop_scratch_database
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_category,
    make_item,
    make_rule,
    make_split,
    make_transaction,
)
from tests.fake_plaid import FakeItem, FakePlaidGateway
from tests.test_migrations import BANK_KINDS, BANK_MAPPINGS, HEAD
from tests.web.csp import csp_problems

with warnings.catch_warnings():  # as tests/web/conftest.py: httpx works the same as httpx2
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

D = Decimal
V1 = "0003_f1_verification_fixes"
TODAY = dt.date(2026, 10, 20)
NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
MONTHS = (month_of(dt.date(2026, 8, 1)), month_of(dt.date(2026, 9, 1)), month_of(TODAY))
LINKED = dt.datetime(2026, 1, 10, 18, 0, tzinfo=dt.UTC)

#: What the owner made or chose, which no migration may change. Arrays as text (hashable rows).
OWNER = {
    "categories": "SELECT id, name, kind, system_key, is_hidden, color, icon, sort_order "
    "FROM categories",
    "mappings": "SELECT source, external_key, category_id FROM category_mappings",
    "rules": "SELECT id, name, priority, is_active, merchant_pattern, description_pattern, "
    "match_type, amount_min, amount_max, account_id, category_id, is_system FROM rules",
    "budgets": "SELECT id, scope, category_id, period, amount, rollover, effective_from "
    "FROM budgets",
    "settings": "SELECT budget_period_mode, budget_cycle_account_id, include_pending_in_budgets, "
    "budget_alerts_enabled, alert_thresholds::text, sync_alerts_enabled, large_charge_threshold, "
    "consent_reminder_days, statement_settle_days, ai_enabled FROM app_settings",
}
#: The money: every row with its category, and what v_allocations makes of it.
LEDGER = {
    "transactions": "SELECT id, account_id, source, plaid_transaction_id, date, authorized_date, "
    "posted_date, pending, amount, category_id, category_source, rule_id, duplicate_status, "
    "notes, tags::text, user_edited_at FROM transactions",
    "splits": "SELECT transaction_id, category_id, amount FROM transaction_splits",
    "allocations": "SELECT transaction_id, category_id, amount, spend_date, pending "
    "FROM v_allocations",
}
#: Spending (or income) per calendar month and category, as v1 counted it: v_allocations by
#: spend date, pending left out (the owner turned "include pending" off).
_BY_MONTH = """
SELECT date_trunc('month', v.spend_date)::date, v.category_id, sum(v.amount)
FROM v_allocations v JOIN categories c ON c.id = v.category_id
WHERE c.kind = :kind AND NOT v.pending
GROUP BY 1, 2
"""


def no_sleep(_seconds: float) -> None:
    return None


def fake_cursor(item: FakeItem, position: int) -> str:
    """The cursor ``FakePlaidGateway`` gives after ``position`` events (its own encoding): what
    the v1 code saved after its last sync, so the next sync gets only what is new."""
    return base64.urlsafe_b64encode(f"{item.item_id}:{position}".encode()).decode()


def snapshot(engine: Engine, queries: dict[str, str]) -> dict[str, list[tuple]]:
    with engine.connect() as conn:
        return {
            name: sorted((tuple(row) for row in conn.execute(text(query))), key=repr)
            for name, query in queries.items()
        }


def by_month(engine: Engine, kind: str) -> dict[tuple[dt.date, int], Decimal]:
    with engine.connect() as conn:
        rows = conn.execute(text(_BY_MONTH), {"kind": kind}).all()
    return {(month, category_id): amount for month, category_id, amount in rows if amount}


def in_month(figures: dict[tuple[dt.date, int], Decimal], month: Period) -> dict[int, Decimal]:
    return {cid: amount for (start, cid), amount in figures.items() if start == month.start}


@pytest.fixture
def scratch(settings) -> Iterator[Engine]:
    name = f"{settings.db_name}_w3up"
    create_scratch_database(settings, name)
    engine = make_engine(settings, dbname=name)
    yield engine
    engine.dispose()
    drop_scratch_database(settings, name)


@pytest.fixture
def production(tmp_path):
    """Production settings whose every folder is a temp folder; nothing connects with them (the
    app and the sync get the scratch database's session factory)."""
    return load_settings("production", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")


def seed_v1(session: Session, fake: FakeItem) -> dict[str, int]:
    """What the v1 code left in a customized production database (only rows v1 could write)."""
    session.execute(text("UPDATE categories SET name = 'Food at home' WHERE name = 'Groceries'"))
    session.execute(text("UPDATE categories SET is_hidden = true WHERE name = 'Education'"))
    coffee = make_category(session, "Coffee", color="cat-3", icon="☕")
    session.execute(
        text(
            "UPDATE category_mappings SET category_id = :c "
            "WHERE source = 'plaid_pfc' AND external_key = 'FOOD_AND_DRINK_COFFEE'"
        ),
        {"c": coffee.id},
    )
    session.execute(
        text(
            "UPDATE app_settings SET include_pending_in_budgets = false, "
            "alert_thresholds = '{50,90,100}', large_charge_threshold = 250.00, "
            "consent_reminder_days = 14, statement_settle_days = 4 WHERE id = 1"
        )
    )
    item = make_item(
        session,
        plaid_item_id=fake.item_id,
        linked_at=LINKED,
        last_consented_at=LINKED,
        history_complete_at=LINKED + dt.timedelta(hours=1),
        last_sync_success_at=NOW - dt.timedelta(hours=3),
        liabilities_status="ok",
        transactions_cursor=fake_cursor(fake, 0),
    )
    card = make_account(
        session,
        item,
        plaid_account_id=fake.default_account_id,
        mask="1005",
        current_balance=D("1234.56"),
        balances_as_of=NOW - dt.timedelta(hours=3),
        statement_closing_day=27,
    )
    cat = {name: get_category(session, name) for name in (
        "Food at home", "Dining & Drinks", "Travel", "Shopping", "Fees & Interest",
        "Credits & Rewards", "Card Payments",
    )}  # fmt: skip
    rides = make_rule(session, cat["Travel"], name="Rides", merchant_pattern="UBER")
    beans = make_rule(session, coffee, name="Coffee", merchant_pattern="BLUE BOTTLE")
    start = dt.date(2026, 1, 1)
    make_budget(session, cat["Food at home"], "600.00", effective_from=start)
    make_budget(session, coffee, "80.00", effective_from=start, rollover=True)
    make_budget(session, None, "3000.00", effective_from=start)  # the overall target
    make_budget(session, cat["Travel"], "5000.00", effective_from=start, period="annual")

    def row(day: dt.date, amount: str, category: str, source: str = "plaid", **kw):
        target = coffee if category == "Coffee" else cat[category]
        return make_transaction(session, card, amount=amount, date=day, category=target,
                                category_source=source, **kw)  # fmt: skip

    for n, month in enumerate(MONTHS):
        on = month.start.replace
        row(
            on(day=2),
            f"-{900 + 10 * n}.00",
            "Card Payments",
            "system",
            transaction_code="payment",
            name="PAYMENT RECEIVED - THANK YOU",
            pfc_primary="LOAN_PAYMENTS",
            pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        )
        row(
            on(day=3),
            f"{84 + n}.10",
            "Food at home",
            name="WHOLE FOODS",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
        )
        row(
            on(day=17),
            "52.30",
            "Food at home",
            name="TRADER JOE'S",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
        )
        row(
            on(day=18),
            "-12.40",
            "Food at home",
            transaction_code="refund",
            name="WHOLE FOODS",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
        )
        for day in (5, 12, 19):
            row(
                on(day=day),
                "6.25",
                "Coffee",
                "rule",
                rule_id=beans.id,
                name="BLUE BOTTLE COFFEE",
                merchant_name="Blue Bottle Coffee",
                pfc_primary="FOOD_AND_DRINK",
                pfc_detailed="FOOD_AND_DRINK_COFFEE",
            )
        row(
            on(day=9),
            "48.00",
            "Dining & Drinks",
            name="NOPA",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_RESTAURANT",
        )
        row(on(day=14), "23.40", "Travel", "rule", rule_id=rides.id, name="UBER TRIP",
            merchant_name="Uber", pfc_primary="TRANSPORTATION",
            pfc_detailed="TRANSPORTATION_TAXIS_AND_RIDE_SHARES")  # fmt: skip
    row(
        dt.date(2026, 8, 25),
        "695.00",
        "Fees & Interest",
        transaction_code="membership fee",
        name="PLATINUM CARD MEMBERSHIP FEE",
        pfc_primary="BANK_FEES",
        pfc_detailed="BANK_FEES_OTHER_BANK_FEES",
    )
    row(
        dt.date(2026, 9, 6),
        "-25.00",
        "Credits & Rewards",
        name="AMEX OFFER CREDIT",
        pfc_primary="INCOME",
        pfc_detailed="INCOME_OTHER",
    )
    row(
        dt.date(2026, 10, 7),
        "-15.00",
        "Credits & Rewards",
        name="AMEX OFFER CREDIT",
        pfc_primary="INCOME",
        pfc_detailed="INCOME_OTHER",
    )
    row(dt.date(2026, 10, 19), "64.00", "Food at home", pending=True, name="BLUE APRON")
    costco = row(dt.date(2026, 9, 20), "200.00", "Food at home", "user", name="COSTCO",
                 pfc_primary="GENERAL_MERCHANDISE",
                 pfc_detailed="GENERAL_MERCHANDISE_SUPERSTORES")  # fmt: skip
    make_split(session, costco, cat["Food at home"], "150.00")
    make_split(session, costco, cat["Shopping"], "50.00")
    return {"item": item.id, "card": card.id, "split": costco.id, "food": cat["Food at home"].id}


def pages(ids: dict[str, int]) -> list[str]:
    return [
        "/",
        "/transactions",
        f"/transactions?account={ids['card']}",
        f"/transactions/{ids['split']}",
        "/budgets",
        "/trends",
        f"/trends?category={ids['food']}",
        "/recurring",
        "/statements",
        "/categories",
        "/categories/mappings?source=plaid_pfc",
        "/categories/mappings?source=amex_csv",
        "/rules",
        "/rules/new",
        "/import",
        "/connection",
        "/settings",
    ]


def check_figures(session: Session, spent: dict, income: dict) -> None:
    """The Budgets page, the Dashboard and Trends show what the v1 data gave (``spent`` and
    ``income`` come from independent SQL on the v1 database)."""
    for month in MONTHS:
        expected = in_month(spent, month)
        found = spending.period_spending(session, month)  # Budgets' and the Dashboard's figure
        assert {c.category_id: c.amount for c in found.categories if c.amount} == expected
        assert found.total == sum(expected.values(), D("0.00"))
        earned = spending.period_income(session, month)
        assert earned.total == -sum(in_month(income, month).values(), D("0.00"))
    this_month = in_month(spent, MONTHS[-1])
    overview = budgets.overview(session, MONTHS[-1], today=TODAY)
    assert {line.category.id: line.spent for line in overview.lines} == {
        cid: this_month.get(cid, D("0.00")) for cid in {line.category.id for line in overview.lines}
    }
    travel = get_category(session, "Travel").id  # an annual budget: outside the overall target
    assert overview.overall.spent == sum(this_month.values()) - this_month.get(travel, 0)
    dashboard = insights.dashboard(session, today=TODAY)
    assert dashboard.overview.spending.total == sum(this_month.values())
    assert dashboard.income is not None and dashboard.income.cards_only
    assert dashboard.income.total == D("15.00")  # October's statement credit
    trends = insights.trends(session, today=TODAY)
    for i, trend_month in enumerate(trends.months):
        month = trend_month.period
        if month.start < MONTHS[0].start:
            continue
        expected = in_month(spent, month)
        assert trends.totals.amounts[i] == sum(expected.values(), D("0.00"))
        for line in trends.categories:
            assert (line.amounts[i] or D("0.00")) == expected.get(line.category.id, D("0.00"))


def test_the_owners_v1_install_upgrades_to_wave_3(
    scratch, production, secrets, notifier, freeze_today, monkeypatch
):
    freeze_today("2026-10-20", now=NOW)
    factory = sessionmaker(bind=scratch, expire_on_commit=False)
    plaid = FakePlaidGateway(plaid_env="production")
    fake = plaid.add_item(item_id="item-amex")  # American Express (ins_10) with one card

    # v1: the database at its last v1 revision, the token under the v1 Keychain name.
    run_migrations(scratch, V1)
    with factory() as session:
        ids = seed_v1(session, fake)
        session.commit()
    secrets.set(app_secrets.LEGACY_PRODUCTION_TOKEN, fake.access_token)
    owner, ledger = snapshot(scratch, OWNER), snapshot(scratch, LEDGER)
    spent, income = by_month(scratch, "expense"), by_month(scratch, "income")
    assert len(ledger["transactions"]) == 3 * 9 + 5 and spent and income

    # The upgrade: 0005_bank_categories, then 0006_income_preference.
    run_migrations(scratch)
    assert current_revision(scratch) == HEAD
    upgraded = snapshot(scratch, OWNER)
    for name in ("rules", "budgets", "settings"):
        assert upgraded[name] == owner[name], name
    assert set(owner["categories"]) <= set(upgraded["categories"])
    assert set(owner["mappings"]) <= set(upgraded["mappings"])
    added = set(upgraded["categories"]) - set(owner["categories"])
    assert {row[1]: row[2] for row in added} == BANK_KINDS
    bank_keys = [m for m in set(upgraded["mappings"]) - set(owner["mappings"])]
    assert len(bank_keys) == BANK_MAPPINGS and all(
        key.endswith("|bank") for _s, key, _c in bank_keys
    )
    assert snapshot(scratch, LEDGER) == ledger  # no row changed category; same allocations
    assert by_month(scratch, "expense") == spent and by_month(scratch, "income") == income
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT show_income FROM app_settings")).scalar() is True

    # Every page renders, as the production web agent will serve it (fakes only).
    app = create_app(production, session_factory=factory, gateway_factory=lambda _s: plaid,
                     secret_store=secrets, notifier=notifier)  # fmt: skip
    with TestClient(app, base_url="http://127.0.0.1:8824") as client:
        for path in pages(ids):
            response = client.get(path)
            assert response.status_code == 200, path
            assert csp_problems(response.text, rendered=True) == [], path
        with factory() as session:
            check_figures(session, spent, income)

        # The first sync moves the v1 token to the Item's own key, once, and syncs with it.
        moves: list[app_secrets.LegacyTokenMove] = []
        move = app_secrets.move_legacy_production_token

        def counted(*args, **kwargs):
            moves.append(move(*args, **kwargs))
            return moves[-1]

        monkeypatch.setattr(app_secrets, "move_legacy_production_token", counted)
        plaid.add_transaction(
            fake,
            transaction_id="new-groceries",
            amount="45.10",
            date=TODAY - dt.timedelta(days=1),
            name="TRADER JOE'S",
            merchant_name="Trader Joe's",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
        )
        plaid.add_transaction(
            fake,
            transaction_id="new-coffee",
            amount="6.25",
            date=TODAY,
            name="BLUE BOTTLE COFFEE",
            merchant_name="Blue Bottle Coffee",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
        )
        for _ in range(2):
            outcome = sync_service.sync_item(factory, plaid, secrets, production, ids["item"],
                                             trigger="schedule", notifier=notifier,
                                             sleep=no_sleep)  # fmt: skip
            assert outcome.status == "success"
        assert [m.status for m in moves] == ["moved"]
        assert secrets.values == {
            app_secrets.PRODUCTION_ITEM_INDEX: '["item-amex"]',
            "plaid.access_token.production.item-amex": fake.access_token,
        }
        assert {c["access_token"] for c in plaid.calls_to("transactions_sync")} == {
            fake.access_token
        }
        assert plaid.calls_to("transactions_sync")[0]["cursor"] == fake_cursor(fake, 0)
        connection = client.get("/connection")
        assert "plaid.access_token.production.item-amex" in connection.text

    # Old rows untouched; the new ones follow the owner's mapping (renamed category) and rule.
    synced = snapshot(scratch, LEDGER)
    assert set(ledger["transactions"]) < set(synced["transactions"])
    with scratch.connect() as conn:
        new = dict(
            conn.execute(
                text(
                    "SELECT t.plaid_transaction_id, c.name FROM transactions t "
                    "JOIN categories c ON c.id = t.category_id "
                    "WHERE t.plaid_transaction_id IN ('new-groceries', 'new-coffee')"
                )
            ).all()
        )
    assert new == {"new-groceries": "Food at home", "new-coffee": "Coffee"}
    october = in_month(by_month(scratch, "expense"), MONTHS[-1])
    expected = dict(in_month(spent, MONTHS[-1]))
    expected[ids["food"]] += D("45.10")
    coffee_id = next(r[0] for r in owner["categories"] if r[1] == "Coffee")
    expected[coffee_id] += D("6.25")
    assert october == expected

    # Back to 0003 (the owner's rows stay, wave 3's additions go), then up again.
    downgrade_migrations(scratch, V1)
    assert current_revision(scratch) == V1
    assert snapshot(scratch, OWNER) == owner
    assert snapshot(scratch, LEDGER) == synced
    run_migrations(scratch)
    assert current_revision(scratch) == HEAD
    again = snapshot(scratch, OWNER)
    assert {row[1]: row[2] for row in set(again["categories"]) - set(owner["categories"])} == (
        BANK_KINDS
    )
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT show_income FROM app_settings")).scalar() is True
