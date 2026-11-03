"""Links between pages land on views that agree with the page they came from (integration of the
Budgets, Recurring, Transactions and Statements packages)."""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

import pytest

from budgetapp.money import MINUS
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_budget,
    make_split,
    make_statement,
    make_transaction,
    set_app_settings,
)
from tests.web.csp import csp_problems


@pytest.fixture(autouse=True)
def october(freeze_today):
    freeze_today("2026-10-20")


def spent_link(response, category) -> tuple[str, Decimal]:
    """The Budgets page's Spent link for ``category``: its URL and the figure it shows."""
    match = re.search(
        rf'<a class="budget-spent" id="spent-{category.id}" href="([^"]+)"[^>]*>(.*?)</a>',
        response.text,
        re.S,
    )
    assert match is not None, f"no Spent link for {category.name}"
    shown = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
    amount = Decimal(shown.replace(MINUS, "-").replace("$", "").replace(",", ""))
    return html.unescape(match.group(1)), amount


@pytest.mark.parametrize(
    ("mode", "include_pending", "expected"),
    [
        ("statement", True, Decimal("89.00")),
        ("statement", False, Decimal("82.00")),
        ("calendar", True, Decimal("67.00")),
        ("calendar", False, Decimal("60.00")),
    ],
)
def test_a_spent_link_lists_exactly_what_the_budget_counts(
    client, db, mode, include_pending, expected
):
    """BUD's Spent figure comes from ``period_spending`` (posting dates in statement mode, the
    pending setting); the Transactions view its link opens must hold exactly those rows."""
    card = make_account(db, statement_closing_day=27)
    make_statement(db, card, "2026-09-27")  # this cycle: Sep 28 – Oct 27
    set_app_settings(
        db,
        budget_period_mode=mode,
        budget_cycle_account_id=card.id,
        include_pending_in_budgets=include_pending,
    )
    groceries = get_category(db, "Groceries")
    make_budget(db, groceries, "500.00", effective_from=dt.date(2026, 1, 1))

    def spend(amount: str, date: str, **kwargs) -> None:
        make_transaction(db, card, amount=amount, date=date, category=groceries, **kwargs)

    spend("30.00", "2026-09-29", authorized_date="2026-09-26")  # charged before, posted inside
    spend("12.00", "2026-09-30", authorized_date="2026-09-30")  # in the cycle, not in October
    spend("20.00", "2026-10-28", authorized_date="2026-10-26")  # charged inside, posted after
    spend("50.00", "2026-10-05")
    spend("-10.00", "2026-10-06")  # a refund
    spend("7.00", "2026-10-19", pending=True)
    spend("99.00", "2026-10-07", source="csv", duplicate_status="suspect")  # never counted

    href, figure = spent_link(client.get("/budgets"), groceries)
    assert figure == expected
    listed = client.get(href)
    assert listed.status_code == 200
    page = listed.context["page"]
    assert page.pages == 1
    assert sum(row.amount for row in page.rows) == page.sums.net == figure
    by_posting_date = mode == "statement"
    assert page.dates.label.endswith(", by posting date") == by_posting_date
    # the filter bar keeps the basis when the user narrows the list further
    assert ('<input type="hidden" name="basis" value="posted_date">' in listed.text) == (
        by_posting_date
    )
    assert csp_problems(listed.text, rendered=True) == []


def test_a_recurring_series_links_to_all_of_its_charges(client, db):
    """A yearly renewal joined across the CSV era (the Amex description) and the Plaid era (the
    merchant's name): the Recurring page's link must list both charges, not only the last."""
    card = make_account(db)
    descriptor = "CA DMV REGISTRATION SACRAMENTO CA"
    make_transaction(
        db,
        card,
        amount="198.00",
        date="2025-04-17",
        source="csv",
        name="CA DMV REGISTRATION       SACRAMENTO CA",
        original_description=descriptor,
    )
    make_transaction(
        db,
        card,
        amount="212.00",
        date="2026-04-17",
        name="CA DMV REGISTRATION",
        merchant_name="California DMV",
        merchant_entity_id="ent-dmv",
        original_description=descriptor,
    )
    page = client.get("/recurring").text
    match = re.search(r'<a class="table__primary" href="([^"]+)">California DMV</a>', page)
    assert match is not None
    assert "2 charges since Apr 2025" in page
    listed = client.get(html.unescape(match.group(1))).context["page"]
    assert sorted(row.amount for row in listed.rows) == [Decimal("198.00"), Decimal("212.00")]


