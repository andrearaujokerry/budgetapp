"""PERF-1 for the Dashboard, Budgets and Trends: under a second with five years of transactions.

The budgets are the heaviest a real setup gets: every spending category has a monthly budget
with rollover from the start of the five-year history (so each page folds a five-year rollover
chain per category), plus an overall target, in calendar and in statement-cycle mode. Measured
on the development Mac: the dashboard about 0.25 s and Budgets under 0.1 s (a chain's spending is
one query: ``spending.periods_spending``), Trends under 0.1 s.

Reuses the Transactions page's five-year data set and timing helper (``tests/test_perf.py``):
about 18,000 transactions, analyzed, each URL warmed up once and then timed three times
(median).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from budgetapp.models import Account
from tests.factories import get_category, make_budget, make_statement, set_app_settings
from tests.test_perf import (
    MERCHANTS,
    TODAY,
    five_years_of_transactions,
    median_seconds,
    perf_client,
)

__all__ = ["perf_client"]  # the fixture, imported so pytest finds it here

HTMX = {"HX-Request": "true"}
#: The first month the five-year history covers in full.
BUDGETS_FROM = dt.date(2021, 11, 1)


@pytest.mark.parametrize("mode", ["calendar", "statement"])
def test_dashboard_budgets_and_trends_with_five_years_of_data(
    db: Session, perf_client: TestClient, freeze_today: Any, mode: str
) -> None:
    freeze_today(TODAY.isoformat())
    data = five_years_of_transactions(db)
    assert data["count"] > 18_000
    for name in sorted({category for _, _, category, _ in MERCHANTS}):
        make_budget(
            db, get_category(db, name), "500.00", effective_from=BUDGETS_FROM, rollover=True
        )
    make_budget(db, None, "6000.00", effective_from=BUDGETS_FROM)
    account = db.scalars(select(Account)).one()
    if mode == "statement":
        month = BUDGETS_FROM
        while month < TODAY:
            make_statement(db, account, month.replace(day=20))
            month = (month + dt.timedelta(days=32)).replace(day=1)
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
    groceries = data["groceries"]
    pages = {
        "dashboard": ("/", None),
        "budgets": ("/budgets", None),
        "trends": ("/trends", None),
        "trends, 36 months (HTMX)": ("/trends?months=36", HTMX),
        "trends, one category": (f"/trends?category={groceries}", None),
    }
    timings = {}
    for label, (url, headers) in pages.items():
        timings[label], response = median_seconds(perf_client, url, headers)
        assert response.status_code == 200
    assert "budget-carry" in perf_client.get("/budgets").text  # the chains carry money
    slow = {label: round(t, 3) for label, t in timings.items() if t >= 1.0}
    assert not slow, f"over a second: {slow} (all: {timings})"
