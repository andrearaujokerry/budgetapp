"""Statements pages (INS-4, REL-3, Gate D): the list of periods and one period's detail."""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from budgetapp.models import Statement
from tests.factories import make_account, make_statement, make_transaction
from tests.web.csp import csp_problems

D = dt.date
TODAY = D(2026, 10, 29)


def toasts(page: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', page)]


def text_of(page: str) -> str:
    """The page's visible text, whitespace collapsed (tags removed, entities decoded)."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


@pytest.fixture
def today(freeze_today):
    return freeze_today(TODAY)


@pytest.fixture
def card(db):
    return make_account(db, name="Blue Cash Preferred", mask="3005", statement_closing_day=27)


@pytest.fixture
def periods(db, today, card):
    """Two statements (Aug 27, Sep 27) whose September activity is $407.68."""
    snapshot = {"closing_date_source": "liabilities", "reported_new_balance_source": "liabilities"}
    make_statement(db, card, "2026-08-27", reported_new_balance=Decimal("1000.00"), **snapshot)
    september = make_statement(
        db, card, "2026-09-27", reported_new_balance=Decimal("1407.68"), **snapshot
    )
    make_transaction(db, card, amount="1387.84", date=D(2026, 9, 2), merchant_name="Best Buy")
    make_transaction(db, card, amount="-1000.00", date=D(2026, 9, 21), transaction_code="payment")
    make_transaction(
        db,
        card,
        amount="19.84",
        date=D(2026, 9, 27),
        transaction_code="interest",
        name="INTEREST CHARGE ON PURCHASES",
    )
    make_transaction(db, card, amount="12.00", date=D(2026, 9, 28), merchant_name="Late Cafe")
    return september


def url(card, closing: str) -> str:
    return f"/statements/{card.id}/{closing}"


# --------------------------------------------------------------------------- list


class TestList:
    def test_an_empty_database_says_how_to_start(self, client):
        response = client.get("/statements")
        assert response.status_code == 200
        assert response.template.name == "statements/index.html"
        assert "No card yet" in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_a_card_without_dates_offers_to_add_a_statement(self, client, db, today):
        make_account(db)
        page = client.get("/statements").text
        assert "No statement dates yet" in page
        assert '<form class="stmt-add" method="post" action="/statements">' in page

    def test_every_period_newest_first(self, client, db, card, periods):
        response = client.get("/statements")
        assert response.status_code == 200
        rows = response.context["rows"]
        assert [r.cycle.closing_date for r in rows] == [
            D(2026, 11, 27),
            D(2026, 10, 27),
            D(2026, 9, 27),
            D(2026, 8, 27),
        ]
        assert [r.cycle.status for r in rows] == ["open", "settling", "final", "final"]
        assert [r.check.state for r in rows] == ["open", "unavailable", "pass", "unavailable"]
        assert [r.check.missing for r in rows] == [None, "new_balance", None, "previous_balance"]
        assert response.context["current"].cycle.closing_date == D(2026, 11, 27)
        assert response.context["latest"].cycle.closing_date == D(2026, 10, 27)
        page = response.text
        assert f'href="{url(card, "2026-09-27")}"' in page
        assert "Sep 28 – Oct 27, 2026" in page
        text = text_of(page)
        assert "Statements that match 1 of 1 All to the cent" in text
        assert "Still open" in text and "Matches so far" not in text
        assert "No New Balance" in text and "No Previous Balance" in text
        assert "doesn't match" not in text
        assert csp_problems(page, rendered=True) == []

    def test_a_one_cent_mismatch_is_announced(self, client, db, card, periods):
        periods.reported_new_balance = Decimal("1407.69")
        db.flush()
        page = client.get("/statements").text
        text = text_of(page)
        assert "1 statement doesn't match your Amex statement." in text
        assert "The period that closed Sep 27, 2026 is off by $0.01." in text
        assert "Off by $0.01" in text
        assert f'href="{url(card, "2026-09-27")}"' in page
        assert "Statements that match 0 of 1 1 statement off" in text

    def test_one_view_per_card(self, client, db, today, card):
        other = make_account(db, name="Gold Card", mask="1009", statement_closing_day=12)
        hidden = make_account(db, name="Old Card", mask="7777", is_hidden=True)
        make_transaction(db, card, amount="10.00", date=D(2026, 10, 3))
        make_transaction(db, other, amount="700.00", date=D(2026, 10, 3))
        page = client.get("/statements").text
        assert f'href="/statements?account={other.id}"' in page
        assert f'href="/statements?account={hidden.id}"' not in page
        first = client.get("/statements").context
        assert first["account"].id == card.id
        assert [r.net_activity for r in first["rows"]] == [Decimal("0.00"), Decimal("10.00")]
        second = client.get(f"/statements?account={other.id}").context
        assert second["account"].id == other.id
        assert second["rows"][0].cycle.closing_date == D(2026, 11, 12)
        assert second["rows"][1].net_activity == Decimal("700.00")
        shown = client.get(f"/statements?account={hidden.id}")
        assert shown.status_code == 200
        assert f'href="/statements?account={hidden.id}"' in shown.text

    def test_unknown_or_malformed_account(self, client, today, card):
        assert client.get("/statements?account=999999").status_code == 404
        assert client.get("/statements?account=abc").status_code == 400


# --------------------------------------------------------------------------- detail


class TestDetail:
    def test_the_summary_box_and_the_check(self, client, db, card, periods):
        response = client.get(url(card, "2026-09-27"))
        assert response.status_code == 200
        assert response.template.name == "statements/detail.html"
        summary = response.context["summary"]
        assert summary.net_activity == Decimal("407.68")
        page = response.text
        text = text_of(page)
        assert "Statement · Aug 28 – Sep 27, 2026" in text
        assert "Matches your statement to the cent." in text
        assert (
            "The app's activity for this period, $407.68, equals your New Balance of $1,407.68 "
            "minus the previous statement's New Balance, $1,000.00."
        ) in text
        box = page.split('id="summary"')[1].split("</table>")[0]
        box_text = text_of(box)
        assert "Previous Balance $1,000.00 —" in box_text
        assert "Payments/Credits −$1,000.00 —" in box_text
        assert "New Charges +$1,387.84 —" in box_text
        assert "Interest Charged +$19.84 —" in box_text
        assert "New Balance $1,407.68 $1,407.68 Matches" in box_text
        assert csp_problems(page, rendered=True) == []

    def test_sections_boundaries_and_navigation(self, client, db, card, periods):
        page = client.get(url(card, "2026-09-27")).text
        assert 'id="bucket-payments"' in page
        assert 'id="bucket-new-charges"' in page
        assert 'id="bucket-interest"' in page
        assert 'id="bucket-credits"' not in page
        boundaries = text_of(page.split('id="boundaries"')[1].split('id="transactions"')[0])
        assert "End of the period · closes Sep 27, 2026" in boundaries
        assert "Late Cafe" in boundaries and "Next" in boundaries
        assert "Closing date: Sep 27, 2026" in boundaries
        assert f'href="{url(card, "2026-08-27")}"' in page  # Earlier
        assert f'href="{url(card, "2026-10-27")}"' in page  # Later
        assert f'href="/statements?account={card.id}"' in page  # back
        assert "data-print" in page
        assert '<script src="/static/js/statements.js" defer></script>' in page

    def test_a_one_cent_difference_in_plain_words(self, client, db, card, periods):
        periods.reported_new_balance = Decimal("1407.69")
        db.flush()
        text = text_of(client.get(url(card, "2026-09-27")).text)
        assert "Off by $0.01." in text
        assert "Your statement says $407.69" in text
        assert "The app has $0.01 less." in text

    def test_typed_numbers_that_dont_add_up(self, client, db, card, periods):
        periods.reported_previous_balance = Decimal("1000.00")
        periods.reported_payments_credits = Decimal("1000.00")
        periods.reported_new_charges = Decimal("1387.84")
        periods.reported_fees = Decimal("0.00")
        periods.reported_interest = Decimal("91.84")
        db.flush()
        text = text_of(client.get(url(card, "2026-09-27")).text)
        assert "The statement's numbers don't add up." in text
        assert "comes to $1,479.68, but the New Balance typed is $1,407.68" in text
        assert "Interest Charged +$19.84 +$91.84 −$72.00" in text

    def test_an_open_period(self, client, db, card, periods):
        text = text_of(client.get(url(card, "2026-11-27")).text)
        assert "This period is still open." in text
        assert "Mark as checked" not in text

    def test_a_settling_period_is_provisional(self, client, db, card, periods, freeze_today):
        freeze_today(D(2026, 9, 29))
        text = text_of(client.get(url(card, "2026-09-27")).text)
        assert "Matches your statement so far." in text
        assert "Late transactions can still arrive until Oct 1, 2026" in text

    def test_a_long_period_is_flagged(self, client, db, today, card):
        make_statement(db, card, "2026-07-27")
        make_statement(db, card, "2026-09-27")
        text = text_of(client.get(url(card, "2026-09-27")).text)
        assert "This period is 62 days long." in text

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("2026-09-20", 404),  # no period closes that day
            ("2026-12-27", 404),  # after the open period
            ("not-a-date", 400),
        ],
    )
    def test_unknown_periods(self, client, db, card, periods, path, status):
        assert client.get(url(card, path)).status_code == status

    def test_unknown_account(self, client, today):
        assert client.get("/statements/999999/2026-09-27").status_code == 404


# --------------------------------------------------------------------------- forms


class TestAddStatement:
    def test_adds_and_opens_it(self, client, db, today, card):
        response = client.post(
            "/statements",
            data={"account_id": str(card.id), "closing_date": "2026-09-26"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-09-26")
        row = db.scalars(select(Statement)).one()
        assert (row.closing_date, row.closing_date_source) == (D(2026, 9, 26), "user")
        assert toasts(client.get(response.headers["location"]).text) == [
            "Statement closing Sep 26, 2026 added"
        ]

    def test_a_refused_date_is_shown_with_the_form(self, client, db, card, periods):
        response = client.post(
            "/statements", data={"account_id": str(card.id), "closing_date": "2026-09-30"}
        )
        assert response.status_code == 422
        assert response.template.name == "statements/index.html"
        text = text_of(response.text)
        assert "That is 3 days from the statement that closes on Sep 27, 2026" in text
        assert 'value="2026-09-30"' in response.text
        assert csp_problems(response.text, rendered=True) == []
        assert len(db.scalars(select(Statement)).all()) == 2

    @pytest.mark.parametrize("account_id", ["", "abc", "999999"])
    def test_unknown_account(self, client, today, account_id):
        response = client.post(
            "/statements", data={"account_id": account_id, "closing_date": "2026-09-26"}
        )
        assert response.status_code == 404


class TestSaveStatement:
    def test_saves_the_numbers_as_printed(self, client, db, card, periods):
        form = {
            "closing_date": "2026-09-27",
            "previous_balance": "1,000.00",
            "payments_credits": "−$1,000.00",
            "new_charges": "1387.84",
            "fees": "0",
            "interest": "19.84",
            "new_balance": "$1,407.68",
            "notes": "From the PDF",
        }
        response = client.post(url(card, "2026-09-27"), data=form, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-09-27")
        db.refresh(periods)
        assert periods.reported_payments_credits == Decimal("1000.00")
        assert (periods.reported_new_balance, periods.reported_new_balance_source) == (
            Decimal("1407.68"),
            "liabilities",  # the same value as the snapshot keeps its source
        )
        assert periods.closing_date_source == "liabilities"  # the date was not changed
        page = client.get(url(card, "2026-09-27")).text
        assert toasts(page) == ["Statement saved"]
        assert 'value="-1000.00"' in page  # shown with its minus, as printed
        text = text_of(page)
        assert "Payments/Credits −$1,000.00 −$1,000.00 Matches" in text
        assert "From the PDF" in text

    def test_moving_the_closing_date_follows_it(self, client, db, card, periods):
        response = client.post(
            url(card, "2026-09-27"), data={"closing_date": "2026-09-28"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-09-28")
        db.refresh(periods)
        assert (periods.closing_date, periods.closing_date_source) == (D(2026, 9, 28), "user")
        assert client.get(url(card, "2026-09-27")).status_code == 404
        moved = client.get(url(card, "2026-09-28")).context["summary"]
        assert moved.net_activity == Decimal("419.68")  # the Sep 28 coffee moved in

    def test_errors_keep_what_was_typed_and_save_nothing(self, client, db, card, periods):
        response = client.post(
            url(card, "2026-09-27"),
            data={"closing_date": "2026-09-27", "fees": "ten", "new_balance": "1.234"},
        )
        assert response.status_code == 422
        assert response.template.name == "statements/detail.html"
        page = response.text
        assert 'value="ten"' in page and 'value="1.234"' in page
        assert 'aria-invalid="true"' in page
        assert csp_problems(page, rendered=True) == []
        db.refresh(periods)
        assert periods.reported_fees is None
        assert periods.reported_new_balance == Decimal("1407.68")

    def test_a_generated_period_gets_a_statement(self, client, db, card, periods):
        response = client.post(
            url(card, "2026-10-27"),
            data={"closing_date": "2026-10-27", "new_balance": "1419.68"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        row = db.scalars(select(Statement).where(Statement.closing_date == D(2026, 10, 27))).one()
        assert (row.closing_date_source, row.reported_new_balance) == ("user", Decimal("1419.68"))

    def test_the_earliest_statement_can_set_its_period_start(self, client, db, today, card):
        first = make_statement(db, card, "2026-09-27")
        page = client.get(url(card, "2026-09-27")).text
        assert 'name="period_start"' in page
        response = client.post(
            url(card, "2026-09-27"),
            data={"closing_date": "2026-09-27", "period_start": "2026-08-30"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        db.refresh(first)
        assert first.period_start_override == D(2026, 8, 30)
        assert 'name="period_start"' not in client.get(url(card, "2026-10-27")).text

    def test_unknown_period(self, client, db, card, periods):
        response = client.post(url(card, "2026-09-20"), data={"closing_date": "2026-09-20"})
        assert response.status_code == 404


class TestMarkChecked:
    def test_mark_and_unmark(self, client, db, card, periods, freeze_today):
        freeze_today(now=dt.datetime(2026, 10, 29, 18, 0, tzinfo=dt.UTC))
        response = client.post(
            f"{url(card, '2026-09-27')}/checked", data={"checked": "1"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-09-27")
        db.refresh(periods)
        assert periods.checked_at is not None
        page = client.get(url(card, "2026-09-27")).text
        assert toasts(page) == ["Marked as checked"]
        assert "You checked this statement on" in page
        client.post(f"{url(card, '2026-09-27')}/checked", data={"checked": "0"})
        db.refresh(periods)
        assert periods.checked_at is None

    def test_unknown_period(self, client, db, card, periods):
        response = client.post(f"{url(card, '2026-09-20')}/checked", data={"checked": "1"})
        assert response.status_code == 404


# --------------------------------------------------------------------------- review fixes


class TestImplausibleDates:
    """Review finding: a year-1 period start was accepted and then every page gave a 500."""

    def test_a_year_typo_in_the_period_start_is_refused(self, client, db, today, card):
        make_statement(db, card, "2026-08-27")
        make_statement(db, card, "2026-09-27")
        response = client.post(
            url(card, "2026-08-27"),
            data={"closing_date": "2026-08-27", "period_start": "0001-01-01"},
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert "within two months before the closing date" in text_of(response.text)
        assert csp_problems(response.text, rendered=True) == []
        assert client.get("/statements").status_code == 200
        assert client.get(url(card, "2026-08-27")).status_code == 200

    @pytest.mark.parametrize("closing", ["9999-12-27", "0001-01-27", "1999-12-27"])
    def test_far_dates_are_not_found(self, client, db, today, card, closing):
        assert client.get(url(card, closing)).status_code == 404
        response = client.post(url(card, closing), data={"closing_date": closing})
        assert response.status_code == 404


class TestOpenEstimate:
    """Review finding: saving on the open period pinned its estimated closing date."""

    def test_the_open_estimate_has_no_form(self, client, db, card, periods):
        page = client.get(url(card, "2026-11-27")).text
        text = text_of(page)
        assert "You can type this statement's numbers once it closes, around Nov 27, 2026" in text
        assert "Save statement" not in text and 'name="closing_date"' not in page
        assert csp_problems(page, rendered=True) == []

    def test_a_crafted_post_saves_nothing(self, client, db, card, periods):
        response = client.post(
            url(card, "2026-11-27"),
            data={"closing_date": "2026-11-27", "notes": "expect the REI refund"},
            follow_redirects=False,
        )
        assert response.status_code == 422
        assert "Nothing was saved. This period hasn't closed yet." in text_of(response.text)
        response = client.post(
            f"{url(card, '2026-11-27')}/checked", data={"checked": "1"}, follow_redirects=False
        )
        assert response.status_code == 303
        page = client.get(response.headers["location"]).text
        assert toasts(page) == [
            "This period hasn't closed yet. Type its numbers once Amex issues the statement "
            "(it should close on Nov 27, 2026)."
        ]
        closings = db.scalars(select(Statement.closing_date).order_by("closing_date")).all()
        assert closings == [D(2026, 8, 27), D(2026, 9, 27)]


class TestRemoveAndCombine:
    """Review finding: a mistaken closing date could not be undone."""

    def test_remove_a_statement_the_user_added(self, client, db, card, periods):
        client.post("/statements", data={"account_id": str(card.id), "closing_date": "2026-09-10"})
        page = client.get(url(card, "2026-09-10")).text
        assert "Remove statement" in page
        assert 'data-confirm="Remove the statement closing Sep 10, 2026?' in page
        assert csp_problems(page, rendered=True) == []
        assert "Remove statement" not in client.get(url(card, "2026-09-27")).text  # from Amex
        response = client.post(f"{url(card, '2026-09-10')}/remove", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/statements?account={card.id}"
        assert toasts(client.get(response.headers["location"]).text) == [
            "Statement closing Sep 10, 2026 removed"
        ]
        closings = db.scalars(select(Statement.closing_date).order_by("closing_date")).all()
        assert closings == [D(2026, 8, 27), D(2026, 9, 27)]

    def test_amex_statements_stay(self, client, db, card, periods):
        periods.plaid_issue_date = D(2026, 9, 27)
        db.flush()
        response = client.post(f"{url(card, '2026-09-27')}/remove", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-09-27")
        assert "came from Amex" in toasts(client.get(url(card, "2026-09-27")).text)[0]
        assert db.get(Statement, periods.id) is not None
        assert client.post(f"{url(card, '2026-10-27')}/remove").status_code == 404  # estimated

    def test_moving_a_typo_onto_the_amex_date_combines_them(self, client, db, today, card):
        make_statement(db, card, "2026-09-28", closing_date_source="liabilities")
        client.post("/statements", data={"account_id": str(card.id), "closing_date": "2026-10-17"})
        make_statement(
            db,
            card,
            "2026-10-27",
            closing_date_source="liabilities",
            plaid_issue_date=D(2026, 10, 27),
            reported_new_balance=Decimal("150.00"),
            reported_new_balance_source="liabilities",
        )
        response = client.post(
            url(card, "2026-10-17"),
            data={"closing_date": "2026-10-27", "previous_balance": "100.00", "new_balance": ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == url(card, "2026-10-27")
        page = client.get(url(card, "2026-10-27")).text
        assert toasts(page) == [
            "Statement saved and combined with the one Amex reported for Oct 27, 2026"
        ]
        assert "Sep 29 – Oct 27, 2026" in text_of(page)
        assert client.get(url(card, "2026-10-17")).status_code == 404