# --------------------------------------------------------------------------- split charges


def money_links(html_text: str, css_class: str) -> list[tuple[str, str, Decimal]]:
    """``(id, href, figure)`` of every ``<a class="css_class">`` whose text is an amount."""
    found = []
    for attrs, body in re.findall(rf'<a class="{css_class}"([^>]*)>(.*?)</a>', html_text, re.S):
        href = re.search(r'href="([^"]+)"', attrs)
        element_id = re.search(r'id="([^"]+)"', attrs)
        shown = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        amount = Decimal(shown.replace(MINUS, "-").replace("$", "").replace(",", ""))
        found.append(
            (element_id.group(1) if element_id else "", html.unescape(href.group(1)), amount)
        )
    return found


def first_list_link(html_text: str, element_id: str) -> str:
    """The first Transactions link at or after the element with this id."""
    match = re.search(rf'id="{element_id}".*?href="(/transactions\?[^"]+)"', html_text, re.S)
    assert match is not None, f"no link in #{element_id}"
    return html.unescape(match.group(1))


@pytest.fixture
def split_charges(db):
    """A card with split charges across categories, in this cycle and month, last month and
    earlier this year. Budgets: Groceries, Dining and Home monthly, Travel annual."""
    card = make_account(db, statement_closing_day=27)
    make_statement(db, card, "2026-09-27")  # this cycle: Sep 28 – Oct 27
    set_app_settings(db, budget_cycle_account_id=card.id)
    cat = {
        key: get_category(db, name)
        for key, name in (
            ("travel", "Travel"),
            ("dining", "Dining & Drinks"),
            ("groceries", "Groceries"),
            ("home", "Home"),
            ("shopping", "Shopping"),
            ("credits", "Credits & Rewards"),
        )
    }
    for key, amount in (("groceries", "500.00"), ("dining", "300.00"), ("home", "200.00")):
        make_budget(db, cat[key], amount, effective_from=dt.date(2026, 1, 1))
    make_budget(db, cat["travel"], "6000.00", effective_from=dt.date(2026, 1, 1), period="annual")

    def charge(amount, date, category, merchant, parts=(), **kwargs):
        txn = make_transaction(
            db, card, amount=amount, date=date, category=cat[category], merchant_name=merchant,
            **kwargs,
        )  # fmt: skip
        for key, part in parts:
            make_split(db, txn, cat[key], part)
        return txn

    # The demo's hotel stay: $533.37 Travel and $94.78 Dining (room service).
    charge("628.15", "2026-10-09", "travel", "Marriott", [("travel", "533.37"),
           ("dining", "94.78")], authorized_date="2026-10-08")  # fmt: skip
    charge("410.00", "2026-08-21", "travel", "Hyatt", [("travel", "350.00"), ("dining", "60.00")])
    charge("300.00", "2026-03-12", "travel", "Delta")
    charge("100.00", "2026-10-05", "shopping", "Costco", [("groceries", "60.00"),
           ("home", "40.00")])  # fmt: skip
    charge("-30.00", "2026-10-12", "shopping", "Costco", [("groceries", "-20.00"),
           ("home", "-10.00")])  # a split refund  # fmt: skip
    # Bought in September, posted in this cycle: September by purchase date, this cycle by
    # posting date.
    charge("80.00", "2026-09-29", "groceries", "Trader Joe's", [("groceries", "50.00"),
           ("dining", "30.00")], authorized_date="2026-09-26")  # fmt: skip
    # A part the user filed outside spending: only the Shopping part is spending.
    charge("120.00", "2026-10-06", "shopping", "Amazon", [("shopping", "100.00"),
           ("credits", "20.00")])  # fmt: skip
    # Parts that don't add up: the whole charge stays in Dining, everywhere.
    charge("40.00", "2026-10-14", "dining", "Bistro", [("groceries", "20.00"), ("home", "19.99")])
    charge("50.00", "2026-10-03", "groceries", "Whole Foods")
    charge("42.00", "2026-10-10", "dining", "Tartine")
    charge("7.00", "2026-10-19", "groceries", "Whole Foods", pending=True)
    make_transaction(db, card, amount="-500.00", date="2026-10-15", category=card_payments(db),
                     name="AUTOPAY PAYMENT - THANK YOU")  # fmt: skip
    return cat


