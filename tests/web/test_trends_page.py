"""The Trends page (INS-2): spending per category per calendar month for 12 months or more, with
the change from last month; the filter bar (HTMX), one category, and the empty and partial
states."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.factories import (
    get_category,
    make_account,
    make_category,
    make_statement,
    make_transaction,
    set_app_settings,
)
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

D = Decimal


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


@pytest.fixture(autouse=True)
def november(freeze_today):
    freeze_today("2026-11-05")


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
def history(db, cat, spend):
    """Groceries every month from Nov 2025 ($160 in Oct 2026); Travel in Aug 2026 only; Dining
    from Oct 2026; a little of November so far."""
    for year, month in [(2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 11)]:
        spend(cat("Groceries"), "100.00", f"{year}-{month:02d}-10")
    spend(cat("Groceries"), "60.00", "2026-10-20")
    spend(cat("Groceries"), "30.00", "2026-11-02")
    spend(cat("Travel"), "900.00", "2026-08-15")
    spend(cat("Dining & Drinks"), "45.00", "2026-10-03")
    spend(cat("Dining & Drinks"), "5.00", "2026-11-01")


def text_of(fragment: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(plain).split())


def section(page: str, element_id: str) -> str:
    match = re.search(
        rf'<(section|div) class="card[^"]*"[^>]*id="{element_id}".*?</\1>', page, re.S
    )
    assert match, f"no #{element_id}"
    return match.group(0)


def changes_card(page: str) -> str:
    """The text of the "Change from last month" card (a card or its empty state)."""
    match = re.search(
        r'id="trends-changes".*?(?=<section class="card[^"]*" id="trends-grid)', page, re.S
    )
    assert match, "no #trends-changes"
    return text_of(match.group(0).split(">", 1)[1])


def row(page: str, row_id: str) -> str:
    match = re.search(rf'<tr id="{row_id}".*?</tr>', page, re.S)
    assert match, f"no row #{row_id}"
    return match.group(0)


def tiles(page: str) -> str:
    match = re.search(r'id="trends-kpis".*?(?=<section class="card)', page, re.S)
    assert match, "no tiles"
    return text_of(match.group(0))


def chart_config(page: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="trends-chart-config">(.*?)</script>', page, re.S
    )
    assert match
    return json.loads(match.group(1))


# --------------------------------------------------------------------------- empty


def test_an_empty_database(client):
    response = client.get("/trends")
    assert response.status_code == 200
    page = response.text
    assert "No spending to chart yet" in page
    assert 'href="/connection"' in page and 'href="/import"' in page
    assert "trends-filters" not in page and "data-chart" not in page
    assert page.count('<h1 class="page-header__title">') == 1
    assert csp_problems(page, rendered=True) == []


def test_linked_but_nothing_has_arrived(client, db):
    from tests.factories import make_item

    make_item(db)
    page = client.get("/trends").text
    assert "Your accounts are linked, but no transactions have arrived yet." in page
    assert 'href="/import"' not in re.search(
        r'id="trends-empty".*?</div>\n</div>', page, re.S
    ).group(0)


def test_the_first_month_of_history(client, cat, spend):
    spend(cat("Home"), "40.00", "2026-11-02")
    page = client.get("/trends").text
    assert "No month to compare yet" in page and 'id="trends-changes"' in page
    assert "Your history starts on Nov 2, 2026" in text_of(page)
    text = tiles(page)
    assert "Last month — no complete month in this range yet" in text
    assert "Nov 2026 so far $40 the first month of your history" in text
    assert "Monthly average — needs one complete month" in text
    assert chart_config(page)["data"]["labels"] == ["Nov 2026"]
    assert csp_problems(page, rendered=True) == []


def test_the_historys_first_month_has_no_month_to_compare_with(client, freeze_today, cat, spend):
    """Review finding 2: history from Oct 15, today Nov 10. The tile shows October's own $120
    (not "—"), the change card says there is no month to compare with (not "No spending in Sep
    2026 or Oct 2026"), and November so far isn't set against Oct 1–10, which are before the
    history."""
    freeze_today("2026-11-10")
    groceries = cat("Groceries")
    spend(groceries, "120.00", "2026-10-15")
    spend(groceries, "30.00", "2026-11-02")
    page = client.get("/trends").text
    text = tiles(page)
    assert "Spent in Oct 2026 $120 from Oct 15, when your history starts" in text
    assert "Nov 2026 so far $30 your history starts on Oct 15" in text
    assert "+$30" not in text and "by Oct 10" not in text
    changes = changes_card(page)
    assert changes == (
        "No month to compare yet Your history starts on Oct 15, 2026, so Oct 2026 has no month "
        "before it to compare with. When Nov 2026 ends, this page compares the two."
    )
    assert csp_problems(page, rendered=True) == []

    one = client.get(f"/trends?category={groceries.id}").text
    assert "Groceries in Oct 2026 $120 from Oct 15, when your history starts" in tiles(one)


def test_a_history_starting_on_the_first(client, freeze_today, cat, spend):
    freeze_today("2026-11-10")
    spend(cat("Home"), "40.00", "2026-10-01")
    spend(cat("Home"), "10.00", "2026-11-03")
    page = client.get("/trends").text
    text = tiles(page)
    assert "Spent in Oct 2026 $40 the first month of your history" in text
    assert "Nov 2026 so far $10 $40 by Oct 10 (−$30)" in text
    assert changes_card(page).startswith("No month to compare yet")


def test_the_historys_first_month_on_its_own(client, cat, spend):
    """The earliest month the "Through" picker offers: one partial month, nothing before it."""
    spend(cat("Shopping"), "512.00", "2025-11-28")
    spend(cat("Groceries"), "48.00", "2025-11-29")
    spend(cat("Groceries"), "60.00", "2025-12-05")
    page = client.get("/trends?through=2025-11").text
    text = tiles(page)
    assert "Spent in Nov 2025 $560 from Nov 28, when your history starts" in text
    assert "In Nov 2025 $560 all categories" in text and "In these 1 months" not in text
    changes = changes_card(page)
    assert changes.startswith("No month to compare yet")
    assert changes.endswith("Choose a later month under “Through” to see a change.")
    assert "Earlier months have no data, so 1 month is shown." in text_of(page)
    assert chart_config(page)["data"]["labels"] == ["Nov 2025"]


def test_a_partial_month_before_is_named_as_such(client, cat, spend):
    spend(cat("Home"), "40.00", "2026-09-15")
    spend(cat("Home"), "50.00", "2026-10-15")
    changes = changes_card(client.get("/trends").text)
    assert changes.startswith(
        "Change from last month Oct 2026 compared with Sep 2026 (from Sep 15, when your history "
        "starts)"
    )


# --------------------------------------------------------------------------- all categories


class TestAllCategories:
    def test_twelve_months_with_the_change_from_last_month(self, client, cat, history):
        response = client.get("/trends")
        assert response.template.name == "trends/index.html"
        page = response.text
        assert '<link rel="stylesheet" href="/static/css/pages/trends.css">' in page
        assert "/static/vendor/chart.umd.min.js" in page
        text = tiles(page)
        assert "Spent in Oct 2026 $205 $105 more than Sep (+105%)" in text
        assert "Nov 2026 so far $35 $45 by Oct 5 (−$10)" in text
        assert "Monthly average $191 over 11 complete months" in text
        assert "Biggest increase +$60 Groceries: $100 → $160" in text
        changes = section(page, "trends-changes")
        assert "Oct 2026 compared with Sep 2026" in changes
        ids = [str(cat(name).id) for name in ("Groceries", "Dining & Drinks")]
        assert re.findall(r'<tr id="change-(\d+)"', changes) == ids  # Travel: none in Sep/Oct
        groceries = text_of(row(changes, f"change-{ids[0]}"))
        assert groceries == "Groceries $160.00 $100.00 +$60.00 +60% $30.00 $105.45"
        dining = text_of(row(changes, f"change-{ids[1]}"))
        assert dining == "Dining & Drinks $45.00 $0.00 +$45.00 new $5.00 $4.09"

    def test_new_and_gone_categories_and_months_without_spending(self, client, cat, history):
        page = client.get("/trends?through=2026-09").text
        travel = row(page, f"change-{cat('Travel').id}")
        assert "none now" in text_of(travel) and "−$900.00" in text_of(travel)
        page = client.get("/trends?through=2026-08").text
        assert "new" in text_of(row(page, f"change-{cat('Travel').id}"))
        grid = row(page, f"grid-{cat('Travel').id}")
        cells = re.findall(r'<td class="num[^"]*">(.*?)</td>', grid, re.S)
        # The range starts with the history (Nov 2025): no Travel until Aug 2026.
        assert [text_of(c) for c in cells[:2]] == ["$900", "$100"]  # total; Dec–Aug average
        assert [text_of(c) for c in cells[2:4]] == ["$0", "$0"]
        assert text_of(cells[11]) == "$900"

    def test_the_grid_lists_every_month(self, client, cat, history):
        page = client.get("/trends").text
        grid = section(page, "trends-grid-card")
        headers = re.findall(r'<abbr title="([^"]+)">', grid)
        assert (
            headers[0] == "Dec 2025" and headers[-1] == "Nov 2026 (so far)" and len(headers) == 12
        )
        groceries = row(page, f"grid-{cat('Groceries').id}")
        # Total and average first (in view on a phone), then Dec 2025 … Nov 2026.
        assert text_of(groceries).startswith("Groceries $1,190 $105 $100 $100")
        assert text_of(groceries).endswith("$160 $30")
        footer = text_of(re.search(r"<tfoot>.*?</tfoot>", grid, re.S).group(0))
        assert footer.startswith("All spending $2,140 $191 $100") and footer.endswith("$205 $35")

    def test_hidden_categories_are_listed_and_marked(self, client, db, cat, spend):
        hidden = make_category(db, "Side business", is_hidden=True)
        spend(cat("Home"), "1.00", "2026-09-02")
        spend(hidden, "80.00", "2026-10-04")
        page = client.get("/trends").text
        assert "(hidden)" in row(page, f"change-{hidden.id}")
        assert "Side business (hidden)" in text_of(row(page, f"grid-{hidden.id}"))

    def test_chart_is_stacked_by_category_in_their_own_colours(self, client, cat, history):
        config = chart_config(client.get("/trends").text)
        assert config["type"] == "bar" and config["stacked"] is True
        assert config["data"]["labels"][0] == "Dec 2025" and len(config["data"]["labels"]) == 12
        series = {d["label"]: d for d in config["data"]["datasets"]}
        assert series["Groceries"]["color"] == cat("Groceries").color
        assert series["Travel"]["data"][8] == "900.00"

    def test_links_open_the_months_transactions(self, client, cat, history):
        page = client.get("/trends").text
        link = re.search(
            r'class="trends-amount" href="([^"]+)"', row(page, f"change-{cat('Groceries').id}")
        )
        href = html.unescape(link.group(1))
        query = parse_qs(urlsplit(href).query)
        assert query == {
            "category": [str(cat("Groceries").id)],
            "from": ["2026-10-01"],
            "to": ["2026-10-31"],
        }
        listed = client.get(href).context["page"]
        assert listed.sums.net == D("160.00")

    def test_pending_charges_dont_count_when_budgets_leave_them_out(self, client, db, cat, spend):
        spend(cat("Home"), "10.00", "2026-09-04")
        spend(cat("Home"), "10.00", "2026-10-04")
        spend(cat("Home"), "7.00", "2026-10-05", pending=True)
        set_app_settings(db, include_pending_in_budgets=False)
        page = client.get("/trends").text
        assert "Pending charges aren’t counted" in page
        home = row(page, f"change-{cat('Home').id}")
        assert "$10.00" in home and "status=posted" in home

    def test_statement_mode_says_trends_use_calendar_months(self, client, db, account, history):
        account.statement_closing_day = 27
        make_statement(db, account, "2026-10-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        page = client.get("/trends").text
        assert "Calendar months." in page and 'id="trends-basis-note"' in page
        assert "basis=posted_date" not in page

    def test_longer_ranges_start_with_the_history(self, client, history):
        page = client.get("/trends?months=24").text
        assert chart_config(page)["data"]["labels"][0] == "Nov 2025"
        assert "Earlier months have no data, so 13 months are shown." in text_of(page)
        assert 'value="24" selected' in page

    def test_a_range_before_the_history(self, client, history):
        page = client.get("/trends?through=2025-06").text
        assert "No data in these months" in page
        assert "data-chart" not in page

    @pytest.mark.parametrize(
        "query",
        ["months=7", "months=abc", "through=2026-13", "through=never", "through=2999-01",
         "category=", "months=36&through=2027-01"],
    )  # fmt: skip
    def test_odd_parameters_fall_back_to_the_defaults(self, client, history, query):
        response = client.get(f"/trends?{query}")
        assert response.status_code == 200
        t = response.context["t"]
        assert t.through.start == day("2026-11-01")
        assert t.span in (12, 36)
        assert csp_problems(response.text, rendered=True) == []


# --------------------------------------------------------------------------- one category


class TestOneCategory:
    def test_month_by_month(self, client, cat, history):
        travel = cat("Travel")
        response = client.get(f"/trends?category={travel.id}")
        page = response.text
        assert 'id="trends-category-heading"' in page
        text = tiles(page)
        assert "Travel in Oct 2026 $0 nothing in Sep" not in text  # Sep had none either
        assert "Travel in Oct 2026 $0 no spending in Sep either" in text
        assert "In these 12 months $900 1 charge" in text
        aug = row(page, "month-2026-08")
        assert "+$900.00" in text_of(aug) and "new" in text_of(aug)
        assert f"category={travel.id}&amp;from=2026-08-01&amp;to=2026-08-31" in aug
        sep = row(page, "month-2026-09")
        assert "none now" in text_of(sep)
        chart = chart_config(page)
        assert [d["label"] for d in chart["data"]["datasets"]] == ["Travel", "Monthly average"]
        assert chart["data"]["datasets"][1]["dashed"] is True
        assert 'id="trends-changes"' not in page and 'id="trends-grid-card"' not in page
        assert csp_problems(page, rendered=True) == []

    def test_the_month_in_progress_is_compared_with_the_same_days(self, client, cat, history):
        page = client.get(f"/trends?category={cat('Dining & Drinks').id}").text
        nov = text_of(row(page, "month-2026-11"))
        assert nov.startswith("Nov 2026 so far, 5 days $5.00")
        assert "−$40.00 −89%" in nov  # against $45 by Oct 5
        assert "through Oct 5" in text_of(section(page, "trends-months-card"))

    def test_a_hidden_category(self, client, db, spend):
        hidden = make_category(db, "Side business", is_hidden=True)
        spend(hidden, "80.00", "2026-10-04")
        page = client.get(f"/trends?category={hidden.id}").text
        assert "its spending still counts in every total" in page

    @pytest.mark.parametrize("value", ["abc", "-3", "99999999999999999999", "987654"])
    def test_an_unknown_category_shows_all(self, client, history, value):
        page = client.get(f"/trends?category={value}").text
        assert 'id="trends-category-notice"' in page
        assert 'id="trends-changes"' in page

    def test_a_category_that_isnt_spending(self, client, cat, history):
        page = client.get(f"/trends?category={cat('Credits & Rewards').id}").text
        assert "That category can’t be shown." in page


# --------------------------------------------------------------------------- HTMX


def test_the_filter_bar_swaps_the_results(client, cat, history):
    page = client.get("/trends").text
    form = re.search(r'<form class="filter-bar"[^>]*>', page).group(0)
    assert 'hx-get="/trends"' in form and 'hx-target="#trends-results"' in form
    assert 'hx-replace-url="true"' in form and "data-autosubmit" in form
    response = client.get(f"/trends?months=24&category={cat('Travel').id}", headers=HTMX)
    assert response.template.name == "trends/_results.html"
    assert "<html" not in response.text and "trends-filters" not in response.text
    assert 'id="trends-chart-config"' in response.text
    assert csp_problems(response.text, rendered=True) == []


def test_new_results_are_announced(client, cat, history):
    """Screen readers hear what the filters now show: app.js copies the results' data-status
    into the live region kept outside them."""
    page = client.get("/trends").text
    live = re.search(r'<p [^>]*data-status-for="trends-results"[^>]*>', page).group(0)
    assert 'role="status"' in live and 'class="sr-only"' in live
    assert page.index(live) < page.index('id="trends-results"')
    response = client.get(f"/trends?months=24&category={cat('Travel').id}", headers=HTMX)
    status = re.search(r' data-status="([^"]*)"', response.text).group(1)
    assert html.unescape(status) == "Trends for Nov 2025 – Nov 2026, Travel."  # from the history
    response = client.get("/trends?through=2026-10", headers=HTMX)
    status = re.search(r' data-status="([^"]*)"', response.text).group(1)
    assert html.unescape(status) == "Trends for Nov 2025 – Oct 2026, all categories."


def test_the_through_picker_lists_the_history(client, history):
    page = client.get("/trends").text
    picker = re.search(r'<select[^>]*name="through".*?</select>', page, re.S).group(0)
    options = re.findall(r'<option value="([^"]+)"', picker)
    assert options[0] == "2026-11" and options[-1] == "2025-11" and len(options) == 13
    assert "Nov 2026 (this month)" in picker


# --------------------------------------------------------------------------- income (wave 3)


def income_category(db, name: str):
    """An income category by name: MONEY's migration seeds Paycheck, Interest and Other Income
    (W3-PLAN C2); without it the test creates the category."""
    try:
        category = get_category(db, name)
    except LookupError:
        category = make_category(db, name, kind="income", color="other")
    assert category.kind == "income"
    return category


@pytest.fixture
def checking(db):
    from tests.factories import make_item

    bank = make_item(db, institution_id="ins_109508", institution_name="First Platypus Bank")
    return make_account(db, bank, name="Plaid Checking", type="depository", subtype="checking")


@pytest.fixture
def earnings(db, cat, spend, checking, history):
    """On top of the spending history: a paycheck every month from Dec 2025 ($2,000; two in
    Oct), savings interest in Sep and Oct, and an Amex credit in Oct."""
    paycheck = income_category(db, "Paycheck")
    interest = income_category(db, "Interest")
    for year, month in [(2025, 12)] + [(2026, m) for m in range(1, 11)]:
        make_transaction(db, checking, amount="-2000.00", category=paycheck,
                         date=f"{year}-{month:02d}-15", name="ACME PAYROLL")  # fmt: skip
    make_transaction(db, checking, amount="-500.00", category=paycheck, date="2026-10-30",
                     name="ACME PAYROLL")  # fmt: skip
    make_transaction(db, checking, amount="-3.00", category=interest, date="2026-09-30")
    make_transaction(db, checking, amount="-4.00", category=interest, date="2026-10-31")
    spend(cat("Credits & Rewards"), "-25.00", "2026-10-12", name="AMEX OFFER CREDIT")
    return {"paycheck": paycheck, "interest": interest, "credits": cat("Credits & Rewards")}


def income_chart(page: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="trends-income-chart-config">(.*?)</script>',
        page,
        re.S,
    )
    assert match, "no income chart"
    return json.loads(match.group(1))


def income_links(page: str) -> list[tuple[str, Decimal]]:
    """``(href, figure)`` of every income amount that opens its transactions."""
    found = []
    for href, body in re.findall(r'<a class="trends-income" href="([^"]+)"[^>]*>(.*?)</a>',
                                 page, re.S):  # fmt: skip
        shown = text_of(body).replace("−", "-").replace("$", "").replace(",", "")
        found.append((html.unescape(href), Decimal(shown)))
    return found


class TestIncome:
    def test_no_switch_without_income(self, client, history):
        page = client.get("/trends").text
        assert "income-switch" not in page and "trends-income-chart" not in page
        assert '<div class="grid grid--4" id="trends-kpis">' in page

    def test_the_switch_sends_the_filters(self, client, earnings):
        page = client.get("/trends?months=24&through=2026-10").text
        form = re.search(r'<form class="income-switch".*?</form>', page, re.S).group(0)
        assert 'method="post" action="/trends/show-income"' in form
        assert 'hx-post="/trends/show-income"' in form and 'hx-trigger="change"' in form
        assert 'hx-target="#trends-results"' in form and 'hx-swap="innerHTML"' in form
        assert 'hx-include="#trends-filters"' in form
        assert 'name="next" value="/trends?months=24&amp;through=2026-10"' in form
        assert re.search(r'<input class="switch" type="checkbox" role="switch" id="show-income" '
                         r'name="show_income" value="1" checked>', form)  # fmt: skip
        assert 'id="trends-filters"' in page  # what hx-include names
        assert csp_problems(page, rendered=True) == []

    def test_tiles_for_the_last_month(self, client, earnings):
        page = client.get("/trends").text
        assert '<div class="grid grid--3" id="trends-kpis">' in page
        text = tiles(page)
        assert "Spent in Oct 2026 $205 $105 more than Sep (+105%)" in text
        assert "Income in Oct 2026 $2,529 $526 more than Sep (+26%)" in text
        # Net: the Dec–Oct average of income − spending (Aug: $900 of travel), so
        # (8 × 1,900 + 1,000 + 1,903 + 2,324) / 11.
        assert "Net in Oct 2026 +$2,324 average +$1,857 over 11 complete months" in text
        order = re.findall(r'id="(trend-[a-z-]+)"', page)
        assert order[:6] == ["trend-compare", "trend-income", "trend-net", "trend-so-far",
                             "trend-average", "trend-increase"]  # fmt: skip
        assert csp_problems(page, rendered=True) == []

    def test_the_income_and_spending_chart(self, client, earnings):
        page = client.get("/trends").text
        config = income_chart(page)
        assert config["type"] == "bar" and "stacked" not in config
        net, income, spent = config["data"]["datasets"]
        # The net line is listed first so Chart.js draws it over the bars.
        assert (net["type"], net["dashed"], net["color"]) == ("line", True, "cat-1")
        assert net["label"] == "Net (income − spending)"
        assert (income["label"], income["color"]) == ("Income", "cat-16")
        assert (spent["label"], spent["color"]) == ("Spending", "accent")
        assert "type" not in income and "type" not in spent
        assert config["data"]["labels"][0] == "Dec 2025" and len(config["data"]["labels"]) == 12
        assert income["data"][-2] == "2529.00" and spent["data"][-2] == "205.00"
        assert net["data"][-2] == "2324.00" and income["data"][-1] == "0.00"
        described = section(page, "trends-income-card")
        assert "Oct 2026 $2,529.00 $205.00 +$2,324.00" in text_of(described)
        assert page.index('id="trends-income-card"') < page.index('id="trends-chart-card"')

    def test_the_monthly_income_statement(self, client, earnings):
        page = client.get("/trends").text
        grid = section(page, "trends-grid-card")
        spending_total = text_of(row(grid, "grid-spending"))
        assert spending_total.startswith("All spending $2,140 $191 $100") and (
            spending_total.endswith("$205 $35")
        )
        assert "<tfoot>" in grid and "All spending" not in text_of(
            re.search(r"<tfoot>.*?</tfoot>", grid, re.S).group(0)
        )
        heading = re.search(r'<th scope="rowgroup"[^>]*>Income</th>', grid)
        assert heading
        ids = re.findall(r'<tr id="grid-income-(\d+)"', grid)
        assert ids == [str(earnings[k].id) for k in ("paycheck", "credits", "interest")]
        paycheck = text_of(row(grid, f"grid-income-{earnings['paycheck'].id}"))
        assert paycheck.startswith("Paycheck $22,500 $2,045 $2,000") and (
            paycheck.endswith("$2,500 $0")
        )
        credits = text_of(row(grid, f"grid-income-{earnings['credits'].id}"))
        assert credits.startswith("Credits & Rewards $25 $2 $0")  # its own line
        total = text_of(row(grid, "grid-income-total"))
        assert total.startswith("All income $22,532 $2,048 $2,000") and total.endswith("$2,529 $0")
        net = text_of(row(grid, "grid-net"))
        assert net.startswith("Net income − spending +$20,392 +$1,857 +$1,900")
        assert net.endswith("+$2,324 −$35")

    def test_income_links_list_what_they_count(self, client, earnings):
        page = client.get("/trends").text
        links = income_links(page)
        assert len(links) >= 20
        hrefs = {href for href, _ in links}
        october = [(h, f) for h, f in links if "from=2026-10-01" in h]
        assert {f for _, f in october} >= {Decimal("2500"), Decimal("25"), Decimal("4"),
                                           Decimal("2529")}  # fmt: skip
        for href, figure in links:
            listed = client.get(href).context["page"]
            assert listed.pages == 1, href
            # The list signs money in as Plaid does (negative): its Net is −figure, in cents.
            assert abs(listed.sums.net + figure) < 1, (href, listed.sums.net, figure)
        tile = re.search(r'<a class="stat" href="([^"]+)" id="trend-income">', page).group(1)
        assert html.unescape(tile) in hrefs
        assert client.get(html.unescape(tile)).context["page"].sums.net == Decimal("-2529.00")

    def test_one_category_stays_spending_only(self, client, cat, earnings):
        page = client.get(f"/trends?category={cat('Groceries').id}").text
        assert "income-switch" in page  # the choice is still offered
        assert "trends-income-card" not in page and 'id="trend-income"' not in page
        assert '<div class="grid grid--4" id="trends-kpis">' in page

    def test_switching_off_in_place_keeps_the_filters(self, client, db, earnings):
        response = client.post(
            "/trends/show-income",
            data={"months": "24", "through": "2026-09", "category": ""},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "trends/_results.html"
        t = response.context["t"]
        assert (t.span, t.through.start) == (24, day("2026-09-01")) and t.income is None
        fragment = response.text
        assert "trends-income-card" not in fragment and 'id="grid-income' not in fragment
        assert "Income is hidden on the Dashboard and Trends." in fragment
        assert 'data-status="Trends for' in fragment  # announced like a filter change
        assert csp_problems(fragment, rendered=True) == []
        from budgetapp.models import get_app_settings

        assert get_app_settings(db).show_income is False
        response = client.post(
            "/trends/show-income", data={"show_income": "1", "months": "12"}, headers=HTMX
        )
        assert "trends-income-card" in response.text
        assert "Income is shown on the Dashboard and Trends." in response.text

    @pytest.mark.parametrize(
        ("next_url", "location"),
        [
            ("/trends?months=24&through=2026-10", "/trends?months=24&through=2026-10"),
            ("/trends", "/trends"),
            ("https://evil.example/trends", "/trends"),
            ("//evil.example/trends", "/trends"),
            ("/transactions", "/trends"),
            ("/trendsetter", "/trends"),
            ("", "/trends"),
        ],
    )
    def test_without_javascript_a_plain_post_returns_to_the_page(
        self, client, earnings, next_url, location
    ):
        response = client.post(
            "/trends/show-income", data={"next": next_url}, follow_redirects=False
        )
        assert response.status_code == 303 and response.headers["location"] == location

    def test_income_without_spending(self, client, db, checking):
        paycheck = income_category(db, "Paycheck")
        make_transaction(db, checking, amount="-1000.00", category=paycheck, date="2026-10-15")
        page = client.get("/trends").text
        assert "Income in Oct 2026 $1,000" in tiles(page)
        grid = section(page, "trends-grid-card")
        assert "All income $1,000" in text_of(row(grid, "grid-income-total"))
        assert csp_problems(page, rendered=True) == []

    def test_the_first_month_of_history_shows_this_months_income(self, client, db, checking):
        paycheck = income_category(db, "Paycheck")
        make_transaction(db, checking, amount="-1000.00", category=paycheck, date="2026-11-02")
        text = tiles(client.get("/trends").text)
        assert "Income in Nov 2026 so far $1,000 all from Paycheck" in text
        assert "Net in Nov 2026 so far +$1,000 income − all spending" in text
