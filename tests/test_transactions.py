"""services.transactions: the filter, search, sums, pagination and CSV export (TXN-1, TXN-5)."""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from starlette.datastructures import QueryParams

from budgetapp.services import transactions as txns
from budgetapp.services.transactions import TransactionFilter
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_category,
    make_split,
    make_statement,
    make_transaction,
    set_app_settings,
    uncategorized,
)

TODAY = dt.date(2026, 10, 29)
D = dt.date.fromisoformat


def F(**params) -> TransactionFilter:
    return TransactionFilter.from_params(params)


def names(page) -> list[str]:
    return [row.name for row in page.rows]


def run(db, filt=None, **kwargs):
    return txns.search(db, filt or TransactionFilter(), today=TODAY, **kwargs)


# --------------------------------------------------------------------------- the filter


class TestFilterParsing:
    def test_defaults_show_everything_counted(self):
        filt = TransactionFilter.from_params({})
        assert filt == TransactionFilter()
        assert filt.is_default and filt.to_params() == [] and filt.problems == ()

    def test_round_trip_through_query_params(self):
        params = QueryParams(
            "q=coffee&range=custom&from=2026-09-01&to=2026-09-30&category=3&category=uncategorized"
            "&merchant=Uber&min=10&max=250.5&status=pending&account=7&tag=work&member=SAM+RIVERA"
            "&credits=1&show=review"
        )
        filt = TransactionFilter.from_params(params)
        assert filt.q == "coffee"
        assert (filt.range, filt.date_from, filt.date_to) == (
            "custom",
            D("2026-09-01"),
            D("2026-09-30"),
        )
        assert filt.categories == ("3", "uncategorized")
        assert (filt.amount_min, filt.amount_max) == (Decimal("10.00"), Decimal("250.50"))
        assert (filt.status, filt.account_id, filt.tag, filt.member) == (
            "pending",
            7,
            "work",
            "SAM RIVERA",
        )
        assert filt.credits_only and filt.show == "review"
        assert TransactionFilter.from_params(filt.to_params()) == filt
        assert filt.advanced_count == 7

    def test_bad_values_are_dropped_and_explained(self):
        filt = F(**{"from": "2026-13-01", "min": "ten", "range": "fortnight", "status": "maybe"})
        assert filt.range == "all" and filt.date_from is None and filt.amount_min is None
        assert filt.status == "all"
        assert len(filt.problems) == 2
        assert "start date" in filt.problems[0] and "minimum amount" in filt.problems[1]

    def test_dates_without_a_preset_mean_a_custom_range(self):
        filt = F(**{"from": "2026-10-01"})
        assert filt.range == "custom" and filt.date_from == D("2026-10-01")

    def test_dates_are_ignored_for_other_presets_and_swapped_when_reversed(self):
        assert F(range="year", **{"from": "2026-10-01"}).date_from is None
        filt = F(range="custom", **{"from": "2026-10-31", "to": "2026-10-01"})
        assert (filt.date_from, filt.date_to) == (D("2026-10-01"), D("2026-10-31"))

    def test_amount_bounds_are_swapped_when_reversed(self):
        filt = F(min="$1,200", max="-5")
        assert (filt.amount_min, filt.amount_max) == (Decimal("-5.00"), Decimal("1200.00"))

    def test_category_tokens(self):
        params = QueryParams("category=007&category=Uncategorized&category=x&category=7&category=0")
        assert TransactionFilter.from_params(params).categories == ("7", "uncategorized")

    def test_page_and_sort(self):
        assert txns.parse_page("3") == 3
        assert [txns.parse_page(v) for v in ("0", "-2", "x", "", None)] == [1, 1, 1, 1, 1]
        assert txns.parse_sort("amount") == "amount"
        assert txns.parse_sort("name") == "-date"


# --------------------------------------------------------------------------- search


@pytest.fixture
def card(db):
    return make_account(db)