def assert_lists_add_up(client, links: list[tuple[str, str, Decimal]]) -> int:
    """Each link's list Net equals its figure; returns how many lists counted a split in part."""
    partly = 0
    for element_id, href, figure in links:
        page = client.get(href).context["page"]
        assert page.pages == 1, href
        assert page.sums.net == figure, (element_id, href, page.sums, figure)
        partly += page.sums.partly_counted > 0
    return partly


@pytest.mark.parametrize("include_pending", [True, False])
@pytest.mark.parametrize("mode", ["calendar", "statement"])
def test_budgets_spent_links_add_up_with_split_charges(
    client, db, split_charges, mode, include_pending
):
    """Every Spent link on the Budgets page (monthly, not budgeted, annual; statement mode
    with ``basis=posted_date``) opens a list whose Net is the figure, split charges included."""
    set_app_settings(db, budget_period_mode=mode, include_pending_in_budgets=include_pending)
    page = client.get("/budgets").text
    links = money_links(page, "budget-spent")
    ids = {element_id for element_id, _href, _figure in links}
    cat = split_charges
    assert {f"spent-{cat[k].id}" for k in ("groceries", "dining", "home")} <= ids
    assert f"annual-spent-{cat['travel'].id}" in ids
    assert all(("basis=posted_date" in href) == (mode == "statement") for _, href, _ in links)
    assert all(("status=posted" in href) != include_pending for _, href, _ in links)
    assert assert_lists_add_up(client, links) >= 3


def test_dashboard_links_add_up_with_split_charges(client, split_charges):
    """The Dashboard's budget (Spent), top-category, top-merchant and annual links."""
    response = client.get("/")
    page, d = response.text, response.context["d"]
    cat = split_charges
    links = [(f"dash-line-{line.category.id}", line.spent) for line in d.overview.lines]
    links += [(f"dash-cat-{row.category.id}", row.amount) for row in d.top_categories.rows]
    links += [(f"dash-annual-{line.category.id}", line.spent) for line in d.annual.lines]
    resolved = [(eid, first_list_link(page, eid), figure) for eid, figure in links]
    assert f"dash-cat-{cat['travel'].id}" in {eid for eid, _ in links}
    merchants = re.findall(r'<a class="table__primary dash-link" href="([^"]+)">', page)
    assert len(merchants) == len(d.top_merchants)
    resolved += [
        (m.merchant, html.unescape(href), m.amount)
        for href, m in zip(merchants, d.top_merchants, strict=True)
    ]
    assert {"Marriott", "Costco", "Amazon"} <= {m.merchant for m in d.top_merchants}
    assert assert_lists_add_up(client, resolved) >= 4
    amazon = next(m for m in d.top_merchants if m.merchant == "Amazon")
    assert amazon.amount == Decimal("100.00")  # the Credits & Rewards part isn't spending


@pytest.mark.parametrize("category", ["dining", "groceries", "travel"])
def test_trends_month_links_add_up_with_split_charges(client, split_charges, category):
    """Trends' per-month links (a category's month by month, and the change table)."""
    category_id = split_charges[category].id
    months = money_links(client.get(f"/trends?category={category_id}").text, "trends-amount")
    assert len(months) >= 2
    assert assert_lists_add_up(client, months) >= 1
    changes = money_links(client.get("/trends").text, "trends-amount")
    assert changes
    assert_lists_add_up(client, changes)


def test_the_demo_marriott_stay_counts_only_its_travel_part(client, db, split_charges):
    """W1 open issue 1, as the demo showed it: Travel's annual figure counts $533.37 of the
    $628.15 Marriott charge, and so does the list its link opens (it used to add the $94.78
    of room service filed under Dining)."""
    travel = split_charges["travel"]
    page = client.get("/budgets").text
    [(_id, href, figure)] = [
        link for link in money_links(page, "budget-spent") if link[0] == f"annual-spent-{travel.id}"
    ]
    assert figure == Decimal("1183.37")  # Delta 300 + Hyatt 350 + Marriott 533.37
    listed = client.get(href)
    result = listed.context["page"]
    assert result.sums.net == figure
    assert result.sums.partly_counted == 2  # Marriott and Hyatt
    marriott = next(row for row in result.rows if row.merchant_name == "Marriott")
    assert marriott.amount == Decimal("628.15")
    assert [(s.category.name, s.amount) for s in marriott.splits] == [
        ("Travel", Decimal("533.37")),
        ("Dining & Drinks", Decimal("94.78")),
    ]
    assert "2 split charges count only their parts in this category" in html.unescape(listed.text)
    assert csp_problems(listed.text, rendered=True) == []
