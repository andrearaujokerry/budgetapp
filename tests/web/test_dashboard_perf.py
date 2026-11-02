"""PERF-1 for the Dashboard and Trends: under a second with five years of transactions.

Measured on the development Mac: the dashboard about 0.75 s (0.55 s of it is
``budgets.overview`` walking a rollover chain back to 2022), Trends under 0.1 s.

Reuses the Transactions page's five-year data set and timing helper (``tests/test_perf.py``):
about 18,000 transactions, analyzed, each URL warmed up once and then timed three times
(median).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from tests.factories import get_category, make_budget
from tests.test_perf import TODAY, five_years_of_transactions, median_seconds, perf_client

__all__ = ["perf_client"]  # the fixture, imported so pytest finds it here

HTMX = {"HX-Request": "true"}


def test_dashboard_and_trends_with_five_years_of_data(
    db: Session, perf_client: TestClient, freeze_today: Any
) -> None:
    freeze_today(TODAY.isoformat())
    data = five_years_of_transactions(db)
    assert data["count"] > 18_000
    groceries = get_category(db, "Groceries")
    make_budget(db, groceries, "600.00", effective_from=dt.date(2022, 1, 1), rollover=True)
    make_budget(db, None, "4000.00", effective_from=dt.date(2022, 1, 1))
    pages = {
        "dashboard": ("/", None),
        "trends": ("/trends", None),
        "trends, 36 months (HTMX)": ("/trends?months=36", HTMX),
        "trends, one category": (f"/trends?category={groceries.id}", None),
    }
    timings = {}
    for label, (url, headers) in pages.items():
        timings[label], response = median_seconds(perf_client, url, headers)
        assert response.status_code == 200
    slow = {label: round(t, 3) for label, t in timings.items() if t >= 1.0}
    assert not slow, f"over a second: {slow} (all: {timings})"
