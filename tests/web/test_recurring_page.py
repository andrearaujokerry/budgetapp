"""Recurring page (INS-3): subscriptions, their monthly cost and next date, and overrides."""

from __future__ import annotations

import datetime as dt
import html
import re

import pytest
from sqlalchemy import select

from budgetapp import periods
from budgetapp.models import RecurringOverride
from tests.factories import get_category, make_account, make_recurring_override, make_transaction
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

D = dt.date
TODAY = D(2026, 10, 29)


def toasts(page: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', page)]


def text_of(page: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


@pytest.fixture
def today(freeze_today):
    return freeze_today(TODAY)


@pytest.fixture
def subscriptions(db, today):
    """Netflix (monthly), Spotify (monthly, price change) and Hulu (lapsed)."""
    card = make_account(db)
    streaming = get_category(db, "Subscriptions & Streaming")
    for i in range(12):
        year, month = periods.add_months(2025, 11, i)
        make_transaction(
            db,
            card,
            amount="15.49",
            date=D(year, month, 5),
            merchant_name="Netflix",
            name="NETFLIX.COM",
            category=streaming,
        )
        make_transaction(
            db,
            card,
            amount="10.99" if i < 6 else "11.99",
            date=D(year, month, 12),
            name="SPOTIFY USA 8777787",
            category=streaming,
        )
        if i < 8:
            make_transaction(db, card, amount="7.99", date=D(year, month, 15), name="HULU 877")
    return card


def override(client, key: str, status: str, name: str = "Netflix", **kwargs):
    return client.post(
        "/recurring/override", data={"key": key, "status": status, "name": name}, **kwargs
    )


class TestPage:
    def test_an_empty_database(self, client):
        response = client.get("/recurring")
        assert response.status_code == 200
        assert response.template.name == "recurring/index.html"
        assert "No recurring charges found yet" in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_lists_active_and_lapsed_series(self, client, subscriptions):
        response = client.get("/recurring")
        assert response.status_code == 200
        overview = response.context["overview"]
        assert [s.merchant for s in overview.active] == ["Netflix", "SPOTIFY USA 8777787"]
        assert [s.merchant for s in overview.lapsed] == ["HULU 877"]
        page = response.text
        assert 'href="/transactions?q=Netflix"' in page
        assert 'href="/transactions?q=SPOTIFY%20USA"' in page
        text = text_of(page)
        assert "Monthly cost $27 About $330 a year · 2 active charges" in text
        assert "Next charge $15.49 Netflix · Nov 5" in text
        assert "Netflix Monthly · next Nov 5" in text
        assert "was $10.99" in text
        assert "Total $27.48" in text
        assert "Lapsed" in text and "HULU 877" in text
        assert csp_problems(page, rendered=True) == []

    def test_htmx_gets_the_content_only(self, client, subscriptions):
        response = client.get("/recurring", headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "recurring/_content.html"
        assert response.text.lstrip().startswith('<div id="recurring-content"')
        assert "<html" not in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_every_row_has_labelled_actions(self, client, subscriptions):
        page = client.get("/recurring").text
        assert 'aria-label="Ignore it: Netflix"' in page
        assert 'aria-label="Always list it: Netflix"' in page
        forms = re.findall(r'<form class="rec-actions"[^>]*>', page)
        assert len(forms) == 3
        assert all('method="post" action="/recurring/override"' in f for f in forms)
        assert all('hx-post="/recurring/override"' in f for f in forms)


class TestOverrides:
    def test_ignore_with_htmx_swaps_the_content_and_toasts(self, client, db, subscriptions):
        response = override(client, "netflix", "ignored", headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "recurring/_content.html"
        assert toasts(response.text) == ["Ignored: Netflix won't be listed as recurring."]
        assert 'hx-swap-oob="beforeend:#flash"' in response.text
        overview = response.context["overview"]
        assert [s.merchant for s in overview.ignored] == ["Netflix"]
        assert "Netflix" not in [s.merchant for s in overview.active]
        assert overview.monthly_total == overview.active[0].monthly_cost
        row = db.scalars(select(RecurringOverride)).one()
        assert (row.merchant_key, row.status) == ("netflix", "ignored")
        assert csp_problems(response.text, rendered=True) == []

    def test_a_plain_post_redirects_back(self, client, db, subscriptions):
        response = override(client, "netflix", "ignored", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/recurring"
        page = client.get("/recurring").text
        assert toasts(page) == ["Ignored: Netflix won't be listed as recurring."]
        assert 'id="ignored-title"' in page

    def test_keep_and_back_to_automatic(self, client, db, today):
        card = make_account(db)
        for day in (D(2026, 9, 3), D(2026, 10, 3)):
            make_transaction(db, card, amount="5.00", date=day, name="PATREON")
        assert client.get("/recurring").context["overview"].is_empty
        response = override(client, "patreon", "confirmed", name="PATREON", headers=HTMX)
        assert toasts(response.text) == ["Kept: PATREON stays on the list."]
        active = response.context["overview"].active
        assert [(s.merchant, s.detected) for s in active] == [("PATREON", False)]
        assert "kept by you" in response.text
        response = override(client, "patreon", "reset", name="PATREON", headers=HTMX)
        assert toasts(response.text) == ["PATREON is back to automatic detection."]
        assert response.context["overview"].is_empty
        assert db.scalars(select(RecurringOverride)).all() == []

    def test_restore_an_ignored_series(self, client, db, subscriptions):
        make_recurring_override(db, "netflix", "ignored")
        page = client.get("/recurring").text
        assert 'aria-label="Restore: Netflix"' in page
        response = override(client, "netflix", "reset", headers=HTMX)
        assert "Netflix" in [s.merchant for s in response.context["overview"].active]

    @pytest.mark.parametrize(
        ("key", "status"), [("netflix", "delete"), ("netflix", ""), ("", "ignored")]
    )
    def test_bad_requests(self, client, db, subscriptions, key, status):
        response = override(client, key, status)
        assert response.status_code == 400
        assert db.scalars(select(RecurringOverride)).all() == []
