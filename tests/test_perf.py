"""PERF-1: the transactions page and its CSV export answer in under a second with 5 years of data.

About 18,000 transactions (five years at ten a day, a busy card) are bulk-inserted into the test's
transaction, with the shapes the page has to handle: Plaid and CSV eras, pending rows, refunds,
monthly card payments, splits, tags, notes and two card members. The tables are then analyzed, as
autovacuum would do in the real database. Each URL is requested once to warm up (template
compilation), then timed three times through the TestClient; the median must stay under one
second, so a burst of load from other processes on the Mac doesn't decide the result.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
import statistics
import time
import warnings
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from budgetapp.config import Settings
from budgetapp.models import Transaction, TransactionSplit
from budgetapp.secrets import MemoryStore
from budgetapp.services.notify import RecordingNotifier
from budgetapp.web.app import create_app
from tests.factories import card_payments, get_category, make_account
from tests.fake_plaid import FakePlaidGateway

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from starlette.testclient import TestClient

pytestmark = pytest.mark.slow

TODAY = dt.date(2026, 10, 29)
YEARS = 5
PER_DAY = 10
LIMIT_SECONDS = 1.0
HTMX = {"HX-Request": "true"}

#: (merchant, statement description, category, (lowest, highest) charge in dollars)
MERCHANTS = (
    ("Blue Bottle Coffee", "BLUE BOTTLE COFFEE", "Dining & Drinks", (4, 12)),
    ("Philz Coffee", "PHILZ COFFEE", "Dining & Drinks", (4, 10)),
    ("Delfina", "SQ *DELFINA", "Dining & Drinks", (40, 160)),
    ("Whole Foods Market", "WHOLEFDS MKT", "Groceries", (20, 220)),
    ("Trader Joe's", "TRADER JOE S", "Groceries", (15, 120)),
    ("Safeway", "SAFEWAY", "Groceries", (10, 150)),
    ("Uber", "UBER *TRIP", "Transportation", (8, 60)),
    ("Lyft", "LYFT *RIDE", "Transportation", (8, 50)),
    ("Shell", "SHELL OIL", "Transportation", (30, 90)),
    ("Amazon", "AMZN MKTP US", "Shopping", (10, 300)),
    ("Target", "TARGET", "Shopping", (10, 200)),
    ("Uniqlo", "UNIQLO", "Shopping", (20, 150)),
    ("Netflix", "NETFLIX.COM", "Subscriptions & Streaming", (15, 23)),
    ("Spotify", "SPOTIFY USA", "Subscriptions & Streaming", (11, 17)),
    ("PG&E", "PGANDE WEB ONLINE", "Utilities & Bills", (60, 260)),
    ("Comcast", "COMCAST CABLE", "Utilities & Bills", (70, 110)),
    ("AMC Theatres", "AMC METREON", "Entertainment", (12, 60)),
    ("Equinox", "EQUINOX", "Health & Fitness", (200, 260)),
    ("Walgreens", "WALGREENS", "Health & Fitness", (5, 60)),
    ("Delta Air Lines", "DELTA AIR LINES", "Travel", (150, 900)),
    ("Marriott", "MARRIOTT", "Travel", (180, 700)),
    ("Sephora", "SEPHORA", "Personal Care", (20, 150)),
    ("IKEA", "IKEA", "Home", (30, 400)),
)


def _cents(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low * 100, high * 100)).scaleb(-2)


def five_years_of_transactions(db: Session) -> dict[str, Any]:
    """Bulk-insert ~18,000 rows ending at TODAY; returns what the test needs to know."""
    rng = random.Random(1)
    account = make_account(db)
    category_ids = {name: get_category(db, name).id for _, _, name, _ in MERCHANTS}
    payments_id = card_payments(db).id
    start = TODAY - dt.timedelta(days=365 * YEARS)
    plaid_era = TODAY - dt.timedelta(days=730)
    rows: list[dict[str, Any]] = []
    day = start
    n = 0
    while day <= TODAY:
        for _ in range(PER_DAY):
            n += 1
            merchant, description, category, (low, high) = MERCHANTS[rng.randrange(len(MERCHANTS))]
            amount = _cents(rng, low, high)
            if n % 25 == 0:
                amount = -amount  # a refund
            name = f"{description} {rng.randint(100, 9999)} SAN FRANCISCO CA"
            category_id = category_ids[category]
            if day.day == 5 and n % PER_DAY == 0:  # the monthly card payment
                merchant, name, amount = None, "AUTOPAY PAYMENT - THANK YOU", Decimal("-2500.00")
                category_id = payments_id
            plaid = day >= plaid_era  # older rows came from Amex CSV files
            pending = plaid and (TODAY - day).days < 2
            rows.append(
                {
                    "account_id": account.id,
                    "source": "plaid" if plaid else "csv",
                    "plaid_transaction_id": f"perf-{n}" if plaid else None,
                    "csv_reference": None if plaid else f"32{n:015d}",
                    "pending": pending,
                    "date": day,
                    "authorized_date": day - dt.timedelta(days=rng.randint(0, 2))
                    if plaid
                    else None,
                    "posted_date": None if pending else day,
                    "name": name,
                    "merchant_name": merchant,
                    "original_description": name,
                    "amount": amount,
                    "iso_currency_code": "USD",
                    "category_id": category_id,
                    "category_source": (
                        "system" if category_id == payments_id else ("plaid" if plaid else "amex")
                    ),
                    "tags": ["work"] if n % 15 == 0 else (["travel", "nyc"] if n % 40 == 0 else []),
                    "notes": "Dinner with Sam" if n % 50 == 0 else None,
                    "account_owner": "SAM RIVERA" if n % 3 == 0 else "ALEX KERRY",
                }
            )
        day += dt.timedelta(days=1)
    db.execute(insert(Transaction.__table__), rows)

    # Split every 60th posted charge between its category and Home.
    home = get_category(db, "Home").id
    to_split = db.execute(
        select(Transaction.id, Transaction.amount, Transaction.category_id).where(
            Transaction.account_id == account.id,
            Transaction.amount > 20,
            Transaction.pending.is_(False),
            Transaction.id % 60 == 0,
        )
    ).all()
    splits = []
    for r in to_split:
        half = (r.amount / 2).quantize(Decimal("0.01"))
        splits.append({"transaction_id": r.id, "category_id": r.category_id, "amount": half})
        splits.append({"transaction_id": r.id, "category_id": home, "amount": r.amount - half})
    db.execute(insert(TransactionSplit.__table__), splits)
    db.execute(text("ANALYZE transactions, transaction_splits"))
    return {"count": len(rows), "groceries": category_ids["Groceries"], "splits": len(to_split)}


@pytest.fixture
def perf_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    fake_plaid: FakePlaidGateway,
    notifier: RecordingNotifier,
    secrets: MemoryStore,
) -> Iterator[TestClient]:
    """The web app on the test connection (as ``tests/web/conftest.py`` builds it)."""
    app = create_app(
        settings,
        session_factory=session_factory,
        gateway_factory=lambda _settings: fake_plaid,
        notifier=notifier,
        secret_store=secrets,
    )
    url = app.state.settings.app_url
    with TestClient(app, base_url=url, headers={"Origin": url}) as client:
        yield client


def median_seconds(client: TestClient, url: str, headers: dict[str, str] | None = None) -> Any:
    client.get(url, headers=headers)  # warm-up: compiles the templates once, as a server would
    times = []
    response = None
    for _ in range(3):
        started = time.perf_counter()
        response = client.get(url, headers=headers)
        times.append(time.perf_counter() - started)
        assert response.status_code == 200, url
    return statistics.median(times), response


def test_transactions_page_and_export_with_five_years_of_data(
    db: Session, perf_client: TestClient, freeze_today: Any
) -> None:
    freeze_today(TODAY.isoformat())
    data = five_years_of_transactions(db)
    assert data["count"] > 18_000 and data["splits"] > 100

    pages = {
        "first page": ("/transactions", None),
        "first page (HTMX)": ("/transactions", HTMX),
        "text search": ("/transactions?q=coffee", None),
        "text search (HTMX)": ("/transactions?q=coffee", HTMX),
        "category filter": (f"/transactions?category={data['groceries']}", None),
        "deep page, by amount": ("/transactions?sort=amount&page=300", None),
    }
    timings: dict[str, float] = {}
    results = {}
    for label, (url, headers) in pages.items():
        timings[label], results[label] = median_seconds(perf_client, url, headers)

    first = results["first page"].context["page"]
    assert first.total_count == data["count"]
    assert len(first.rows) == 50 and first.pages == -(-data["count"] // 50)
    search = results["text search"].context["page"]
    assert 0 < search.total_count < data["count"]
    assert all("coffee" in (r.merchant_name or r.name).lower() for r in search.rows)
    groceries = results["category filter"].context["page"]
    assert 0 < groceries.total_count < data["count"]
    assert results["deep page, by amount"].context["page"].page == 300

    timings["CSV export, one year"], export = median_seconds(
        perf_client, "/transactions/export.csv?range=custom&from=2025-01-01&to=2025-12-31"
    )
    lines = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert lines[0][0] == "Date" and 3_600 < len(lines) - 1 < 3_700
    assert all(line[0].startswith("2025-") for line in lines[1:])

    report = ", ".join(f"{label} {seconds * 1000:.0f} ms" for label, seconds in timings.items())
    print(f"\nPERF-1 ({data['count']:,} transactions): {report}")
    slow = {label: seconds for label, seconds in timings.items() if seconds >= LIMIT_SECONDS}
    assert not slow, f"over {LIMIT_SECONDS:.0f} s: {report}"
