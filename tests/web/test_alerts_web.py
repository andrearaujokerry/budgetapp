"""Budget alerts (ALR-1) after edits that change categories in the web app (ARCHITECTURE §11).

The page routes call ``alerts.evaluate_budgets`` before they commit; the notification goes out when
the request's transaction commits (the app here uses the recording notifier).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from budgetapp.models import AlertEvent
from tests.factories import get_category, make_account, make_budget, make_rule, make_transaction
from tests.web.conftest import HTMX

TODAY = dt.date(2026, 11, 12)


@pytest.fixture(autouse=True)
def today(freeze_today):
    return freeze_today(TODAY)


@pytest.fixture
def card(db):
    return make_account(db)


@pytest.fixture
def dining(db):
    category = get_category(db, "Dining & Drinks")
    make_budget(db, category, "100.00")
    return category


def budget_keys(db) -> list[str]:
    db.expire_all()
    return sorted(db.scalars(select(AlertEvent.dedupe_key).where(AlertEvent.kind == "budget")))


def test_moving_a_charge_into_a_budget_alerts_and_moving_it_out_re_arms(
    db, client, notifier, card, dining
):
    shopping = get_category(db, "Shopping")
    txn = make_transaction(db, card, amount="90.00", date=TODAY, category=shopping)
    db.commit()
    url = f"/transactions/{txn.id}/category"
    assert client.post(url, data={"category_id": str(dining.id)}, headers=HTMX).status_code == 200
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]
    assert budget_keys(db) == [f"budget:{dining.id}:2026-11-01:80"]
    client.post(url, data={"category_id": str(shopping.id)}, headers=HTMX)
    assert budget_keys(db) == []
    client.post(url, data={"category_id": str(dining.id)}, headers=HTMX)
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"] * 2


def test_re_applying_rules_alerts_but_a_preview_does_not(db, client, notifier, card, dining):
    make_rule(db, dining, merchant_pattern="TARTINE")
    make_transaction(db, card, amount="95.00", date=TODAY, name="TARTINE BAKERY")
    db.commit()
    client.post("/rules/reapply", data={"scope": "all", "action": "preview"}, headers=HTMX)
    assert notifier.sent == [] and budget_keys(db) == []
    response = client.post("/rules/reapply", data={"scope": "all", "action": "apply"})
    assert response.status_code == 200
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]


def test_merging_categories_alerts(db, client, notifier, card, dining):
    snacks = get_category(db, "Other")
    make_transaction(db, card, amount="120.00", date=TODAY, category=snacks)
    db.commit()
    response = client.post(f"/categories/{snacks.id}/merge", data={"target_id": dining.id})
    assert response.status_code == 200
    assert notifier.titles == ["Dining & Drinks: over budget"]


def test_a_refused_merge_sends_nothing(db, client, notifier, card, dining):
    other = get_category(db, "Other")
    make_transaction(db, card, amount="120.00", date=TODAY, category=other)
    db.commit()
    client.post(f"/categories/{other.id}/merge", data={"target_id": ""})
    assert notifier.sent == [] and budget_keys(db) == []


def rule_form(category, *, apply: bool) -> dict[str, str]:
    form = {
        "merchant_pattern": "TARTINE",
        "match_type": "contains",
        "category_id": str(category.id),
        "is_active": "1",
        "priority": "1",
    }
    if apply:
        form["apply_to_existing"] = "1"
    return form


def test_a_rule_saved_with_apply_to_existing_alerts(db, client, notifier, card, dining):
    """The Rules page form (pre-filled by a transaction's "make a rule" link) moves past charges."""
    shopping = get_category(db, "Shopping")
    make_transaction(db, card, amount="95.00", date=TODAY, name="TARTINE BAKERY", category=shopping)
    db.commit()
    assert client.post("/rules", data=rule_form(dining, apply=False)).status_code == 200
    assert notifier.sent == [] and budget_keys(db) == []
    assert client.post("/rules", data=rule_form(dining, apply=True)).status_code == 200
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]
    assert budget_keys(db) == [f"budget:{dining.id}:2026-11-01:80"]


def test_editing_a_rule_with_apply_to_existing_alerts_and_re_arms(
    db, client, notifier, card, dining
):
    shopping = get_category(db, "Shopping")
    rule = make_rule(db, dining, merchant_pattern="TARTINE")
    make_transaction(db, card, amount="95.00", date=TODAY, name="TARTINE BAKERY", category=shopping)
    db.commit()
    client.post("/rules/reapply", data={"scope": "all", "action": "apply"})
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"]
    response = client.post(f"/rules/{rule.id}", data=rule_form(shopping, apply=True))
    assert response.status_code == 200
    assert budget_keys(db) == []  # the charge left Dining: the 80% alert can fire again
    client.post(f"/rules/{rule.id}", data=rule_form(dining, apply=True))
    assert notifier.titles == ["Dining & Drinks: 80% of budget used"] * 2
