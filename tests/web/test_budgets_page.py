"""The Budgets page (BUD-1..5): rendering, period navigation and every edit, HTMX and plain."""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp.models import Budget
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_budget,
    make_category,
    make_statement,
    make_transaction,
    set_app_settings,
)
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

D = Decimal
OCT_1 = "2026-10-01"


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
    """Groceries: $500 a month since January, $250 spent in October."""
    groceries = cat("Groceries")
    make_budget(db, groceries, "500.00", effective_from=day("2026-01-01"))
    spend(groceries, "300.00", "2026-10-03")
    spend(groceries, "-50.00", "2026-10-04")
    return groceries


def text_of(response) -> str:
    """The response's visible text, whitespace collapsed (for sentence checks)."""
    plain = re.sub(r"<[^>]+>", " ", response.text)
    return " ".join(html.unescape(plain).split())


def rows(db, category=None):
    query = select(Budget).order_by(Budget.effective_from, Budget.id)
    if category is not None:
        query = query.where(Budget.category_id == category.id)
    return [(b.period, b.effective_from, b.amount, b.rollover) for b in db.scalars(query)]


def row_of(response, kind: str, category) -> str:
    """The ``<tr id="<kind>-<id>">`` row in a response (``line``, ``annual``, ``unbudgeted``)."""
    match = re.search(rf'<tr id="{kind}-{category.id}">.*?</tr>', response.text, re.S)
    assert match is not None, f"no {kind} row for {category.name}"
    return match.group(0)


def toasts(response) -> list[str]:
    return [
        " ".join(html.unescape(t).split())
        for t in re.findall(r'<div class="toast__text">(.*?)</div>', response.text, re.S)
    ]


def oob_parts(response) -> tuple[str, list[str]]:
    """``(markup outside <template>s, ids of the out-of-band elements inside them)``.

    htmx parses a response that starts with a ``<tr>`` in table context: a ``<tfoot>``,
    ``<table>`` or ``<form>`` after the row would be dropped by the HTML parser (checked in
    Chrome), so out-of-band parts after a row must each sit in a ``<template>``.
    """
    templates = re.findall(r"<template>(.*?)</template>", response.text, re.S)
    outside = re.sub(r"<template>.*?</template>", "", response.text, flags=re.S)
    ids = []
    for part in templates:
        match = re.match(r'\s*<\w+[^>]*\bid="([^"]+)"[^>]*\bhx-swap-oob="true"', part)
        assert match is not None, f"a template without one out-of-band element: {part[:80]}"
        ids.append(match.group(1))
    return outside, ids


# --------------------------------------------------------------------------- the page


