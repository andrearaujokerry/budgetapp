"""CSV ↔ Plaid reconciliation (SYNC-8, ARCHITECTURE §6): matching, the full pass, the cutover and
attaching a manual account. Every value is synthetic."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from budgetapp import clock
from budgetapp.models import (
    Account,
    CsvImport,
    Rule,
    Statement,
    Transaction,
    get_app_settings,
    v_allocations,
)
from budgetapp.services import csv_import, reconcile
from budgetapp.services.reconcile import CsvSide, PlaidSide, match
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_csv_import,
    make_item,
    make_rule,
    make_split,
    make_statement,
    make_transaction,
    set_app_settings,
)
from tests.test_csv_import import amex_csv, amex_row, ledger_locks_held

D = dt.date.fromisoformat


def csv_side(n: int, date: str, amount: str, *texts: str, account: int = 1) -> CsvSide:
    return CsvSide((0, n), account, D(date), Decimal(amount), texts or ("",))


def plaid_side(
    n: int,
    date: str,
    amount: str,
    *texts: str,
    authorized: str | None = None,
    account: int = 1,
) -> PlaidSide:
    return PlaidSide(
        n,
        account,
        D(date),
        D(authorized) if authorized else None,
        Decimal(amount),
        texts or ("",),
    )


def pairs_of(result) -> list[tuple[int, int]]:
    return [(c.key[1], p.id) for c, p in result]


# --------------------------------------------------------------------------- the matcher


class TestMatcher:
    def test_identical_charges_pair_one_to_one(self):
        csv = [csv_side(i, "2025-08-08", "189.00", "HILTON") for i in range(1, 6)]
        plaid = [
            plaid_side(i, "2025-08-11", "189.00", "Hilton", authorized="2025-08-08")
            for i in range(11, 16)
        ]
        result = pairs_of(match(csv, plaid))
        assert len(result) == 5
        assert len({c for c, _ in result}) == len({p for _, p in result}) == 5

    def test_more_csv_rows_than_plaid_rows(self):
        csv = [csv_side(i, "2025-08-08", "189.00", "HILTON") for i in range(1, 6)]
        plaid = [plaid_side(11, "2025-08-11", "189.00", authorized="2025-08-08")]
        assert len(match(csv, plaid)) == 1

    def test_amounts_must_be_equal(self):
        assert (
            match([csv_side(1, "2025-08-08", "10.00")], [plaid_side(2, "2025-08-08", "10.01")])
            == []
        )
        assert (
            match([csv_side(1, "2025-08-08", "10.00")], [plaid_side(2, "2025-08-08", "-10.00")])
            == []
        )
        assert (
            len(match([csv_side(1, "2025-08-08", "10.0")], [plaid_side(2, "2025-08-08", "10.00")]))
            == 1
        )

    @pytest.mark.parametrize(
        ("csv_date", "paired"),
        [
            ("2025-08-07", True),  # authorized − 3 days: the window's first day
            ("2025-08-06", False),  # authorized − 4 days
            ("2025-08-10", True),  # authorized
            ("2025-08-12", True),  # the posting date: the window's last day
            ("2025-08-13", False),  # after the posting date
        ],
    )
    def test_window_with_an_authorized_date(self, csv_date, paired):
        plaid = plaid_side(2, "2025-08-12", "10.00", authorized="2025-08-10")
        assert bool(match([csv_side(1, csv_date, "10.00")], [plaid])) is paired

    @pytest.mark.parametrize(
        ("csv_date", "paired"),
        [
            ("2025-07-13", True),  # date − 30 days
            ("2025-07-12", False),  # date − 31 days
            ("2025-08-12", True),
            ("2025-08-13", False),
        ],
    )
    def test_window_without_an_authorized_date(self, csv_date, paired):
        plaid = plaid_side(2, "2025-08-12", "10.00")
        assert bool(match([csv_side(1, csv_date, "10.00")], [plaid])) is paired

    def test_the_closest_date_wins(self):
        csv = [csv_side(1, "2025-08-10", "10.00", "CAFE")]
        plaid = [
            plaid_side(2, "2025-08-12", "10.00", "Cafe", authorized="2025-08-11"),
            plaid_side(3, "2025-08-12", "10.00", "Cafe", authorized="2025-08-10"),
        ]
        assert pairs_of(match(csv, plaid)) == [(1, 3)]

    def test_the_best_pairs_are_assigned_first_globally(self):
        # CSV 1 could take either Plaid row; CSV 2 fits only Plaid 3 well. Global best-first
        # gives every row its closest partner instead of letting CSV 1 grab Plaid 3.
        csv = [csv_side(1, "2025-08-10", "10.00"), csv_side(2, "2025-08-12", "10.00")]
        plaid = [
            plaid_side(3, "2025-08-13", "10.00", authorized="2025-08-12"),
            plaid_side(4, "2025-08-13", "10.00", authorized="2025-08-10"),
        ]
        assert sorted(pairs_of(match(csv, plaid))) == [(1, 4), (2, 3)]

    def test_description_similarity_breaks_date_ties(self):
        csv = [
            csv_side(1, "2025-08-10", "25.00", "STARBUCKS STORE 1234", "STARBUCKS"),
            csv_side(2, "2025-08-10", "25.00", "SFMTA PARKING METER", "SFMTA PARKING"),
        ]
        plaid = [
            plaid_side(3, "2025-08-11", "25.00", "SFMTA Parking", authorized="2025-08-10"),
            plaid_side(4, "2025-08-11", "25.00", "Starbucks", authorized="2025-08-10"),
        ]
        assert sorted(pairs_of(match(csv, plaid))) == [(1, 4), (2, 3)]

    def test_the_same_account_breaks_remaining_ties(self):
        csv = [csv_side(1, "2025-08-10", "25.00", "X", account=7)]
        plaid = [
            plaid_side(3, "2025-08-11", "25.00", "X", authorized="2025-08-10", account=8),
            plaid_side(4, "2025-08-11", "25.00", "X", authorized="2025-08-10", account=7),
        ]
        assert pairs_of(match(csv, plaid)) == [(1, 4)]

    def test_the_result_does_not_depend_on_input_order(self):
        csv = [csv_side(i, "2025-08-08", "189.00", "HILTON") for i in range(1, 6)]
        plaid = [
            plaid_side(i, "2025-08-09", "189.00", authorized="2025-08-08") for i in range(11, 16)
        ]
        assert sorted(pairs_of(match(csv, plaid))) == sorted(
            pairs_of(match(list(reversed(csv)), list(reversed(plaid))))
        )

    def test_description_similarity(self):
        assert reconcile.description_similarity(["WHOLEFDS MKT 10255"], ["Whole Foods"]) > 0.2
        assert reconcile.description_similarity(["CAFÉ REVEILLE"], ["Cafe Reveille"]) == 1.0
        assert reconcile.description_similarity(["UBER"], ["Delta Air Lines"]) == 0.0
        assert reconcile.description_similarity([""], ["x"]) == 0.0


# --------------------------------------------------------------------------- the full pass


def plaid_row(db, account, amount, date, authorized=None, **kwargs) -> Transaction:
    return make_transaction(
        db, account, amount=amount, date=date, authorized_date=authorized, **kwargs
    )


def csv_row(db, account, amount, date, reference, **kwargs) -> Transaction:
    return make_transaction(
        db,
        account,
        source="csv",
        amount=amount,
        date=date,
        csv_reference=reference,
        **kwargs,
    )


class TestReconcileItem:
    def test_csv_first_then_plaid(self, db):
        """CSV rows imported before linking pair with the Plaid rows a later sync inserts."""
        account = make_account(db)
        imported = make_csv_import(db, account)
        old = csv_row(
            db, account, "5.25", "2025-10-06", "320252810000000001", csv_import_id=imported.id
        )
        kept = csv_row(db, account, "8.00", "2025-10-06", "320252810000000002")
        plaid = plaid_row(db, account, "5.25", "2025-10-08", "2025-10-06", name="Philz Coffee")
        result = reconcile.reconcile_item(db, account.item_id)
        assert (result.paired, result.pairs) == (1, ((old.id, plaid.id),))
        assert db.get(Transaction, old.id) is None
        assert db.get(Transaction, kept.id) is not None
        db.refresh(plaid)
        assert plaid.csv_reference == "320252810000000001"
        assert plaid.csv_import_id == imported.id
        assert (result.posted_dates_compared, result.posted_dates_matched) == (1, 1)
        assert reconcile.reconcile_item(db, account.item_id).paired == 0  # idempotent

    def test_plaid_first_then_csv(self, db):
        """Importing a file after linking: rows Plaid has are merged, never inserted."""
        account = make_account(db)
        plaid = plaid_row(db, account, "189.00", "2025-08-11", "2025-08-08", name="Hilton")
        data = amex_csv(
            *[
                amex_row("2025-08-08", "HILTON GARDEN INN", "189.00", category="Travel-Lodging")
                for _ in range(3)
            ]
        )
        preview = csv_import.preview(db, data, "a.csv", account.id)
        assert (preview.matched, preview.new) == (1, 2)
        outcome = csv_import.commit(db, preview.token)
        assert (outcome.matched_plaid_count, outcome.inserted_count) == (1, 2)
        db.refresh(plaid)
        assert plaid.csv_reference is not None
        stored = db.scalars(select(Transaction).where(Transaction.account_id == account.id)).all()
        assert len(stored) == 3  # 1 Plaid + 2 CSV: three real charges, none twice

    def test_identical_charges_pair_with_different_rows(self, db):
        account = make_account(db)
        csv = [
            csv_row(db, account, "189.00", "2025-08-08", f"32025223000000000{i}") for i in range(5)
        ]
        plaid = [plaid_row(db, account, "189.00", "2025-08-11", "2025-08-08") for _ in range(5)]
        result = reconcile.reconcile_item(db, account.item_id)
        assert result.paired == 5
        assert {c for c, _ in result.pairs} == {t.id for t in csv}
        assert {p for _, p in result.pairs} == {t.id for t in plaid}
        references = {db.get(Transaction, t.id).csv_reference for t in plaid}
        assert len(references) == 5

    def test_pending_and_already_paired_plaid_rows_are_not_candidates(self, db):
        account = make_account(db)
        plaid_row(db, account, "10.00", "2025-10-06", pending=True)
        plaid_row(
            db, account, "10.00", "2025-10-07", "2025-10-06", csv_reference="320252800000000009"
        )
        csv_row(db, account, "10.00", "2025-10-06", "320252800000000001")
        assert reconcile.reconcile_item(db, account.item_id).paired == 0

    def test_pairs_across_accounts_of_the_same_item_but_never_other_items(self, db):
        item = make_item(db)
        basic = make_account(db, item)
        additional = make_account(db, item)
        other = make_account(db)  # another Item
        csv = csv_row(db, basic, "15.00", "2025-10-06", "320252800000000001")
        plaid_row(db, other, "15.00", "2025-10-07", "2025-10-06")
        target = plaid_row(db, additional, "15.00", "2025-10-07", "2025-10-06")
        assert reconcile.reconcile_item(db, item.id).pairs == ((csv.id, target.id),)

    def test_manual_accounts_are_never_reconciled(self, db):
        manual = make_account(db, manual=True)
        linked = make_account(db)
        csv_row(db, manual, "15.00", "2025-10-06", "320252800000000001")
        plaid_row(db, linked, "15.00", "2025-10-07", "2025-10-06")
        assert reconcile.reconcile_item(db, linked.item_id).paired == 0

    def test_an_unknown_item_does_nothing(self, db):
        assert reconcile.reconcile_item(db, 999_999) == reconcile.ReconcileResult()

    def test_the_pass_takes_the_ledger_lock(self, db):
        account = make_account(db)
        assert ledger_locks_held(db) == 0
        reconcile.reconcile_item(db, account.item_id)
        assert ledger_locks_held(db) >= 1


class TestUserState:
    def test_the_csv_rows_edits_move_onto_the_plaid_row(self, db):
        account = make_account(db)
        dining = get_category(db, "Dining & Drinks")
        gifts = get_category(db, "Gifts & Donations")
        csv = csv_row(
            db,
            account,
            "60.00",
            "2025-10-06",
            "320252800000000001",
            category=dining,
            category_source="user",
            notes="Birthday dinner",
            tags=["family", "birthday"],
            user_edited_at=clock.now(),
        )
        make_split(db, csv, dining, "40.00")
        make_split(db, csv, gifts, "20.00", note="gift")
        plaid = plaid_row(db, account, "60.00", "2025-10-07", "2025-10-06", tags=["Family"])
        reconcile.reconcile_item(db, account.item_id)
        db.refresh(plaid)
        assert (plaid.category_id, plaid.category_source) == (dining.id, "user")
        assert plaid.notes == "Birthday dinner"
        assert plaid.tags == ["Family", "birthday"]  # union, first spelling kept
        assert [(s.category_id, s.amount, s.note) for s in plaid.splits] == [
            (dining.id, Decimal("40.00"), None),
            (gifts.id, Decimal("20.00"), "gift"),
        ]
        assert plaid.user_edited_at is not None
        spent = db.execute(
            select(v_allocations.c.category_id, v_allocations.c.amount)
            .where(v_allocations.c.transaction_id == plaid.id)
            .order_by(v_allocations.c.amount.desc())
        ).all()
        assert spent == [(dining.id, Decimal("40.00")), (gifts.id, Decimal("20.00"))]

    def test_the_plaid_rows_own_edits_are_never_clobbered(self, db):
        account = make_account(db)
        dining = get_category(db, "Dining & Drinks")
        travel = get_category(db, "Travel")
        csv = csv_row(
            db,
            account,
            "60.00",
            "2025-10-06",
            "320252800000000001",
            category=dining,
            category_source="user",
            notes="from the CSV",
        )
        make_split(db, csv, dining, "30.00")
        make_split(db, csv, dining, "30.00")
        plaid = plaid_row(
            db,
            account,
            "60.00",
            "2025-10-07",
            "2025-10-06",
            category=travel,
            category_source="user",
            notes="mine",
        )
        reconcile.reconcile_item(db, account.item_id)
        db.refresh(plaid)
        assert (plaid.category_id, plaid.category_source) == (travel.id, "user")
        assert plaid.splits == []  # the carried splits don't override the user's own category
        assert plaid.notes == "from the CSV\n\nmine"  # notes are never lost

    def test_a_payment_never_takes_a_carried_category(self, db):
        account = make_account(db)
        dining = get_category(db, "Dining & Drinks")
        csv_row(
            db,
            account,
            "-500.00",
            "2025-10-06",
            "320252800000000001",
            category=dining,
            category_source="user",
        )
        plaid = plaid_row(
            db,
            account,
            "-500.00",
            "2025-10-06",
            "2025-10-06",
            name="AUTOPAY PAYMENT RECEIVED - THANK YOU",
            category=card_payments(db),
            category_source="system",
        )
        reconcile.reconcile_item(db, account.item_id)
        db.refresh(plaid)
        assert plaid.category_id == card_payments(db).id and plaid.needs_review

    def test_rules_made_from_the_csv_row_survive_it(self, db):
        account = make_account(db)
        csv = csv_row(db, account, "9.00", "2025-10-06", "320252800000000001")
        rule = make_rule(db, get_category(db, "Travel"), source_transaction_id=csv.id)
        plaid_row(db, account, "9.00", "2025-10-07", "2025-10-06")
        reconcile.reconcile_item(db, account.item_id)
        db.expire_all()
        assert db.get(Rule, rule.id).source_transaction_id is None  # ON DELETE SET NULL


class TestCutover:
    def test_unpaired_rows_inside_plaids_history_become_suspects(self, db):
        item = make_item(db, history_complete_at=clock.now())
        account = make_account(db, item)
        plaid_row(db, account, "40.00", "2025-10-01", "2025-09-30")
        before = csv_row(db, account, "11.00", "2025-09-30", "320252730000000001")
        on = csv_row(db, account, "12.00", "2025-10-01", "320252740000000002")
        after = csv_row(db, account, "13.00", "2025-10-05", "320252780000000003")
        kept = csv_row(
            db, account, "14.00", "2025-10-06", "320252790000000004", duplicate_status="kept"
        )
        result = reconcile.reconcile_item(db, item.id)
        assert result.suspects_flagged == 2
        db.expire_all()
        assert db.get(Account, account.id).plaid_history_start == D("2025-10-01")
        statuses = {
            t.id: db.get(Transaction, t.id).duplicate_status for t in (before, on, after, kept)
        }
        assert statuses == {
            before.id: "none",
            on.id: "suspect",
            after.id: "suspect",
            kept.id: "kept",
        }
        assert reconcile.reconcile_item(db, item.id).suspects_flagged == 0
        counted = db.scalars(select(v_allocations.c.transaction_id)).all()
        assert on.id not in counted and after.id not in counted and before.id in counted

    def test_a_history_start_set_by_the_sync_is_kept(self, db):
        item = make_item(db, history_complete_at=clock.now())
        account = make_account(db, item, plaid_history_start=D("2025-10-04"))
        plaid_row(db, account, "40.00", "2025-10-01")
        early = csv_row(db, account, "12.00", "2025-10-02", "320252750000000001")
        reconcile.reconcile_item(db, item.id)
        db.expire_all()
        assert db.get(Account, account.id).plaid_history_start == D("2025-10-04")
        assert db.get(Transaction, early.id).duplicate_status == "none"

    def test_nothing_is_flagged_before_history_is_complete(self, db):
        account = make_account(db)
        plaid_row(db, account, "40.00", "2025-10-01")
        row = csv_row(db, account, "12.00", "2025-10-05", "320252780000000001")
        assert reconcile.reconcile_item(db, account.item_id).suspects_flagged == 0
        assert db.get(Account, account.id).plaid_history_start is None
        assert db.get(Transaction, row.id).duplicate_status == "none"

    def test_an_account_without_plaid_rows_has_no_cutover(self, db):
        item = make_item(db, history_complete_at=clock.now())
        account = make_account(db, item)
        csv_row(db, account, "12.00", "2025-10-05", "320252780000000001")
        assert reconcile.reconcile_item(db, item.id).suspects_flagged == 0

    def test_a_suspect_pairs_when_its_plaid_row_arrives(self, db):
        item = make_item(db, history_complete_at=clock.now())
        account = make_account(db, item)
        plaid_row(db, account, "40.00", "2025-10-01")
        suspect = csv_row(db, account, "12.00", "2025-10-05", "320252790000000001")
        reconcile.reconcile_item(db, item.id)
        assert db.get(Transaction, suspect.id).duplicate_status == "suspect"
        late = plaid_row(db, account, "12.00", "2025-10-06", "2025-10-05")
        result = reconcile.reconcile_item(db, item.id)
        assert result.pairs == ((suspect.id, late.id),)
        assert db.get(Transaction, suspect.id) is None


# --------------------------------------------------------------------------- attaching


class TestAttach:
    def test_attach_moves_everything_and_reconciles(self, db):
        item = make_item(db, history_complete_at=clock.now())
        linked = make_account(db, item)
        manual = make_account(db, manual=True, statement_closing_day=27, notice="Check July")
        imported = make_csv_import(db, manual)
        rule = make_rule(db, get_category(db, "Travel"), account_id=manual.id)
        old = csv_row(
            db, manual, "9.00", "2025-06-01", "320251530000000001", csv_import_id=imported.id
        )
        twin = csv_row(
            db, manual, "15.00", "2025-10-06", "320252800000000002", csv_import_id=imported.id
        )
        orphan = csv_row(db, manual, "16.00", "2025-10-08", "320252820000000003")
        plaid = plaid_row(db, linked, "15.00", "2025-10-07", "2025-10-06")
        plaid_row(db, linked, "40.00", "2025-10-01")
        set_app_settings(db, budget_cycle_account_id=manual.id, budget_period_mode="statement")

        result = reconcile.attach_manual_account(db, manual.id, linked.id)

        db.expire_all()
        assert db.get(Account, manual.id) is None
        assert (result.moved_transactions, result.dropped_duplicates) == (3, 0)
        assert (result.moved_imports, result.moved_rules) == (1, 1)
        assert result.reconcile.pairs == ((twin.id, plaid.id),)
        assert result.reconcile.suspects_flagged == 1
        assert db.get(Transaction, old.id).account_id == linked.id
        assert db.get(Transaction, old.id).duplicate_status == "none"  # before Plaid's history
        assert db.get(Transaction, orphan.id).duplicate_status == "suspect"
        assert db.get(Transaction, plaid.id).csv_import_id == imported.id
        assert db.get(CsvImport, imported.id).account_id == linked.id
        assert db.get(Rule, rule.id).account_id == linked.id
        assert get_app_settings(db).budget_cycle_account_id == linked.id
        target = db.get(Account, linked.id)
        assert (target.statement_closing_day, target.notice) == (27, "Check July")

    def test_the_linked_accounts_own_settings_win(self, db):
        linked = make_account(db, statement_closing_day=3, notice="Plaid re-issued this account")
        manual = make_account(db, manual=True, statement_closing_day=27, notice="Check July")
        reconcile.attach_manual_account(db, manual.id, linked.id)
        target = db.get(Account, linked.id)
        assert (target.statement_closing_day, target.notice) == (3, "Plaid re-issued this account")

    def test_statements_are_moved_or_merged(self, db):
        linked = make_account(db)
        manual = make_account(db, manual=True)
        # Only on the manual account: moved.
        moved = make_statement(db, manual, "2025-05-27", reported_new_balance=Decimal("100.00"))
        # Same closing date: merged, the typed (user) balance beats the Liabilities snapshot.
        mine = make_statement(
            db,
            manual,
            "2025-06-27",
            reported_new_balance=Decimal("812.40"),
            reported_new_balance_source="user",
            reported_fees=Decimal("0.00"),
            notes="From the PDF",
        )
        theirs = make_statement(
            db,
            linked,
            "2025-06-27",
            closing_date_source="liabilities",
            plaid_issue_date=D("2025-06-27"),
            reported_new_balance=Decimal("812.39"),
            reported_new_balance_source="liabilities",
            notes="Snapshot",
        )
        # Same issue date, different closing date: merged; the user's closing date wins.
        typed = make_statement(db, manual, "2025-07-26", plaid_issue_date=D("2025-07-28"))
        snapshot = make_statement(
            db,
            linked,
            "2025-07-28",
            closing_date_source="liabilities",
            plaid_issue_date=D("2025-07-28"),
        )
        result = reconcile.attach_manual_account(db, manual.id, linked.id)
        assert (result.moved_statements, result.merged_statements) == (1, 2)
        db.expire_all()
        rows = db.scalars(
            select(Statement)
            .where(Statement.account_id == linked.id)
            .order_by(Statement.closing_date)
        ).all()
        assert [s.id for s in rows] == [moved.id, theirs.id, snapshot.id]
        merged = db.get(Statement, theirs.id)
        assert (merged.reported_new_balance, merged.reported_new_balance_source) == (
            Decimal("812.40"),
            "user",
        )
        assert merged.reported_fees == Decimal("0.00")
        assert merged.plaid_issue_date == D("2025-06-27")
        assert merged.notes == "Snapshot\n\nFrom the PDF"
        renamed = db.get(Statement, snapshot.id)
        assert (renamed.closing_date, renamed.closing_date_source) == (D("2025-07-26"), "user")
        assert db.get(Statement, mine.id) is None and db.get(Statement, typed.id) is None

    def test_a_user_balance_on_the_linked_side_is_kept(self, db):
        linked = make_account(db)
        manual = make_account(db, manual=True)
        make_statement(
            db,
            manual,
            "2025-06-27",
            reported_new_balance=Decimal("1.00"),
            reported_new_balance_source="user",
        )
        kept = make_statement(
            db,
            linked,
            "2025-06-27",
            reported_new_balance=Decimal("2.00"),
            reported_new_balance_source="user",
        )
        reconcile.attach_manual_account(db, manual.id, linked.id)
        assert db.get(Statement, kept.id).reported_new_balance == Decimal("2.00")

    @pytest.mark.parametrize("swap", [False, True])
    def test_wrong_accounts_are_refused(self, db, swap):
        linked = make_account(db)
        manual = make_account(db, manual=True)
        first, second = (linked, manual) if swap else (manual, make_account(db, manual=True))
        with pytest.raises(reconcile.AttachError):
            reconcile.attach_manual_account(db, first.id, second.id)
        with pytest.raises(reconcile.AttachError):
            reconcile.attach_manual_account(db, 999_999, linked.id)

    def test_attaching_twice_is_refused(self, db):
        linked = make_account(db)
        manual = make_account(db, manual=True)
        reconcile.attach_manual_account(db, manual.id, linked.id)
        with pytest.raises(reconcile.AttachError):
            reconcile.attach_manual_account(db, manual.id, linked.id)

    def test_an_imported_file_then_attached_matches_like_an_import_after_linking(self, db):
        """The whole flow: import before linking, link, attach → each charge stored once."""
        manual_preview = csv_import.preview(
            db,
            amex_csv(
                amex_row("2025-06-01", "OLD COFFEE", "4.00"),
                amex_row("2025-10-06", "PHILZ COFFEE", "5.25"),
            ),
            "before-linking.csv",
            None,
        )
        imported = csv_import.commit(db, manual_preview.token)
        item = make_item(db, history_complete_at=clock.now())
        linked = make_account(db, item)
        plaid_row(db, linked, "5.25", "2025-10-08", "2025-10-06", name="Philz Coffee")
        result = reconcile.attach_manual_account(db, imported.account_id, linked.id)
        assert result.reconcile.paired == 1 and result.moved_transactions == 2
        total = db.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.account_id == linked.id)
        )
        assert total == 2  # the old coffee and the (merged) Philz charge
