"""The Dashboard (INS-1): the Budgets page's figures for the current period, top categories and
merchants, days left, the pace chart, and every empty or partial state."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from budgetapp.services import budgets
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_category,
    make_item,
    make_statement,
    make_transaction,
    set_app_settings,
)
from tests.web.csp import csp_problems

D = Decimal


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


@pytest.fixture(autouse=True)
def october(freeze_today):
    freeze_today("2026-10-15")


@pytest.fixture
def account(db):
    return make_account(db)


@pytest.fixture
def cat(db):
    return lambda name: get_category(db, name)


@pytest.fixture
def spend(db, account):
    def make(category, amount, date, **kwargs):
        return make_transaction(db, account, amount=amount, category=category, date=date, **kwargs)

    return make


@pytest.fixture
def groceries(db, cat, spend):
    """Groceries: $500 a month, $250 spent in October (a $50 refund included)."""
    groceries = cat("Groceries")
    make_budget(db, groceries, "500.00", effective_from=day("2026-01-01"))
    spend(groceries, "300.00", "2026-10-03", merchant_name="Whole Foods")
    spend(groceries, "-50.00", "2026-10-04", merchant_name="Whole Foods")
    return groceries


def text_of(fragment: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(plain).split())


def block(page: str, element_id: str) -> str:
    """The element with this id and everything up to the next card/tile (good enough for the
    text checks below)."""
    match = re.search(
        rf'id="{element_id}".*?(?=<(?:section|div) class="(?:card|stat)\b|$)', page, re.S
    )
    assert match, f"no #{element_id}"
    return match.group(0)


def kpis(page: str) -> str:
    """The headline tiles' text."""
    match = re.search(r'id="dash-kpis".*?(?=<div class="grid grid--2 dash-grid">)', page, re.S)
    assert match, "no tiles"
    return text_of(match.group(0))


def card(page: str, element_id: str) -> str:
    """A whole card (``<section class="card" id=…>`` … ``</section>``)."""
    match = re.search(rf'<section class="card[^"]*"[^>]*id="{element_id}".*?</section>', page, re.S)
    assert match, f"no card #{element_id}"
    return match.group(0)