class TestPage:
    def test_budgets_spending_and_the_annual_section(self, client, db, cat, spend, groceries):
        travel, dining = cat("Travel"), cat("Dining & Drinks")
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
        make_budget(db, travel, None, effective_from=day("2026-01-01"))
        spend(travel, "1500.00", "2026-06-02")
        spend(dining, "80.00", "2026-10-05")
        make_budget(db, None, "1000.00")
        response = client.get("/budgets")
        assert response.status_code == 200
        assert response.template.name == "budgets/index.html"
        ov = response.context["ov"]
        assert [line.category.name for line in ov.lines] == ["Groceries"]
        assert [u.category.name for u in ov.unbudgeted] == ["Dining & Drinks"]
        assert [a.category.name for a in response.context["annual"].lines] == ["Travel"]
        page = text_of(response)
        for sentence in (
            "Calendar month · 17 days left",
            "Overall target",
            "of $1,000.00 target",
            "Monthly budgets",
            "Spending without a budget",
            "Annual budgets · 2026",
        ):
            assert sentence in page
        assert html.unescape(response.text).count('<h1 class="page-header__title">') == 1
        assert '<link rel="stylesheet" href="/static/css/pages/budgets.css">' in response.text
        assert '<script src="/static/js/budgets.js" defer></script>' in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_a_budget_row(self, client, db, groceries):
        response = client.get("/budgets")
        row = row_of(response, "line", groceries)
        assert 'value="500.00"' in row and 'aria-label="Groceries monthly budget"' in row
        href = f"/transactions?category={groceries.id}&amp;from=2026-10-01&amp;to=2026-10-31"
        assert f'href="{href}"' in row
        assert "$250.00" in row  # spent: charges minus the refund
        assert 'role="meter"' in row and 'aria-valuetext="50% used' in row
        assert 'role="switch"' in row and 'name="enabled"' in row
        assert f'hx-post="/budgets/category/{groceries.id}"' in row
        assert 'name="period" value="2026-10-01"' in row

    def test_an_empty_database(self, client):
        response = client.get("/budgets")
        assert response.status_code == 200
        page = text_of(response)
        assert "No monthly budgets yet" in page
        assert "No annual budgets for 2026" in page
        assert "No budgets yet for Oct 2026" in page
        assert "Spending without a budget" not in page
        assert csp_problems(response.text, rendered=True) == []

    def test_card_payments_and_credits_are_not_spending(self, client, db, cat, spend):
        spend(card_payments(db), "-900.00", "2026-10-02")
        spend(cat("Credits & Rewards"), "-20.00", "2026-10-02")
        ov = client.get("/budgets").context["ov"]
        assert ov.totals.spent == D("0.00") and ov.unbudgeted == ()

    def test_hidden_categories_are_not_listed(self, client, db, cat, spend):
        home = cat("Home")
        make_budget(db, home, "100.00")
        home.is_hidden = True
        spend(cat("Shopping"), "10.00", "2026-10-02")
        cat("Shopping").is_hidden = True
        db.flush()
        response = client.get("/budgets")
        assert f'id="line-{home.id}"' not in response.text
        assert "Spending without a budget" not in text_of(response)
        assert "Includes $10.00 in hidden categories." in text_of(response)

    def test_rows_are_sorted_by_percent_used_then_name(self, client, db, cat, spend):
        pets = make_category(db, "Pets")
        for category, budget, spent in (
            (cat("Home"), "100", "10"),
            (pets, "100", "90"),
            (cat("Groceries"), "200", "20"),
        ):
            make_budget(db, category, budget)
            spend(category, spent, "2026-10-02")
        ids = re.findall(r'<tr id="line-(\d+)">', client.get("/budgets").text)
        assert [int(i) for i in ids] == [pets.id, cat("Groceries").id, cat("Home").id]

    def test_carry_in_notes(self, client, db, cat, spend):
        care, home = cat("Personal Care"), cat("Home")
        make_budget(db, care, "100.00", effective_from=day("2026-09-01"), rollover=True)
        make_budget(db, home, "100.00", effective_from=day("2026-09-01"), rollover=True)
        spend(care, "60.00", "2026-09-10")
        spend(home, "125.00", "2026-09-10")
        page = text_of(client.get("/budgets"))
        assert "+$40.00 from Sep" in page
        assert "−$25.00 overspent in Sep" in page

    def test_over_budget_is_not_shown_by_colour_alone(self, client, db, cat, spend):
        dining = cat("Dining & Drinks")
        make_budget(db, dining, "100.00")
        spend(dining, "124.00", "2026-10-02")
        row = row_of(client.get("/budgets"), "line", dining)
        assert "progress--over" in row and "(over budget)" in row
        assert 'aria-valuetext="124% used, over budget' in row

    def test_a_zero_budget_with_spending_says_over_in_words(self, client, db, cat, spend):
        home = cat("Home")
        make_budget(db, home, "0.00")
        spend(home, "12.00", "2026-10-02")
        assert "Over by $12.00" in text_of(client.get("/budgets"))

    def test_suggest_buttons_carry_the_suggestion(self, client, db, cat, spend, groceries):
        for month, amount in ((7, "600.00"), (8, "612.40"), (9, "625.00")):
            spend(groceries, amount, f"2026-{month:02d}-10")
        response = client.get("/budgets")
        button = re.search(rf'<button[^>]*id="suggest-budget-{groceries.id}"[^>]*>', response.text)
        assert button is not None
        assert 'data-suggest="615.00"' in button.group(0)
        assert f'data-suggest-target="budget-{groceries.id}"' in button.group(0)
        assert "average of Jul–Sep 2026, rounded up" in html.unescape(button.group(0))
        assert 'id="suggest-overall"' in response.text

    def test_no_suggestions_without_history(self, client, groceries):
        response = client.get("/budgets")
        assert "data-suggest=" not in response.text