@pytest.fixture
def cats(db):
    return {
        "groceries": get_category(db, "Groceries"),
        "dining": get_category(db, "Dining & Drinks"),
        "shopping": get_category(db, "Shopping"),
        "home": get_category(db, "Home"),
        "payments": card_payments(db),
        "uncategorized": uncategorized(db),
    }


class TestOrder:
    def test_newest_first_by_authorized_date_then_id(self, db, card):
        a = make_transaction(db, card, name="A", date="2026-10-20", authorized_date="2026-10-18")
        b = make_transaction(db, card, name="B", date="2026-10-19")
        c = make_transaction(db, card, name="C", date="2026-10-19")
        page = run(db)
        assert names(page) == ["C", "B", "A"]
        assert page.rows[2].spend_date == D("2026-10-18") and a.id < b.id < c.id

    def test_oldest_first_and_by_amount(self, db, card):
        make_transaction(db, card, name="small", amount="5.00", date="2026-10-01")
        make_transaction(db, card, name="big", amount="500.00", date="2026-10-02")
        make_transaction(db, card, name="credit", amount="-20.00", date="2026-10-03")
        assert names(run(db, sort="date")) == ["small", "big", "credit"]
        assert names(run(db, sort="-amount")) == ["big", "small", "credit"]
        assert names(run(db, sort="amount")) == ["credit", "small", "big"]
        assert names(run(db, sort="nonsense")) == ["credit", "big", "small"]