def links(fragment: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', fragment)]


def chart_config(page: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="dash-pace-chart-config">(.*?)</script>', page, re.S
    )
    assert match
    return json.loads(match.group(1))


# --------------------------------------------------------------------------- empty states


class TestEmpty:
    def test_nothing_linked_or_imported(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert response.template.name == "dashboard/index.html"
        page = response.text
        assert "Nothing to show yet" in page
        assert 'href="/connection"' in page and 'href="/import"' in page
        assert 'id="dash-kpis"' not in page and "data-chart" not in page
        assert "/static/vendor/chart.umd.min.js" not in page  # no chart, no Chart.js
        assert page.count('<h1 class="page-header__title">') == 1
        assert csp_problems(page, rendered=True) == []

    def test_linked_but_the_first_transactions_havent_arrived(self, client, db):
        make_item(db)
        page = client.get("/").text
        assert "Your transactions are on their way" in page
        assert "Nothing to show yet" not in page

    def test_first_day_of_a_month_with_nothing_spent_yet(self, client, freeze_today, cat, spend):
        freeze_today("2026-11-01")
        spend(cat("Home"), "40.00", "2026-10-20")
        page = client.get("/").text
        assert "November 2026 · 30 days left" in text_of(page)
        assert "No spending yet this month" in page
        assert "No merchants yet" in page
        tiles = kpis(page)
        assert "Spent $0 in all categories" in tiles
        assert "Days left 30 of 30 · ends Nov 30" in tiles
        assert "Compared with October $0 all spending: $0 so far, $0 by Oct 1" in tiles
        config = chart_config(page)
        assert config["data"]["labels"][:2] == ["Start", "Nov 1"]
        this, last = config["data"]["datasets"]
        assert this["data"][:3] == ["0.00", "0.00", None] and last["label"] == "October"
        assert csp_problems(page, rendered=True) == []

    def test_the_first_month_of_history_has_nothing_to_compare(self, client, cat, spend):
        spend(cat("Home"), "40.00", "2026-10-02")
        page = client.get("/").text
        tile = text_of(block(page, "kpi-compare"))
        assert "Compared with September — no spending history before this month" in tile
        datasets = chart_config(page)["data"]["datasets"]
        assert [d["label"] for d in datasets] == ["This month"]  # no line for September


# --------------------------------------------------------------------------- the figures


class TestFigures:
    def test_the_same_overview_as_the_budgets_page(self, client, db, groceries, cat, spend):
        make_budget(db, None, "2000.00", effective_from=day("2026-01-01"))
        travel = cat("Travel")
        make_budget(db, travel, "3000.00", effective_from=day("2026-01-01"), period="annual")
        spend(travel, "400.00", "2026-10-08")
        spend(cat("Home"), "75.00", "2026-10-09")
        dash = client.get("/")
        page = client.get("/budgets")
        assert dash.context["ov"] == page.context["ov"]
        assert dash.context["d"].annual == page.context["annual"]
        tiles = kpis(dash.text)
        # Target $2,000 compared with every category except the annual one: 250 + 75.
        assert "Spent $325 of $2,000 target" in tiles
        assert "Remaining $1,675 about $99 a day for 17 days" in tiles
        assert "Days left 17 of 31 · ends Oct 31" in tiles
        budgets_text = text_of(card(dash.text, "dash-budgets"))
        assert "Groceries $250.00 left $250.00 of $500.00 50%" in budgets_text
        assert "$75.00 spent without a budget (counted in the target)" in budgets_text
        assert "$400.00 in categories with an annual budget" in budgets_text
        annual = text_of(card(dash.text, "dash-annual"))
        assert "Annual budgets · 2026" in annual
        assert "Travel $2,600.00 left $400.00 of $3,000.00 13%" in annual

    def test_without_any_budget(self, client, cat, spend):
        spend(cat("Home"), "75.00", "2026-10-09")
        page = client.get("/").text
        tiles = kpis(page)
        assert "Spent $75 in all categories" in tiles
        assert "Remaining — no budget set yet" in tiles
        assert '<a class="stat" href="/budgets" id="kpi-remaining">' in page
        assert "No monthly budgets yet" in page
        assert "No annual budgets" in page

    def test_over_budget(self, client, db, cat, spend):
        dining = cat("Dining & Drinks")
        make_budget(db, dining, "100.00", effective_from=day("2026-01-01"))
        spend(dining, "130.00", "2026-10-02")
        page = client.get("/").text
        tiles = kpis(page)
        assert "Remaining −$30 over budget" in tiles
        assert 'class="stat stat--over"' in page
        line = text_of(block(page, f"dash-line-{dining.id}"))
        assert "Dining & Drinks $30.00 over $130.00 of $100.00 130%" in line

    def test_a_zero_budget_has_no_percentage(self, client, db, cat, spend):
        home = cat("Home")
        make_budget(db, home, "0.00", effective_from=day("2026-01-01"))
        spend(home, "12.00", "2026-10-02")
        line = text_of(block(client.get("/").text, f"dash-line-{home.id}"))
        assert "$12.00 of $0.00" in line and "Over by $12.00" in line

    def test_rollover_shows_what_carried_in(self, client, db, cat, spend):
        care = cat("Personal Care")
        make_budget(db, care, "100.00", effective_from=day("2026-09-01"), rollover=True)
        spend(care, "60.00", "2026-09-12")
        line = text_of(block(client.get("/").text, f"dash-line-{care.id}"))
        assert "$140.00 left $0.00 of $140.00" in line
        assert "+$40.00 from September" in line

    def test_only_the_six_most_used_budgets(self, client, db, cat, spend):
        names = ["Groceries", "Dining & Drinks", "Home", "Shopping", "Travel", "Education",
                 "Entertainment"]  # fmt: skip
        for i, name in enumerate(names):
            make_budget(db, cat(name), "100.00", effective_from=day("2026-01-01"))
            spend(cat(name), str(90 - i), "2026-10-02")
        budgets_card = card(client.get("/").text, "dash-budgets")
        assert len(re.findall(r'class="dash-budget"', budgets_card)) == 6
        assert "Entertainment" not in budgets_card
        assert "1 more budget on the Budgets page" in text_of(budgets_card)

    def test_top_categories_add_up_and_mark_hidden_ones(self, client, db, cat, spend):
        hidden = make_category(db, "Side business", is_hidden=True)
        spend(hidden, "300.00", "2026-10-02")
        for i, name in enumerate(["Groceries", "Dining & Drinks", "Home", "Shopping", "Travel",
                                  "Education", "Entertainment"]):  # fmt: skip
            spend(cat(name), str(100 - i * 10), "2026-10-02")
        top = card(client.get("/").text, "dash-categories")
        text = text_of(top)
        assert text.startswith("Top categories This month · share of all spending Side business")
        assert "(hidden)" in text
        assert "2 more categories $90.00" in text  # Education 50 + Entertainment 40
        assert "All spending $790.00" in text

    def test_top_merchants_link_to_their_transactions(self, client, groceries, cat, spend):
        spend(cat("Dining & Drinks"), "18.00", "2026-10-05", merchant_name="Tartine & Co")
        response = client.get("/")
        merchants = card(response.text, "dash-merchants")
        assert "Whole Foods" in merchants and "$250.00" in merchants
        href = next(h for h in links(merchants) if "Tartine" in h)
        query = parse_qs(urlsplit(href).query)
        assert query == {
            "from": ["2026-10-01"],
            "to": ["2026-10-31"],
            "category": [str(cat("Dining & Drinks").id)],
            "merchant": ["Tartine & Co"],
        }
        listed = client.get(href).context["page"]
        assert listed.total_count == 1 and listed.sums.net == D("18.00")


# --------------------------------------------------------------------------- links


class TestLinks:
    def test_spent_links_are_the_budgets_pages(self, client, db, account, groceries):
        dash = client.get("/").text
        budgets_page = client.get("/budgets").text
        expected = re.search(rf'id="spent-{groceries.id}" href="([^"]+)"', budgets_page)
        line = block(dash, f"dash-line-{groceries.id}")
        assert f'href="{expected.group(1)}"' in line
        top = block(dash, f"dash-cat-{groceries.id}")
        assert f'href="{expected.group(1)}"' in top
        # The list holds what the figure counts.
        listed = client.get(html.unescape(expected.group(1))).context["page"]
        assert listed.sums.net == D("250.00")

    def test_pending_off_and_statement_mode_carry_into_the_links(
        self, client, db, account, groceries
    ):
        set_app_settings(db, include_pending_in_budgets=False)
        account.statement_closing_day = 27
        make_statement(db, account, "2026-09-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        dash = client.get("/").text
        cycle = "from=2026-09-28&amp;to=2026-10-27&amp;basis=posted_date&amp;status=posted"
        assert f'href="/transactions?category={groceries.id}&amp;{cycle}"' in block(
            dash, f"dash-line-{groceries.id}"
        )
        budgets_page = client.get("/budgets").text
        assert f'href="/transactions?category={groceries.id}&amp;{cycle}"' in budgets_page
        assert "Statement cycle Sep 28 – Oct 27 · 13 days left" in text_of(dash)
        assert "closes Oct 27" in text_of(block(dash, "kpi-days"))
        assert "Compared with the last cycle" in dash

    def test_merchant_links_list_what_the_figure_counts(self, client, cat, spend):
        """Review finding 3: an Amex Offer credit (Credits & Rewards) under the merchant's
        name isn't in its figure, so it isn't in its list; "Whole Foods" and "WHOLE  FOODS"
        are one merchant on both."""
        spend(cat("Dining & Drinks"), "60.00", "2026-10-03", merchant_name="Uber Eats")
        spend(cat("Credits & Rewards"), "-10.00", "2026-10-05", merchant_name="Uber Eats",
              name="AMEX OFFER CREDIT UBER EATS")  # fmt: skip
        spend(cat("Groceries"), "40.00", "2026-10-02", merchant_name="Whole Foods")
        spend(cat("Groceries"), "15.00", "2026-10-03", merchant_name=None, name="WHOLE  FOODS")
        spend(cat("Shopping"), "7.00", "2026-10-04", merchant_name=None, name="whole foods")
        response = client.get("/")
        d = response.context["d"]
        assert [(m.merchant, m.amount) for m in d.top_merchants] == [
            ("Whole Foods", D("62.00")),
            ("Uber Eats", D("60.00")),
        ]
        hrefs = [h for h in links(card(response.text, "dash-merchants")) if "merchant=" in h]
        assert len(hrefs) == 2
        for href, m in zip(hrefs, d.top_merchants, strict=True):
            listed = client.get(href).context["page"]
            assert listed.sums.net == m.amount, href
            assert listed.total_count == m.transaction_count, href

    def test_annual_links_cover_the_year_to_date(self, client, db, cat, spend):
        travel = cat("Travel")
        make_budget(db, travel, "3000.00", effective_from=day("2026-01-01"), period="annual")
        spend(travel, "400.00", "2026-03-08")
        line = block(client.get("/").text, f"dash-annual-{travel.id}")
        assert f"/transactions?category={travel.id}&amp;from=2026-01-01&amp;to=2026-10-31" in line

    def test_a_cycle_across_the_year_end_shows_this_years_annual_budgets(
        self, client, db, account, cat, spend, freeze_today
    ):
        """Review finding 1: on Dec 30 in a Dec 28 – Jan 27 cycle the card is 2026's year to
        date (the Budgets page's 2026 tally), not 2027's empty one; on Jan 5 it is 2027's, with
        a note pointing to 2026's final tally."""
        account.statement_closing_day = 27
        for closing in ("2026-10-27", "2026-11-27", "2026-12-27"):
            make_statement(db, account, closing)
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        travel = cat("Travel")
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
        spend(travel, "2500.00", "2026-06-10")
        spend(travel, "300.00", "2026-12-29")
        spend(travel, "40.00", "2027-01-03")

        freeze_today("2026-12-30")
        response = client.get("/")
        annual = card(response.text, "dash-annual")
        text = text_of(annual)
        assert text.startswith("Annual budgets · 2026 Spending so far this year")
        assert "$2,800.00 of $6,000.00" in text and "$3,200.00 left" in text
        assert "This cycle runs into 2027: its annual budgets count from Jan 1." in text
        assert "/budgets#annual-closed-2026" in links(annual)
        year = f"/transactions?category={travel.id}&from=2026-01-01&to=2026-12-31"
        assert f"{year}&basis=posted_date" in links(annual)
        budgets_page = client.get("/budgets")
        (closed,) = budgets_page.context["annual_closed"]
        assert response.context["d"].annual == closed  # the Budgets page's own 2026 figures
        assert 'id="annual-closed-2026"' in budgets_page.text

        freeze_today("2027-01-05")
        annual = card(client.get("/").text, "dash-annual")
        text = text_of(annual)
        assert text.startswith("Annual budgets · 2027 Spending so far this year")
        assert "$40.00 of $6,000.00" in text
        assert "2026 ended on Dec 31, during this cycle: $2,800.00 of $6,000.00 for the year." in (
            text
        )
        assert "/budgets#annual-closed-2026" in links(annual)
        assert "/budgets#annual-section" in links(annual)  # "All budgets": the 2027 section


# --------------------------------------------------------------------------- notices


class TestNotices:
    def test_statement_mode_without_a_card(self, client, db, groceries):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=None)
        page = client.get("/").text
        notice = block(page, "period-notice")
        assert budgets.NOTICE_NO_CARD in html.unescape(notice)
        assert 'href="/settings"' in notice

    def test_a_flagged_cycle(self, client, db, account, groceries):
        account.statement_closing_day = 27
        for closing in ("2026-08-27", "2026-10-27"):
            make_statement(db, account, closing)
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        notice = block(client.get("/").text, "cycle-notice")
        assert "61 days long" in notice and 'href="/statements"' in notice

    def test_rows_held_for_review(self, client, db, account, groceries):
        make_transaction(db, account, amount="12.00", source="csv", duplicate_status="suspect")
        notice = text_of(block(client.get("/").text, "held-notice"))
        assert "1 imported row held for review." in notice

    def test_the_layout_banners_are_not_repeated(self, client, db, groceries, freeze_today):
        now = dt.datetime(2026, 10, 15, 12, tzinfo=dt.UTC)
        freeze_today("2026-10-15", now=now)
        make_item(
            db,
            consent_expires_at=now + dt.timedelta(days=10),
            status="needs_reconnect",
            error_code="ITEM_LOGIN_REQUIRED",
        )
        page = client.get("/").text
        assert page.count('id="reconnect-banner"') == 1
        assert "Reconnect" not in kpis(page)


# --------------------------------------------------------------------------- the page


def test_the_page_is_clean_and_complete(client, db, groceries, cat, spend):
    for month in (7, 8, 9, 10):
        spend(cat("Subscriptions & Streaming"), "15.49", f"2026-{month:02d}-02",
              merchant_name="Netflix")  # fmt: skip
    response = client.get("/", headers={"HX-Request": "true"})  # no partial: the full page
    page = response.text
    assert response.template.name == "dashboard/index.html"
    assert '<link rel="stylesheet" href="/static/css/pages/dashboard.css">' in page
    assert "/static/vendor/chart.umd.min.js" in page
    for element in ("dash-kpis", "dash-budgets", "dash-categories", "dash-pace", "dash-merchants",
                    "dash-upcoming", "dash-annual"):  # fmt: skip
        assert f'id="{element}"' in page
    assert page.count('<h1 class="page-header__title">') == 1
    assert csp_problems(page, rendered=True) == []
    upcoming = text_of(card(page, "dash-upcoming"))
    assert "No recurring charge is due before Oct 31, 2026." in upcoming  # next one: Nov 2
    assert "Recurring charges cost about $15.49 a month." in upcoming


def test_recurring_charges_still_to_come_this_period(client, cat, spend):
    for month in (6, 7, 8, 9):
        spend(cat("Subscriptions & Streaming"), "15.49", f"2026-{month:02d}-20",
              merchant_name="Netflix")  # fmt: skip
    upcoming = text_of(card(client.get("/").text, "dash-upcoming"))
    assert "Oct 20 Netflix Monthly · Subscriptions & Streaming $15.49" in upcoming
    assert "About $15.49 still to come before Oct 31." in upcoming


def test_the_compare_tile_says_it_counts_all_spending(client, db, cat, spend):
    """Review finding: next to "Spent" (the overall target's spending, annual budgets left out),
    "Compared with" counted all spending without saying so."""
    groceries, travel = cat("Groceries"), cat("Travel")
    make_budget(db, None, "3800.00", effective_from=day("2026-01-01"))
    make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
    spend(groceries, "100.00", "2026-09-05")
    spend(groceries, "200.00", "2026-10-05")
    spend(travel, "900.00", "2026-10-06")
    page = client.get("/").text
    assert "Spent $200 of $3,800 target" in text_of(block(page, "kpi-spent"))
    compare = text_of(block(page, "kpi-compare"))
    assert "Compared with September +$1,000 all spending: $1,100 so far, $100 by Sep 15" in compare


def test_pace_chart_compares_with_the_same_day_last_month(client, cat, spend):
    spend(cat("Home"), "100.00", "2026-09-01")
    spend(cat("Home"), "200.00", "2026-09-20")
    spend(cat("Home"), "30.00", "2026-10-02")
    page = client.get("/").text
    config = chart_config(page)
    assert config["type"] == "line" and config["format"] == "usd0"
    assert len(config["data"]["labels"]) == 32  # Start + Oct 1–31
    this, last = config["data"]["datasets"]
    assert this["data"][15] == "30.00" and this["data"][16] is None  # Oct 15 is today
    assert last["data"][15] == "100.00" and last["data"][20] == "300.00"
    assert last["data"][31] is None  # September has 30 days
    kpi = text_of(block(page, "kpi-compare"))
    assert "Compared with September −$70 all spending: $30 so far, $100 by Sep 15" in kpi
    described = text_of(block(page, "dash-pace-chart-desc"))
    assert "September: $100.00 by Sep 15, 2026, $300.00 in all." in described