class TestPeriods:
    def test_navigating_to_another_period(self, client, groceries):
        response = client.get("/budgets?period=2026-09-10")
        ov = response.context["ov"]
        assert (ov.period.start, ov.period.end) == (day("2026-09-01"), day("2026-09-30"))
        assert 'href="/budgets?period=2026-08-01"' in response.text
        assert 'href="/budgets?period=2026-10-01"' in response.text
        assert 'id="period-today"' in response.text
        assert "You are looking at a past month." in text_of(response)
        assert "Changes you make here apply from Sep 2026 on" in text_of(response)

    def test_htmx_navigation_returns_the_page_body(self, client, groceries):
        response = client.get("/budgets?period=2026-11-01", headers=HTMX)
        assert response.template.name == "budgets/_page.html"
        assert "<html" not in response.text and '<h1 class="page-header__title">' in response.text
        assert "You are planning ahead." in text_of(response)
        assert 'hx-target="#budgets-page"' in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_the_current_period_has_no_note(self, client):
        response = client.get("/budgets")
        assert 'id="edit-note"' not in response.text and 'id="period-today"' not in response.text

    def test_a_malformed_period_is_a_400_page(self, client):
        assert client.get("/budgets?period=soon").status_code == 400

    def test_statement_cycles(self, client, db, account):
        account.statement_closing_day = 27
        make_statement(db, account, "2026-09-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=account.id)
        response = client.get("/budgets")
        page = text_of(response)
        assert "Statement Sep 28 – Oct 27" in page
        assert "Statement cycle · closes Oct 27 · 13 days left" in page
        assert 'href="/budgets?period=2026-08-28"' in response.text  # the previous cycle

    def test_statement_mode_without_a_card_says_so(self, client, db):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=None)
        response = client.get("/budgets")
        banner = re.search(
            r'<div class="banner[^"]*"[^>]*id="period-notice".*?</div>\s*</div>',
            response.text,
            re.S,
        )
        assert banner is not None
        assert "Budgets use calendar months until you pick the card in Settings" in text_of(
            response
        )
        assert 'href="/settings"' in banner.group(0)
        assert "Calendar month · 17 days left" in text_of(response)

    def test_the_settings_link(self, client):
        response = client.get("/budgets")
        link = re.search(r'<a [^>]*id="period-settings"[^>]*>', response.text).group(0)
        assert 'href="/settings"' in link


# --------------------------------------------------------------------------- monthly edits


