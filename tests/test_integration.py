"""Where the wave-1 packages must agree (integration).

* Statement-mode budget periods (BUD, and the "this period" views of TXN and CAT) are exactly the
  Statements page's cycles (STMT), user-typed closing dates and period start included.
* A category merge (CAT) writes a budget where the Budgets page (BUD) writes an edit.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from budgetapp.periods import STATEMENT
from budgetapp.services import budgets, categories, statements
from budgetapp.services import transactions as txns
from tests.factories import (
    get_category,
    make_account,
    make_budget,
    make_category,
    make_statement,
    set_app_settings,
)

TODAY = dt.date(2026, 10, 20)


@pytest.fixture(autouse=True)
def today(freeze_today):
    freeze_today(TODAY)


@pytest.fixture
def card(db):
    """The budget card, in statement mode. Amex reported the August statement (closing the 27th,
    the card's closing day); the user typed the September one, which closed on the 29th, and the
    earliest statement's period start (July 30)."""
    card = make_account(db, statement_closing_day=27)
    make_statement(
        db,
        card,
        "2026-08-27",
        closing_date_source="liabilities",
        plaid_issue_date=dt.date(2026, 8, 27),
    )
    statements.add_statement(db, card.id, "2026-09-29")
    statements.set_period_start(db, card.id, dt.date(2026, 8, 27), "2026-07-30")
    set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=card.id)
    db.flush()
    return card


def test_statement_mode_budget_periods_are_the_statements_pages_cycles(db, card):
    cycles = statements.cycles(db, card.id, start=dt.date(2026, 6, 1), end=TODAY)
    spans = [(c.start, c.closing_date) for c in cycles]
    assert spans[:3] == [  # newest first: the open cycle, the user's, Amex's (typed start)
        (dt.date(2026, 9, 30), dt.date(2026, 10, 27)),
        (dt.date(2026, 8, 28), dt.date(2026, 9, 29)),
        (dt.date(2026, 7, 30), dt.date(2026, 8, 27)),
    ]
    for cycle in cycles:
        middle = cycle.start + (cycle.closing_date - cycle.start) // 2
        for day in (cycle.start, middle, cycle.closing_date):
            expected = (cycle.start, cycle.closing_date)
            budget = budgets.period_for(db, day)
            assert (budget.kind, budget.start, budget.end) == (STATEMENT, *expected), day
            listed = txns.budget_period(db, day)  # /transactions "This statement period"
            assert (listed.start, listed.end) == expected, day
            shown = categories.current_period(db, today=day)  # /categories "this period"
            assert (shown.start, shown.end) == expected, day

    current = budgets.current_period(db)
    assert (current.start, current.end) == spans[0]
    previous = budgets.previous_period(db, current)
    assert (previous.start, previous.end) == spans[1]
    assert budgets.previous_period(db, previous).start == spans[2][0]
    assert budgets.next_period(db, previous) == current


def test_a_merge_starts_the_budget_where_a_budgets_page_edit_would(db, card):
    """A merge target with no budget now takes the source's, from ``budgets.edit_day`` of the
    current cycle (its middle day), the date a Budgets page edit of that cycle writes."""
    coffee, cafes = make_category(db, "Coffee"), make_category(db, "Cafes")
    make_budget(db, coffee, None, effective_from=dt.date(2026, 1, 1))  # no budget now
    make_budget(db, cafes, "100.00", effective_from=dt.date(2026, 1, 1))
    cycle = budgets.current_period(db)  # Sep 30 – Oct 27, after the user's Sep 29 statement
    edit = budgets.set_budget(db, get_category(db, "Groceries").id, Decimal("80.00"))
    plan = categories.merge_preview(db, cafes.id, coffee.id).counts.budgets
    assert plan.action == "take_source"
    assert plan.effective_from == edit.row.effective_from == budgets.edit_day(cycle)
    assert plan.effective_from == dt.date(2026, 10, 13)
    categories.merge_categories(db, cafes.id, coffee.id)
    line = budgets.overview(db, cycle).line_for(coffee.id)
    assert line is not None and line.budget == Decimal("100.00")
    assert budgets.overview(db, budgets.previous_period(db, cycle)).line_for(coffee.id) is None