class TestEachFilter:
    def test_text_search_covers_every_field_case_insensitively(self, db, card):
        make_transaction(db, card, name="BLUE BOTTLE COFFEE")
        make_transaction(db, card, name="X1", merchant_name="Blue Bottle")
        make_transaction(db, card, name="X2", original_description="SQ *BLUE BOTTLE OAKLAND")
        make_transaction(db, card, name="X3", notes="met Sam at blue bottle")
        make_transaction(db, card, name="X4", tags=["Blue-team"])
        make_transaction(db, card, name="UNRELATED")
        assert sorted(names(run(db, F(q="blue")))) == ["BLUE BOTTLE COFFEE", "X1", "X2", "X3", "X4"]

    def test_every_word_must_match_somewhere(self, db, card):
        make_transaction(db, card, name="BLUE BOTTLE", notes="with Sam")
        make_transaction(db, card, name="BLUE APRON")
        assert names(run(db, F(q="bottle   SAM"))) == ["BLUE BOTTLE"]

    def test_wildcards_are_literal(self, db, card):
        make_transaction(db, card, name="100% JUICE")
        make_transaction(db, card, name="ANYTHING")
        make_transaction(db, card, name="A_B")
        assert names(run(db, F(q="%"))) == ["100% JUICE"]
        assert names(run(db, F(q="_"))) == ["A_B"]

    def test_an_amount_word_matches_the_amount(self, db, card):
        make_transaction(db, card, name="REFUND", amount="-54.99")
        make_transaction(db, card, name="OTHER", amount="54.98")
        assert names(run(db, F(q="54.99"))) == ["REFUND"]
        assert names(run(db, F(q="$54.99"))) == ["REFUND"]

    def test_custom_dates_use_the_spend_date_inclusively(self, db, card):
        make_transaction(db, card, name="in-auth", date="2026-10-02", authorized_date="2026-09-30")
        make_transaction(db, card, name="first", date="2026-10-01")
        make_transaction(db, card, name="last", date="2026-10-31")
        make_transaction(db, card, name="after", date="2026-11-01")
        filt = F(range="custom", **{"from": "2026-10-01", "to": "2026-10-31"})
        assert names(run(db, filt)) == ["last", "first"]

    @pytest.mark.parametrize(
        ("preset", "start", "end"),
        [
            ("period", "2026-10-01", "2026-10-31"),
            ("last_month", "2026-09-01", "2026-09-30"),
            ("90d", "2026-08-01", None),
            ("year", "2026-01-01", "2026-12-31"),
            ("all", None, None),
        ],
    )
    def test_presets(self, db, preset, start, end):
        dates = txns.resolve_dates(db, F(range=preset), TODAY)
        assert dates.start == (D(start) if start else None)
        assert dates.end == (D(end) if end else None)
        assert dates.label

    def test_the_budget_period_follows_statement_mode(self, db, card):
        make_statement(db, card, "2026-09-27")
        make_statement(db, card, "2026-10-27")
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=card.id)
        dates = txns.resolve_dates(db, F(range="period"), TODAY)
        assert (dates.start, dates.end) == (D("2026-10-28"), D("2026-11-27"))
        assert dates.label.startswith("This statement period")
        dates = txns.resolve_dates(db, F(range="period"), D("2026-10-27"))
        assert (dates.start, dates.end) == (D("2026-09-28"), D("2026-10-27"))

    def test_statement_mode_without_cycles_falls_back_to_the_month(self, db, card):
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=card.id)
        dates = txns.resolve_dates(db, F(range="period"), TODAY)
        assert (dates.start, dates.end) == (D("2026-10-01"), D("2026-10-31"))

    def test_categories_including_uncategorized(self, db, card, cats):
        make_transaction(db, card, name="G", category=cats["groceries"])
        make_transaction(db, card, name="D", category=cats["dining"])
        make_transaction(db, card, name="U")
        groceries = str(cats["groceries"].id)
        assert names(run(db, F(category=groceries))) == ["G"]
        assert sorted(names(run(db, F(category=[groceries, "uncategorized"])))) == ["G", "U"]
        assert names(run(db, F(category=str(cats["uncategorized"].id)))) == ["U"]

    def test_a_split_row_matches_through_its_parts(self, db, card, cats):
        costco = make_transaction(
            db, card, name="COSTCO", amount="100.00", category=cats["shopping"]
        )
        make_split(db, costco, cats["groceries"], "60.00")
        make_split(db, costco, cats["home"], "40.00")
        assert names(run(db, F(category=str(cats["groceries"].id)))) == ["COSTCO"]
        assert names(run(db, F(category=str(cats["home"].id)))) == ["COSTCO"]
        assert names(run(db, F(category=str(cats["shopping"].id)))) == []

    def test_splits_that_do_not_add_up_fall_back_to_the_category(self, db, card, cats):
        odd = make_transaction(db, card, name="ODD", amount="100.00", category=cats["shopping"])
        make_split(db, odd, cats["groceries"], "60.00")
        make_split(db, odd, cats["home"], "39.99")
        assert names(run(db, F(category=str(cats["shopping"].id)))) == ["ODD"]
        assert names(run(db, F(category=str(cats["groceries"].id)))) == []
        row = run(db).rows[0]
        assert row.splits_mismatch and not row.is_split

    def test_merchant_is_an_exact_case_insensitive_match(self, db, card):
        make_transaction(db, card, name="UBER *TRIP", merchant_name="Uber")
        make_transaction(db, card, name="UBER EATS", merchant_name="Uber Eats")
        make_transaction(db, card, name="uber")
        assert sorted(names(run(db, F(merchant="UBER")))) == ["UBER *TRIP", "uber"]

    def test_amount_bounds_are_inclusive_on_the_signed_amount(self, db, card):
        for amount in ("-50.00", "9.99", "10.00", "100.00", "100.01"):
            make_transaction(db, card, name=amount, amount=amount)
        assert sorted(names(run(db, F(min="10", max="100")))) == ["10.00", "100.00"]
        assert names(run(db, F(max="-1"))) == ["-50.00"]

    def test_status(self, db, card):
        make_transaction(db, card, name="P", pending=True)
        make_transaction(db, card, name="S")
        assert names(run(db, F(status="pending"))) == ["P"]
        assert names(run(db, F(status="posted"))) == ["S"]

    def test_account(self, db, card):
        other = make_account(db, manual=True)
        make_transaction(db, card, name="linked")
        make_transaction(db, other, name="manual", source="csv")
        assert names(run(db, F(account=str(other.id)))) == ["manual"]

    def test_tag_is_case_insensitive(self, db, card):
        make_transaction(db, card, name="T1", tags=["Work", "travel"])
        make_transaction(db, card, name="T2", tags=["work"])
        make_transaction(db, card, name="T3", tags=["homework"])
        assert sorted(names(run(db, F(tag="WORK")))) == ["T1", "T2"]

    def test_card_member(self, db, card):
        make_transaction(db, card, name="A", account_owner="ALEX RIVERA")
        make_transaction(db, card, name="S", account_owner="SAM RIVERA")
        make_transaction(db, card, name="none")
        assert names(run(db, F(member="SAM RIVERA"))) == ["S"]

    def test_credits_only(self, db, card, cats):
        make_transaction(db, card, name="charge", amount="10.00")
        make_transaction(db, card, name="refund", amount="-10.00")
        make_transaction(db, card, name="payment", amount="-500.00", category=cats["payments"])
        assert sorted(names(run(db, F(credits="1")))) == ["payment", "refund"]

    def test_suspected_duplicates_only_under_their_own_view(self, db, card):
        make_transaction(db, card, name="real")
        make_transaction(db, card, name="dupe", source="csv", duplicate_status="suspect")
        make_transaction(db, card, name="kept", source="csv", duplicate_status="kept")
        assert sorted(names(run(db))) == ["kept", "real"]
        assert names(run(db, F(show="duplicates"))) == ["dupe"]
        assert names(run(db, F(q="dupe"))) == []

    def test_needs_review_view(self, db, card):
        make_transaction(db, card, name="flagged", needs_review=True, review_reason="Check it")
        make_transaction(db, card, name="fine")
        make_transaction(
            db,
            card,
            name="flagged dupe",
            source="csv",
            duplicate_status="suspect",
            needs_review=True,
        )
        page = run(db, F(show="review"))
        assert names(page) == ["flagged"] and page.rows[0].review_reason == "Check it"
        assert txns.attention_counts(db) == txns.AttentionCounts(needs_review=1, duplicates=1)


