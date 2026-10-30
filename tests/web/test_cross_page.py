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
    get_category,
    make_account,
    make_budget,
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