class TestMonthlyEdits:
    def test_an_htmx_edit_returns_the_row_and_the_summaries(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "650", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "budgets/_line_update.html"
        body = response.text
        assert body.lstrip().startswith(f'<tr id="line-{groceries.id}">')
        assert 'value="650.00"' in body
        assert body.count('<tfoot id="budget-totals" hx-swap-oob="true">') == 1
        assert '<section class="card budget-overall" id="overall-card"' in body
        assert body.count('hx-swap-oob="true"') == 3  # totals, overall card, unbudgeted
        assert '<div id="unbudgeted" hx-swap-oob="true">' in body
        assert toasts(response) == ["Groceries has a monthly budget of $650.00 from Oct 2026 on."]
        assert "<html" not in body and csp_problems(body, rendered=True) == []
        assert rows(db, groceries) == [
            ("monthly", day("2026-01-01"), D("500.00"), False),
            ("monthly", day("2026-10-01"), D("650.00"), False),
        ]

    @pytest.mark.parametrize(
        ("path", "data", "kind", "status", "totals"),
        [
            ("", {"amount": "650"}, "line", 200, "budget-totals"),
            ("", {"amount": "-5"}, "line", 422, "budget-totals"),
            ("/rollover", {"enabled": "1"}, "line", 200, "budget-totals"),
            ("/annual", {"amount": "7000"}, "annual", 200, "annual-totals"),
            ("/annual", {"amount": "1.001"}, "annual", 422, "annual-totals"),
        ],
    )
    def test_row_responses_keep_out_of_band_parts_in_templates(
        self, client, db, cat, groceries, path, data, kind, status, totals
    ):
        """After the row, the totals, the overall card (with its form) and the unbudgeted list
        (with its table) each come in a <template>, so htmx's parser keeps them whole."""
        if kind == "annual":
            travel = cat("Travel")
            make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
            target = travel
        else:
            target = groceries
        response = client.post(
            f"/budgets/category/{target.id}{path}", data={**data, "period": OCT_1}, headers=HTMX
        )
        assert response.status_code == status
        assert response.text.lstrip().startswith(f'<tr id="{kind}-{target.id}">')
        outside, oob_ids = oob_parts(response)
        assert oob_ids == [totals, "overall-card", "unbudgeted"]
        after_row = outside.split("</tr>", 1)[1]
        assert not re.search(r"<(table|thead|tbody|tfoot|tr|section|form)\b", after_row)
        assert re.findall(r'hx-swap-oob="([^"]+)"', outside) == (
            ["beforeend:#flash"] if status == 200 else []
        )

    def test_edits_persist_and_past_periods_keep_their_amounts(self, client, db, groceries):
        client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "650", "period": OCT_1},
            headers=HTMX,
        )
        assert client.get("/budgets").context["ov"].lines[0].budget == D("650.00")
        september = client.get("/budgets?period=2026-09-01").context["ov"]
        assert september.lines[0].budget == D("500.00")

    def test_the_same_amount_changes_nothing_and_says_nothing(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "$500.00", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 200 and toasts(response) == []
        assert len(rows(db, groceries)) == 1

    @pytest.mark.parametrize(
        ("amount", "message"),
        [
            ("-5", "A budget can't be negative."),
            ("12.345", "Amounts can have at most two decimal places."),
            ("lots", "“lots” is not an amount."),
        ],
    )
    def test_invalid_amounts_come_back_inline(self, client, db, groceries, amount, message):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": amount, "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.template.name == "budgets/_line_update.html"
        row = row_of(response, "line", groceries)
        assert message in html.unescape(row)
        assert 'aria-invalid="true"' in row and f'value="{html.escape(amount)}"' in row
        assert f'aria-describedby="budget-{groceries.id}-error"' in row
        assert 'role="alert"' in row
        assert rows(db, groceries) == [("monthly", day("2026-01-01"), D("500.00"), False)]

    def test_removing_a_budget(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}", data={"amount": "", "period": OCT_1}, headers=HTMX
        )
        assert response.status_code == 200
        assert "No budget from Oct 2026 on" in text_of(response)
        assert toasts(response) == ["Groceries has no budget from Oct 2026 on."]
        assert rows(db, groceries)[-1] == ("monthly", day("2026-10-01"), None, False)
        # The spending now shows as unbudgeted (in the out-of-band card and on the next load).
        assert f'<tr id="unbudgeted-{groceries.id}">' in response.text
        assert [u.category.name for u in client.get("/budgets").context["ov"].unbudgeted] == [
            "Groceries"
        ]

    def test_a_plain_form_post_redirects_back_with_a_toast(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "700", "period": "2026-09-01"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/budgets?period=2026-09-01"
        page = client.get(response.headers["location"])
        assert toasts(page) == ["Groceries has a monthly budget of $700.00 from Sep 2026 on."]

    def test_a_plain_form_post_with_an_error_shows_the_page(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "-1", "period": OCT_1},
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert response.template.name == "budgets/index.html"
        assert "A budget can't be negative." in html.unescape(response.text)

    def test_an_unknown_category_is_a_404(self, client):
        response = client.post(
            "/budgets/category/999999999", data={"amount": "1", "period": OCT_1}, headers=HTMX
        )
        assert response.status_code == 404

    def test_a_category_that_is_not_spending_is_refused(self, client, db):
        payments = card_payments(db)
        response = client.post(
            f"/budgets/category/{payments.id}",
            data={"amount": "100", "period": OCT_1, "row": "unbudgeted"},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert "Card Payments isn't a spending category" in html.unescape(response.text)

    def test_a_malformed_period_field_is_a_400(self, client, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}",
            data={"amount": "1", "period": "yesterday"},
            headers=HTMX,
        )
        assert response.status_code == 400


class TestRollover:
    def test_turning_rollover_on_and_off(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}/rollover",
            data={"enabled": "1", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 200
        row = row_of(response, "line", groceries)
        assert re.search(rf'id="rollover-{groceries.id}" name="enabled" value="1" checked', row)
        assert toasts(response) == [
            "Rollover is on for Groceries from Oct 2026: what's left carries into the next month."
        ]
        assert rows(db, groceries)[-1] == ("monthly", day("2026-10-01"), D("500.00"), True)
        response = client.post(
            f"/budgets/category/{groceries.id}/rollover", data={"period": OCT_1}, headers=HTMX
        )
        assert toasts(response) == ["Rollover is off for Groceries from Oct 2026."]
        assert rows(db, groceries)[-1][3] is False
        assert csp_problems(response.text, rendered=True) == []

    def test_the_carry_shows_after_turning_it_on(self, client, db, cat, spend):
        home = cat("Home")
        make_budget(db, home, "100.00", effective_from=day("2026-09-01"))
        spend(home, "70.00", "2026-09-10")
        client.post(
            f"/budgets/category/{home.id}/rollover",
            data={"enabled": "1", "period": "2026-09-01"},
            headers=HTMX,
        )
        assert "+$30.00 from Sep" in text_of(client.get("/budgets"))

    def test_rollover_needs_a_budget(self, client, cat):
        response = client.post(
            f"/budgets/category/{cat('Home').id}/rollover",
            data={"enabled": "1", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 409

    def test_a_plain_post_redirects(self, client, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}/rollover",
            data={"enabled": "1", "period": OCT_1},
            follow_redirects=False,
        )
        assert response.status_code == 303


# --------------------------------------------------------------------------- the overall target


class TestOverallTarget:
    def test_setting_and_removing_the_target(self, client, db, groceries):
        response = client.post(
            "/budgets/overall", data={"amount": "3,000", "period": OCT_1}, headers=HTMX
        )
        assert response.status_code == 200
        assert response.template.name == "budgets/_overall.html"
        assert response.text.startswith(
            '<section class="card budget-overall" id="overall-card" '
            'aria-labelledby="overall-card-title">'
        )
        assert "of $3,000.00 target" in text_of(response)
        assert toasts(response) == ["The overall target is $3,000.00 from Oct 2026 on."]
        assert rows(db)[-1] == ("monthly", day("2026-10-01"), D("3000.00"), False)
        response = client.post(
            "/budgets/overall", data={"amount": "", "period": OCT_1}, headers=HTMX
        )
        assert toasts(response) == ["No overall target from Oct 2026 on."]
        assert "of $500.00 budgeted" in text_of(response)  # the monthly budgets added up
        assert csp_problems(response.text, rendered=True) == []

    def test_an_invalid_target(self, client, db):
        response = client.post(
            "/budgets/overall", data={"amount": "-10", "period": OCT_1}, headers=HTMX
        )
        assert response.status_code == 422
        assert "A budget can't be negative." in html.unescape(response.text)
        assert 'id="overall-amount"' in response.text and 'aria-invalid="true"' in response.text
        assert rows(db) == []

    def test_a_plain_post_redirects(self, client):
        response = client.post(
            "/budgets/overall", data={"amount": "100", "period": OCT_1}, follow_redirects=False
        )
        assert response.status_code == 303 and response.headers["location"] == (
            "/budgets?period=2026-10-01"
        )


# --------------------------------------------------------------------------- annual budgets


class TestAnnualEdits:
    @pytest.fixture
    def travel(self, db, cat, spend):
        travel = cat("Travel")
        make_budget(db, travel, "6000.00", effective_from=day("2026-01-01"), period="annual")
        make_budget(db, travel, None, effective_from=day("2026-01-01"))
        spend(travel, "1200.00", "2026-03-10")
        return travel

    def test_the_annual_row(self, client, travel):
        response = client.get("/budgets")
        row = row_of(response, "annual", travel)
        assert 'value="6000.00"' in row and "$1,200.00" in row
        href = f"/transactions?category={travel.id}&amp;from=2026-01-01&amp;to=2026-10-31"
        assert f'href="{href}"' in row
        assert "On an even pace: $4,734.25 by now" in text_of(response)  # 6000 × 288 / 365
        assert 'aria-valuetext="20% used, 79% of the period elapsed"' in row

    def test_editing_an_annual_amount(self, client, db, travel):
        response = client.post(
            f"/budgets/category/{travel.id}/annual",
            data={"amount": "7000", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.template.name == "budgets/_annual_update.html"
        assert response.text.lstrip().startswith(f'<tr id="annual-{travel.id}">')
        assert '<tfoot id="annual-totals" hx-swap-oob="true">' in response.text
        assert toasts(response) == ["Travel has an annual budget of $7,000.00 from Oct 2026 on."]
        assert csp_problems(response.text, rendered=True) == []
        # September (as of its end) still shows the old amount.
        september = client.get("/budgets?period=2026-09-01").context["annual"]
        assert september.lines[0].budget == D("6000.00")

    def test_an_invalid_annual_amount(self, client, db, travel):
        response = client.post(
            f"/budgets/category/{travel.id}/annual",
            data={"amount": "1.001", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert "two decimal places" in response.text

    def test_switching_a_monthly_budget_to_annual_reloads_the_page(self, client, db, groceries):
        response = client.post(
            f"/budgets/category/{groceries.id}/annual",
            data={"amount": "6000", "period": OCT_1},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/budgets?period=2026-10-01"
        assert rows(db, groceries)[-2:] == [
            ("annual", day("2026-10-01"), D("6000.00"), False),
            ("monthly", day("2026-10-01"), None, False),
        ]
        page = client.get("/budgets")
        assert toasts(page) == [
            "Groceries has an annual budget of $6,000.00 from Oct 2026 on. Its monthly budget "
            "ends then."
        ]
        assert page.context["ov"].lines == ()


# --------------------------------------------------------------------------- adding budgets


class TestAdding:
    def test_add_budget_from_an_unbudgeted_row(self, client, db, cat, spend):
        dining = cat("Dining & Drinks")
        spend(dining, "80.00", "2026-10-05")
        page = client.get("/budgets")
        assert f'<tr id="unbudgeted-{dining.id}">' in page.text
        response = client.post(
            f"/budgets/category/{dining.id}",
            data={"amount": "300", "period": OCT_1, "row": "unbudgeted"},
            headers=HTMX,
        )
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/budgets?period=2026-10-01"
        ov = client.get("/budgets").context["ov"]
        assert [line.category.name for line in ov.lines] == ["Dining & Drinks"]
        assert ov.unbudgeted == ()

    def test_add_budget_needs_an_amount(self, client, db, cat, spend):
        dining = cat("Dining & Drinks")
        spend(dining, "80.00", "2026-10-05")
        response = client.post(
            f"/budgets/category/{dining.id}",
            data={"amount": " ", "period": OCT_1, "row": "unbudgeted"},
            headers=HTMX,
        )
        assert response.status_code == 422
        assert response.template.name == "budgets/_unbudgeted_row.html"
        assert response.text.lstrip().startswith(f'<tr id="unbudgeted-{dining.id}">')
        assert "Enter an amount, like 250." in response.text
        assert "$80.00" in response.text
        assert rows(db, dining) == []

    def test_the_add_dialogs(self, client, db, groceries):
        response = client.get("/budgets")
        for kind in ("monthly", "annual"):
            dialog = re.search(rf'<dialog id="add-{kind}".*?</dialog>', response.text, re.S)
            assert dialog is not None
            assert f'name="kind" value="{kind}"' in dialog.group(0)
        options = re.search(
            r'<select[^>]*id="add-monthly-category".*?</select>', response.text, re.S
        )
        assert "Groceries (now $500.00 a month)" in html.unescape(options.group(0))
        assert "Card Payments" not in options.group(0)

    def test_adding_a_monthly_budget(self, client, db, cat):
        home = cat("Home")
        response = client.post(
            "/budgets/add",
            data={"category_id": str(home.id), "kind": "monthly", "amount": "150", "period": OCT_1},
            headers=HTMX,
        )
        assert response.headers["HX-Redirect"] == "/budgets?period=2026-10-01"
        assert rows(db, home) == [("monthly", day("2026-10-01"), D("150.00"), False)]
        page = client.get("/budgets")
        assert toasts(page) == ["Home has a monthly budget of $150.00 from Oct 2026 on."]

    def test_adding_an_annual_budget(self, client, db, cat):
        gifts = cat("Gifts & Donations")
        response = client.post(
            "/budgets/add",
            data={"category_id": str(gifts.id), "kind": "annual", "amount": "800", "period": OCT_1},
            follow_redirects=False,
        )
        assert response.status_code == 303
        annual = client.get("/budgets").context["annual"]
        assert [(a.category.name, a.budget) for a in annual.lines] == [
            ("Gifts & Donations", D("800.00"))
        ]

    @pytest.mark.parametrize(
        ("data", "errors"),
        [
            ({"category_id": "", "amount": "10"}, ["Choose a category."]),
            ({"amount": ""}, ["Enter an amount, like 250."]),
            ({"amount": "-3"}, ["A budget can't be negative. Leave it empty for no budget."]),
            (
                {"category_id": "", "amount": ""},
                ["Choose a category.", "Enter an amount, like 250."],
            ),
        ],
    )
    def test_dialog_errors(self, client, db, cat, data, errors):
        form = {"category_id": str(cat("Home").id), "kind": "monthly", "period": OCT_1}
        form.update(data)
        response = client.post("/budgets/add", data=form, headers=HTMX)
        assert response.status_code == 422
        assert response.template.name == "budgets/_add_form.html"
        assert response.text.lstrip().startswith('<form class="form" id="add-monthly-form"')
        found = [
            " ".join(html.unescape(m).split())
            for m in re.findall(
                r'<p class="field__error"[^>]*>.*?<span>(.*?)</span>', response.text, re.S
            )
        ]
        assert found == errors
        assert rows(db) == []
        assert csp_problems(response.text, rendered=True) == []

    def test_a_plain_dialog_post_with_errors_reopens_the_dialog(self, client, cat):
        response = client.post(
            "/budgets/add",
            data={
                "category_id": str(cat("Home").id),
                "kind": "annual",
                "amount": "x",
                "period": OCT_1,
            },
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert re.search(r'<dialog id="add-annual"[^>]*data-dialog-autoopen', response.text)
        assert not re.search(r'<dialog id="add-monthly"[^>]*data-dialog-autoopen', response.text)

    def test_only_spending_categories_can_be_added(self, client, db):
        payments = card_payments(db)
        response = client.post(
            "/budgets/add",
            data={
                "category_id": str(payments.id),
                "kind": "monthly",
                "amount": "5",
                "period": OCT_1,
            },
            headers=HTMX,
        )
        assert response.status_code == 422
        assert "Choose a category." in response.text