class TestCombined:
    def test_filters_combine_with_and(self, db, card, cats):
        make_transaction(
            db,
            card,
            name="UBER A",
            merchant_name="Uber",
            amount="25.00",
            date="2026-10-10",
            category=cats["dining"],
            tags=["work"],
        )
        make_transaction(
            db,
            card,
            name="UBER B",
            merchant_name="Uber",
            amount="25.00",
            date="2026-09-10",
            category=cats["dining"],
            tags=["work"],
        )
        make_transaction(
            db,
            card,
            name="UBER C",
            merchant_name="Uber",
            amount="5.00",
            date="2026-10-11",
            category=cats["dining"],
            tags=["work"],
        )
        make_transaction(
            db,
            card,
            name="UBER D",
            merchant_name="Uber",
            amount="25.00",
            date="2026-10-12",
            category=cats["shopping"],
            tags=["work"],
        )
        make_transaction(
            db,
            card,
            name="UBER E",
            merchant_name="Uber",
            amount="25.00",
            date="2026-10-13",
            category=cats["dining"],
            pending=True,
            tags=["work"],
        )
        make_transaction(
            db,
            card,
            name="UBER F",
            merchant_name="Uber",
            amount="25.00",
            date="2026-10-14",
            category=cats["dining"],
        )
        filt = F(
            q="uber",
            range="period",
            category=str(cats["dining"].id),
            min="10",
            status="posted",
            tag="work",
            merchant="uber",
        )
        assert names(run(db, filt)) == ["UBER A"]


class TestSumsAndPages:
    def test_sums_are_raw_sums_of_every_matching_row(self, db, card, cats):
        make_transaction(db, card, name="a", amount="100.00")
        make_transaction(db, card, name="b", amount="0.10", pending=True)
        make_transaction(db, card, name="refund", amount="-20.00", category=cats["groceries"])
        make_transaction(db, card, name="payment", amount="-1000.00", category=cats["payments"])
        make_transaction(
            db, card, name="dupe", amount="999.00", source="csv", duplicate_status="suspect"
        )
        page = run(db, per_page=2)
        assert page.total_count == 4 and len(page.rows) == 2
        assert page.sums == txns.Sums(Decimal("100.10"), Decimal("-1020.00"), Decimal("-919.90"))
        dupes = run(db, F(show="duplicates"))
        assert dupes.sums == txns.Sums(Decimal("999.00"), Decimal("0.00"), Decimal("999.00"))

    def test_no_rows(self, db):
        page = run(db, F(q="nothing"))
        assert (page.total_count, page.page, page.pages, page.rows) == (0, 1, 1, ())
        assert page.sums == txns.Sums(Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
        assert (page.first_index, page.last_index) == (0, 0)

    @pytest.mark.parametrize(
        ("count", "page", "expected_page", "pages", "on_page"),
        [
            (10, 1, 1, 2, 5),
            (10, 2, 2, 2, 5),
            (10, 3, 2, 2, 5),  # past the end: the last page
            (11, 3, 3, 3, 1),
            (5, 0, 1, 1, 5),
            (5, -4, 1, 1, 5),
        ],
    )
    def test_pagination_edges(self, db, card, count, page, expected_page, pages, on_page):
        for i in range(count):
            make_transaction(
                db, card, name=f"T{i:02d}", date=dt.date(2026, 10, 1) + dt.timedelta(days=i)
            )
        result = run(db, page=page, per_page=5)
        assert (result.page, result.pages, len(result.rows)) == (expected_page, pages, on_page)
        assert result.total_count == count
        assert result.first_index == (expected_page - 1) * 5 + 1

    def test_pages_do_not_overlap(self, db, card):
        for i in range(7):
            make_transaction(db, card, name=f"T{i}", date="2026-10-01")
        seen = names(run(db, page=1, per_page=3)) + names(run(db, page=2, per_page=3))
        seen += names(run(db, page=3, per_page=3))
        assert sorted(seen) == [f"T{i}" for i in range(7)]


class TestRows:
    def test_a_row_carries_what_the_list_shows(self, db, card, cats):
        rule_category = cats["dining"]
        from tests.factories import make_rule

        rule = make_rule(db, rule_category, merchant_pattern="BLUE BOTTLE")
        txn = make_transaction(
            db,
            card,
            name="BLUE BOTTLE COFFEE",
            merchant_name="Blue Bottle Coffee",
            category=rule_category,
            category_source="rule",
            rule_id=rule.id,
            tags=["coffee"],
        )
        row = txns.get_row(db, txn.id)
        assert row.title == "Blue Bottle Coffee" and row.subtitle == "BLUE BOTTLE COFFEE"
        assert row.category.name == "Dining & Drinks" and row.category_source_label == "Rule"
        assert row.rule_label == "Merchant contains “BLUE BOTTLE”"
        assert row.tags == ("coffee",) and not row.is_payment and not row.is_refund

    def test_payments_refunds_and_raw_descriptions(self, db, card, cats):
        payment = make_transaction(
            db,
            card,
            name="AUTOPAY PAYMENT - THANK YOU",
            amount="-900.00",
            category=cats["payments"],
            category_source="system",
        )
        refund = make_transaction(db, card, name="REI", amount="-30.00", category=cats["shopping"])
        csv_row = make_transaction(
            db,
            card,
            name="WHOLEFDS MKT 10234",
            original_description="WHOLEFDS MKT 10234 SAN FRANCISCO CA",
            source="csv",
        )
        p, r, c = (txns.get_row(db, t.id) for t in (payment, refund, csv_row))
        assert p.is_payment and p.category_source_label == "Card payment" and not p.is_refund
        assert r.is_refund and r.is_credit
        assert (
            c.title == "WHOLEFDS MKT 10234" and c.subtitle == "WHOLEFDS MKT 10234 SAN FRANCISCO CA"
        )

    def test_missing_row(self, db):
        with pytest.raises(txns.TransactionNotFound):
            txns.get_row(db, 987654321)

    def test_rule_label_summaries(self):
        assert txns.rule_label(1, "Rides", "UBER", None, "contains", None, None) == "Rides"
        assert (
            txns.rule_label(2, None, None, "NETFLIX", "starts_with", Decimal("5"), None)
            == "Description starts with “NETFLIX” · at least $5.00"
        )
        assert (
            txns.rule_label(3, None, None, None, "contains", None, Decimal("9")) == "at most $9.00"
        )
        assert txns.rule_label(4, None, None, None, None, None, None) == "Rule 4"


class TestReferenceData:
    def test_tags_are_de_duplicated_case_insensitively_most_used_first(self, db, card):
        make_transaction(db, card, tags=["work", "Travel"])
        make_transaction(db, card, tags=["Work"])
        make_transaction(db, card, tags=["work"])
        assert txns.all_tags(db) == ["work", "Travel"]

    def test_filter_options(self, db, card):
        manual = make_account(db, manual=True, name="Old Amex", mask=None)
        make_transaction(db, card, merchant_name="Uber", account_owner="SAM RIVERA", tags=["x"])
        make_transaction(db, card, merchant_name="Uber")
        make_transaction(
            db, manual, merchant_name="Lyft", source="csv", account_owner="ALEX RIVERA"
        )
        options = txns.filter_options(db, today=TODAY)
        assert options.members == ("ALEX RIVERA", "SAM RIVERA")
        assert options.merchants == ("Uber", "Lyft")
        assert [a.name for a in options.accounts][-1] == "Old Amex"
        assert options.tags == ("x",) and options.period_label == "This month"
        assert any(c.system_key == "uncategorized" for c in options.categories)
        assert txns.has_card_members(db) and txns.has_transactions(db)

    def test_empty_database(self, db):
        assert not txns.has_transactions(db)
        assert not txns.has_card_members(db)
        assert txns.attention_counts(db) == txns.AttentionCounts(0, 0)

    def test_hidden_and_custom_categories_are_listed(self, db):
        pets = make_category(db, "Pets", color="#9f1239", is_hidden=True)
        categories = txns.load_categories(db)
        assert categories[pets.id].is_hidden and categories[pets.id].color == "#9f1239"


# --------------------------------------------------------------------------- CSV export


def read_export(db, filt=None, **kwargs) -> tuple[str, list[list[str]]]:
    text = "".join(txns.export_csv(db, filt or TransactionFilter(), today=TODAY, **kwargs))
    return text, list(csv.reader(io.StringIO(text.removeprefix("﻿"))))


class TestExport:
    def test_header_and_a_full_row(self, db, card, cats):
        make_transaction(
            db,
            card,
            name="BLUE BOTTLE COFFEE",
            merchant_name="Blue Bottle",
            amount="6.75",
            date="2026-10-20",
            authorized_date="2026-10-18",
            posted_date="2026-10-20",
            category=cats["dining"],
            notes="latte",
            tags=["coffee", "work"],
            account_owner="SAM RIVERA",
        )
        text, rows = read_export(db)
        assert text.startswith("﻿Date,Authorized date,Posted date,Description,Merchant,")
        assert rows[0] == list(txns.EXPORT_COLUMNS)
        assert rows[1] == [
            "2026-10-18",
            "2026-10-18",
            "2026-10-20",
            "BLUE BOTTLE COFFEE",
            "Blue Bottle",
            "Dining & Drinks",
            "6.75",
            "no",
            card.display_name,
            "SAM RIVERA",
            "latte",
            "coffee, work",
            "Plaid",
        ]
        assert "\r\n" in text

    def test_amounts_round_trip_exactly(self, db, card):
        amounts = ["0.01", "-0.01", "1234567.89", "-54.99", "100.10", "999999999999.99"]
        for amount in amounts:
            make_transaction(db, card, amount=amount)
        _, rows = read_export(db, sort="amount")
        exported = [Decimal(row[6]) for row in rows[1:]]
        assert exported == sorted(Decimal(a) for a in amounts)
        assert all(row[6] == str(Decimal(row[6]).quantize(Decimal("0.01"))) for row in rows[1:])

    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
    def test_formulas_are_neutralised_except_in_the_amount(self, db, card, cats, prefix):
        evil = f'{prefix}HYPERLINK("http://x")'
        make_transaction(
            db,
            card,
            name=evil,
            merchant_name=evil,
            notes=evil,
            tags=[evil],
            amount="-5.00",
            account_owner=evil,
        )
        _, rows = read_export(db)
        row = dict(zip(rows[0], rows[1], strict=True))
        for column in ("Description", "Merchant", "Notes", "Tags", "Card member"):
            assert row[column] == "'" + evil, column
        assert row["Amount"] == "-5.00"

    def test_a_category_name_cannot_inject_either(self, db, card):
        weird = make_category(db, "=cmd|' /C calc'!A0")
        make_transaction(db, card, category=weird)
        _, rows = read_export(db)
        assert rows[1][5] == "'=cmd|' /C calc'!A0"

    def test_split_rows_list_their_parts(self, db, card, cats):
        txn = make_transaction(db, card, amount="213.77", category=cats["shopping"])
        make_split(db, txn, cats["groceries"], "150.00")
        make_split(db, txn, cats["home"], "63.77")
        _, rows = read_export(db)
        assert rows[1][5] == "Groceries 150.00; Home 63.77" and rows[1][6] == "213.77"

    def test_pending_csv_rows_and_the_filter(self, db, card, cats):
        make_transaction(db, card, name="P", pending=True, amount="1.00", date="2026-10-20")
        make_transaction(db, card, name="C", source="csv", amount="2.00", date="2026-10-19")
        make_transaction(db, card, name="old", amount="3.00", date="2025-01-01")
        make_transaction(db, card, name="dupe", source="csv", duplicate_status="suspect")
        _, rows = read_export(db, F(range="year"))
        assert [(r[3], r[7], r[12], r[2]) for r in rows[1:]] == [
            ("P", "yes", "Plaid", ""),
            ("C", "no", "Amex CSV", "2026-10-19"),
        ]
        _, rows = read_export(db, F(show="duplicates"))
        assert [r[3] for r in rows[1:]] == ["dupe"]

    def test_streams_in_batches(self, db, card):
        for i in range(7):
            make_transaction(db, card, name=f"T{i}", date="2026-10-01")
        chunks = list(txns.export_csv(db, TransactionFilter(), today=TODAY, batch_size=3))
        assert len(chunks) == 1 + 3  # header, then 3 + 3 + 1 rows
        assert sum(chunk.count("\r\n") for chunk in chunks) == 8

    def test_filename(self):
        dates = txns.DateRange(D("2026-10-01"), D("2026-10-31"), "")
        assert txns.export_filename(TransactionFilter(), dates, TODAY) == (
            "transactions-2026-10-01-to-2026-10-31.csv"
        )
        since = txns.DateRange(D("2026-08-01"), None, "")
        assert txns.export_filename(TransactionFilter(), since, TODAY) == (
            "transactions-since-2026-08-01.csv"
        )
        everything = txns.DateRange(None, None, "")
        assert txns.export_filename(TransactionFilter(), everything, TODAY) == (
            "transactions-2026-10-29.csv"
        )
