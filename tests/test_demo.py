"""Demo databases (``budgetapp.devtools``): determinism and the invariants every page relies on.

One scratch demo database (``budgetapp_test_demo_t<pid>``) is created for a fixed seed and date,
checked against the real services (Categorizer, spending, status), recreated to prove determinism,
and dropped at the end.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from budgetapp import cli, clock
from budgetapp.db import make_engine
from budgetapp.devtools import demo
from budgetapp.devtools import demo_catalog as cat
from budgetapp.devtools.demo_gateway import DEMO_MESSAGE, DemoPlaidGateway
from budgetapp.devtools.demo_plan import build_plan, local_time
from budgetapp.models import (
    Account,
    AlertEvent,
    Budget,
    Category,
    CategoryMapping,
    CsvImport,
    PlaidItem,
    Rule,
    Statement,
    SyncRun,
    Transaction,
    TransactionSplit,
    v_allocations,
)
from budgetapp.periods import month_of, previous_period
from budgetapp.plaid_gateway import ALLOWED_METHODS, PlaidError, PlaidGateway, gateway_methods
from budgetapp.services import status
from budgetapp.services.categorize import Categorizer, is_payment
from budgetapp.services.spending import period_spending

SEED = 7
TODAY = dt.date(2026, 10, 15)
NAME = f"t{os.getpid()}"
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
PLAID_CODES = {
    "purchase",
    "refund",
    "payment",
    "adjustment",
    "interest",
    "membership fee",
    "bank charge",
}
FEE_CODES = {"membership fee", "late fee", "bank charge", "returned item fee"}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def created() -> Iterator[demo.DemoSummary]:
    summary = demo.create_demo(NAME, seed=SEED, today=TODAY)
    try:
        yield summary
    finally:
        demo.drop_demo(NAME)


@pytest.fixture(scope="module")
def demo_engine(created: demo.DemoSummary) -> Iterator[Engine]:
    engine = make_engine(demo.demo_settings(NAME))
    yield engine
    engine.dispose()


@pytest.fixture
def s(demo_engine: Engine) -> Iterator[Session]:
    with Session(demo_engine) as session:
        yield session
        session.rollback()


def _all(session: Session) -> list[Transaction]:
    return list(session.scalars(select(Transaction).order_by(Transaction.id)))


def _category_ids(session: Session) -> dict[str, int]:
    return {c.name: c.id for c in session.scalars(select(Category))}


# --------------------------------------------------------------------------- names and settings


@pytest.mark.parametrize("name", ["a", "budgets", "wp_sync_2", "x" * 20, "0demo"])
def test_valid_names_map_to_test_databases(name):
    assert demo.demo_db_name(name) == f"budgetapp_test_demo_{name}"


@pytest.mark.parametrize("name", ["", "Budgets", "_x", "x" * 21, "a-b", "a b", "ü", None])
def test_invalid_names_are_refused(name):
    with pytest.raises(demo.DemoError, match="not a demo name"):
        demo.demo_db_name(name)


def test_settings_for_a_missing_demo_say_how_to_create_it():
    with pytest.raises(demo.DemoError, match="run ./run demo create nosuchdemo first"):
        demo.demo_settings("nosuchdemo")


def test_demo_settings_are_test_settings_on_the_chosen_port(created):
    settings = demo.demo_settings(NAME, port=8805)
    assert settings.env == "test" and settings.is_test
    assert settings.db_name == f"budgetapp_test_demo_{NAME}"
    assert settings.db_role == "budgetapp_test" and settings.plaid_env == "sandbox"
    assert settings.web_port == 8805 and settings.app_url == "http://127.0.0.1:8805"
    assert demo.demo_settings(NAME).web_port == demo.DEMO_PORT == 8800


def test_demo_settings_refuse_an_outdated_schema(created, monkeypatch):
    monkeypatch.setattr(demo, "code_head", lambda: "9999_future")
    with pytest.raises(demo.DemoError, match=f"run ./run demo create {NAME} again"):
        demo.demo_settings(NAME)


def test_bad_ports_are_demo_errors(created):
    with pytest.raises(demo.DemoError, match="port"):
        demo.demo_settings(NAME, port=0)


def test_list_and_drop(created):
    assert NAME in demo.list_demos()
    assert demo.drop_demo("nosuchdemo") is False


# --------------------------------------------------------------------------- determinism


def _fingerprint(session: Session) -> list[tuple]:
    rows: list[tuple] = []
    names = {c.id: c.name for c in session.scalars(select(Category))}
    for t in _all(session):
        rows.append(
            (
                t.source,
                t.plaid_transaction_id,
                t.pending_transaction_id,
                t.csv_reference,
                t.date,
                t.authorized_date,
                t.posted_date,
                t.name,
                t.merchant_name,
                t.amount,
                t.pfc_detailed,
                t.transaction_code,
                names[t.category_id],
                t.category_source,
                t.notes,
                tuple(t.tags),
                t.duplicate_status,
                t.needs_review,
                tuple((names[x.category_id], x.amount) for x in t.splits),
            )
        )
    for st in session.scalars(select(Statement).order_by(Statement.closing_date)):
        rows.append(
            (
                st.closing_date,
                st.reported_previous_balance,
                st.reported_payments_credits,
                st.reported_new_charges,
                st.reported_fees,
                st.reported_interest,
                st.reported_new_balance,
            )
        )
    for b in session.scalars(select(Budget).order_by(Budget.id)):
        rows.append((b.category_id, b.period, b.amount, b.rollover, b.effective_from))
    for r in session.scalars(select(SyncRun).order_by(SyncRun.id)):
        rows.append((r.trigger, r.status, r.started_at, r.added_count, r.error_code))
    for a in session.scalars(select(AlertEvent).order_by(AlertEvent.id)):
        rows.append((a.kind, a.dedupe_key, a.created_at))
    item = session.scalars(select(PlaidItem)).one()
    account = session.scalars(select(Account)).one()
    rows.append((item.plaid_item_id, item.consent_expires_at, account.current_balance))
    return rows


def test_the_plan_is_deterministic_for_seed_and_today():
    now = local_time(TODAY, 23, 59)
    first, again = build_plan(SEED, TODAY, now), build_plan(SEED, TODAY, now)
    other = build_plan(SEED + 1, TODAY, now)
    key = lambda p: [(r.auth, r.amount, r.name, r.plaid_id, r.csv_reference) for r in p.rows]  # noqa: E731
    assert key(first) == key(again)
    assert key(first) != key(other)


def test_recreating_gives_the_same_database_quickly(created, demo_engine):
    with Session(demo_engine) as session:
        before = _fingerprint(session)
    again = demo.create_demo(NAME, seed=SEED, today=TODAY)
    demo_engine.dispose()  # the old connections died with the dropped database
    with Session(demo_engine) as session:
        assert _fingerprint(session) == before
    assert again.counts == created.counts
    assert created.seconds < 60 and again.seconds < 60


def test_summary_counts_every_table(created):
    counts = created.counts
    assert 2200 <= counts["transactions"] <= 2900
    assert counts["plaid_items"] == counts["accounts"] == counts["csv_imports"] == 1
    assert counts["statements"] == 12 and counts["rules"] == len(cat.RULES)
    assert counts["sync_runs"] >= 12 and counts["alert_events"] >= 2
    assert created.transactions["suspect"] == 2 and created.transactions["needs_review"] == 1
    assert 2 <= created.transactions["pending"] <= 15
    text = "\n".join(created.lines())
    assert "Health & Fitness" in text and "services.spending" in text


# --------------------------------------------------------------------------- Plaid conventions


def test_plaid_rows_follow_plaid_conventions(s):
    mapped = set(s.scalars(select(CategoryMapping.external_key)))
    ids: set[str] = set()
    for t in _all(s):
        if t.source != "plaid":
            continue
        assert t.amount == t.amount.quantize(CENT) and t.amount != 0
        assert t.plaid_transaction_id and len(t.plaid_transaction_id) == 37
        ids.add(t.plaid_transaction_id)
        assert t.transaction_code in PLAID_CODES
        assert t.iso_currency_code == "USD" and isinstance(t.location, dict)
        if t.pending:
            assert t.posted_date is None and t.date == t.authorized_date
            assert t.pending_transaction_id is None and t.amount > 0
        else:
            assert t.posted_date == t.date
            assert 0 <= (t.date - t.authorized_date).days <= 3
        if t.pfc_detailed is not None:
            assert t.pfc_detailed.startswith(t.pfc_primary + "_") and t.pfc_version == "v2"
            assert t.pfc_detailed in mapped and t.pfc_primary in mapped
        sign = {"payment": -1, "refund": -1, "adjustment": -1, "purchase": 1, "interest": 1}
        if t.transaction_code in sign:
            assert (t.amount > 0) == (sign[t.transaction_code] > 0)
        if t.transaction_code == "purchase" and t.pfc_detailed is not None:
            assert t.merchant_name and t.merchant_entity_id
        if t.transaction_code == "payment":
            assert t.merchant_name is None and t.pfc_detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    pending_ids = {t.pending_transaction_id for t in _all(s) if t.pending_transaction_id}
    assert pending_ids and not pending_ids & ids


def test_payments_are_card_payments_and_only_payments_are(s):
    card_payments = _category_ids(s)["Card Payments"]
    payments = [t for t in _all(s) if is_payment(t)]
    assert len(payments) >= 22
    assert all(t.category_id == card_payments and t.category_source == "system" for t in payments)
    assert all(t.amount < 0 for t in payments)
    assert {t.source for t in payments} == {"plaid", "csv"}
    others = [t for t in _all(s) if t.category_id == card_payments and not is_payment(t)]
    assert others == []
    names = {t.name for t in payments if t.source == "plaid"}
    assert "AUTOPAY PAYMENT - THANK YOU" in names and "MOBILE PAYMENT - THANK YOU" in names


def test_every_row_is_categorized_exactly_as_the_categorizer_decides(s):
    categorizer = Categorizer.load(s)
    rows = _all(s)
    for t in rows:
        if t.category_source == "user":
            continue
        decision = categorizer.decide(t)
        assert (t.category_id, t.category_source, t.rule_id) == (
            decision.category_id,
            decision.source,
            decision.rule_id,
        ), (t.name, t.amount, decision.reason)
    sources = {t.category_source for t in rows}
    assert sources == {"user", "system", "rule", "plaid", "amex", "default"}
    used = {t.category_id for t in rows}
    assert set(_category_ids(s).values()) <= used  # every category, Uncategorized included


def test_rules_match_and_the_refund_rule_is_used(s):
    rules = list(s.scalars(select(Rule)))
    by_rule = defaultdict(int)
    for t in _all(s):
        if t.rule_id is not None:
            by_rule[t.rule_id] += 1
    for rule in rules:
        assert (by_rule[rule.id] > 0) == rule.is_active, rule.name
    assert any(r.source_transaction_id is not None for r in rules)
    refunds = [t for t in _all(s) if t.transaction_code == "refund"]
    assert refunds and all(t.amount < 0 for t in refunds)
    rescued = [t for t in refunds if t.pfc_primary == "TRANSFER_IN"]
    shopping = _category_ids(s)["Shopping"]
    assert rescued and all(
        t.category_id == shopping and t.category_source == "default" for t in rescued
    )


def test_user_edits(s):
    rows = _all(s)
    user = [t for t in rows if t.category_source == "user"]
    assert len(user) >= 5 and all(t.user_edited_at is not None for t in user)
    assert sum(1 for t in rows if t.notes) >= 8
    tags = {tag for t in rows for tag in t.tags}
    assert {"NYC conference", "reimbursable", "gift"} <= tags
    assert all(t.user_edited_at is not None for t in rows if t.notes or t.tags)


# --------------------------------------------------------------------------- CSV history


def test_csv_rows_are_older_than_the_plaid_history_with_amex_fields(s):
    account = s.scalars(select(Account)).one()
    plaid_posted = [t.posted_date for t in _all(s) if t.source == "plaid" and t.posted_date]
    assert account.plaid_history_start == min(plaid_posted)
    csv_rows = [t for t in _all(s) if t.source == "csv"]
    assert len(csv_rows) > 400
    for t in csv_rows:
        assert re.fullmatch(r"3\d{17}", t.csv_reference)
        embedded = dt.datetime.strptime(t.csv_reference[1:8], "%Y%j").date()
        assert embedded == t.posted_date and t.date <= t.posted_date <= t.date + dt.timedelta(30)
        assert t.authorized_date is None and t.merchant_name is None and t.pfc_detailed is None
        assert t.account_owner in (cat.CARD_MEMBER, cat.ADDITIONAL_CARD_MEMBER)
        assert (t.external_category is None) == is_payment(t)
        if t.duplicate_status == "suspect":
            assert t.date >= account.plaid_history_start
        else:
            assert t.posted_date < account.plaid_history_start
            assert t.category_source in ("amex", "rule", "system", "user")


def test_the_csv_import_row_matches_the_rows(s):
    imp = s.scalars(select(CsvImport)).one()
    csv_rows = [t for t in _all(s) if t.source == "csv"]
    paired = [t for t in _all(s) if t.source == "plaid" and t.csv_reference]
    assert paired and all(t.csv_import_id == imp.id for t in csv_rows + paired)
    assert imp.row_count == len(csv_rows) + len(paired)
    assert imp.inserted_count == len(csv_rows) and imp.matched_plaid_count == len(paired)
    assert imp.duplicate_count == imp.error_count == 0
    for t in paired:
        assert re.fullmatch(r"3\d{17}", t.csv_reference)
        assert dt.datetime.strptime(t.csv_reference[1:8], "%Y%j").date() == t.posted_date
    dates = [t.date if t.source == "csv" else t.authorized_date for t in csv_rows + paired]
    assert imp.date_min == min(dates) and imp.date_max <= max(dates)


def test_the_two_suspects_duplicate_unpaired_plaid_rows_and_never_count(s):
    suspects = [t for t in _all(s) if t.duplicate_status == "suspect"]
    assert len(suspects) == 2 and all(t.source == "csv" for t in suspects)
    for t in suspects:
        twins = [
            p
            for p in _all(s)
            if p.source == "plaid" and p.amount == t.amount and p.posted_date == t.posted_date
        ]
        assert len(twins) == 1 and twins[0].csv_reference is None
        assert t.date < twins[0].authorized_date - dt.timedelta(days=3)  # outside the window
    ids = [t.id for t in suspects]
    assert not s.execute(select(v_allocations).where(v_allocations.c.transaction_id.in_(ids))).all()
    month = month_of(suspects[0].date)
    travel = _category_ids(s)["Travel"]
    expected = sum(
        (
            t.amount
            for t in _all(s)
            if t.category_id == travel
            and not t.splits
            and t.duplicate_status != "suspect"
            and month.contains(t.authorized_date or t.date)
        ),
        ZERO,
    )
    assert period_spending(s, month).amount_for(travel) == expected


# --------------------------------------------------------------------------- splits and review


def test_splits_sum_exactly_and_feed_the_allocations(s):
    split_rows = [t for t in _all(s) if t.splits]
    assert len(split_rows) == 3
    for t in split_rows:
        assert len(t.splits) >= 2 and not t.pending
        assert sum((x.amount for x in t.splits), ZERO) == t.amount
        allocations = s.execute(
            select(v_allocations.c.category_id, v_allocations.c.amount).where(
                v_allocations.c.transaction_id == t.id
            )
        ).all()
        assert sorted(allocations) == sorted((x.category_id, x.amount) for x in t.splits)
    assert s.scalar(select(TransactionSplit.id).limit(1)) is not None


def test_exactly_one_row_needs_review_and_says_why(s):
    flagged = [t for t in _all(s) if t.needs_review]
    assert len(flagged) == 1
    (t,) = flagged
    assert t.review_reason and "rescaled" in t.review_reason
    assert t.splits and sum((x.amount for x in t.splits), ZERO) == t.amount


# --------------------------------------------------------------------------- statements


def _buckets(rows: list[Transaction]) -> dict[str, Decimal]:
    out = dict.fromkeys(("payments", "charges", "fees", "interest"), ZERO)
    for t in rows:
        code = t.transaction_code or ""
        if t.amount < 0:
            out["payments"] -= t.amount
        elif code == "interest" or t.pfc_detailed == "BANK_FEES_INTEREST_CHARGE":
            out["interest"] += t.amount
        elif (
            code in FEE_CODES
            or t.pfc_primary == "BANK_FEES"
            or (t.source == "csv" and (t.external_category or "").startswith("Fees & Adjustments"))
        ):
            out["fees"] += t.amount
        else:
            out["charges"] += t.amount
    return out


def test_statements_follow_the_amex_identity_with_one_cent_off_once(s):
    statements = list(s.scalars(select(Statement).order_by(Statement.closing_date)))
    assert len(statements) == 12
    assert statements[0].period_start_override is not None
    assert all(x.period_start_override is None for x in statements[1:])
    days = [(b.closing_date - a.closing_date).days for a, b in itertools.pairwise(statements)]
    assert all(25 <= d <= 35 for d in days)
    assert len({x.closing_date.day for x in statements}) > 1  # closing dates drift
    posted = [t for t in _all(s) if t.posted_date and t.duplicate_status != "suspect"]
    off_identity, off_gate_d, off_fields = [], [], []
    start = statements[0].period_start_override
    for i, st in enumerate(statements):
        assert st.closing_date_source == "liabilities" and st.plaid_issue_date == st.closing_date
        if i:
            start = statements[i - 1].closing_date + dt.timedelta(days=1)
            assert st.reported_previous_balance == statements[i - 1].reported_new_balance
        # A real statement always adds up.
        assert st.reported_new_balance == (
            st.reported_previous_balance
            - st.reported_payments_credits
            + st.reported_new_charges
            + st.reported_fees
            + st.reported_interest
        )
        b = _buckets([t for t in posted if start <= t.posted_date <= st.closing_date])
        computed = (
            st.reported_previous_balance - b["payments"] + b["charges"] + b["fees"] + b["interest"]
        )
        if computed != st.reported_new_balance:
            off_identity.append((st.closing_date, st.reported_new_balance - computed))
        reported = (
            st.reported_payments_credits,
            st.reported_new_charges,
            st.reported_fees,
            st.reported_interest,
        )
        if reported != (b["payments"], b["charges"], b["fees"], b["interest"]):
            off_fields.append(st.closing_date)
        if i:
            net = b["charges"] + b["fees"] + b["interest"] - b["payments"]
            delta = st.reported_new_balance - statements[i - 1].reported_new_balance
            if delta != net:
                off_gate_d.append((st.closing_date, delta - net))
    assert len(off_identity) == 1 and off_identity[0][1] == CENT
    assert off_gate_d == off_identity and off_fields == [off_identity[0][0]]
    assert any(x.reported_fees > 0 for x in statements)
    assert any(x.reported_interest > 0 for x in statements)


def test_autopay_pays_each_statement_balance_except_one_partial_month(s):
    statements = list(s.scalars(select(Statement).order_by(Statement.closing_date)))
    payments = [t for t in _all(s) if is_payment(t)]
    full = partial = 0
    for st in statements:
        due = st.closing_date + dt.timedelta(days=25)
        if due > TODAY:
            continue
        paid = -sum((t.amount for t in payments if st.closing_date < t.posted_date <= due), ZERO)
        if paid == st.reported_new_balance:
            full += 1
        else:
            assert ZERO < paid < st.reported_new_balance
            partial += 1
    assert partial == 1 and full >= 9


def test_the_card_reflects_the_latest_statement(s):
    account = s.scalars(select(Account)).one()
    latest = s.scalars(select(Statement).order_by(Statement.closing_date.desc())).first()
    assert account.type == "credit" and account.subtype == "credit card"
    assert account.statement_closing_day == cat.CLOSING_DAY
    assert account.statement_balance == latest.reported_new_balance
    assert account.statement_issue_date == latest.closing_date
    assert account.payment_due_date == latest.closing_date + dt.timedelta(days=25)
    assert account.credit_limit == cat.CREDIT_LIMIT
    for field in (
        "current_balance",
        "available_balance",
        "balances_as_of",
        "minimum_payment",
        "last_payment_amount",
        "last_payment_date",
        "liabilities_as_of",
    ):
        assert getattr(account, field) is not None, field
    assert account.available_balance < account.credit_limit - account.current_balance + CENT


# --------------------------------------------------------------------------- budgets


def _budget(s: Session, category_id: int | None, period: str, on: dt.date) -> Budget | None:
    rows = [
        b
        for b in s.scalars(select(Budget).where(Budget.period == period))
        if b.category_id == category_id and b.effective_from <= on
    ]
    return max(rows, key=lambda b: b.effective_from) if rows else None


def _available(s: Session, category_id: int, month) -> tuple[Decimal, Decimal]:
    """(budget amount, carry-in) for a month: ARCHITECTURE §5, BUD-4."""
    row = _budget(s, category_id, "monthly", month.end)
    carry = ZERO
    if row.rollover:
        chain, p = [], month
        while (v := _budget(s, category_id, "monthly", (p := previous_period(p)).end)).rollover:
            chain.append((p, v.amount))
        for p, amount in reversed(chain):
            carry = amount + carry - period_spending(s, p).amount_for(category_id)
    return row.amount, carry


def test_budgets_follow_the_versioning_rules_and_this_month_has_one_over_one_near(s):
    ids = _category_ids(s)
    month = month_of(TODAY)
    spending = period_spending(s, month)
    budgets = list(s.scalars(select(Budget)))
    assert all(b.effective_from.day == 1 for b in budgets)
    assert all(b.effective_from.month == 1 for b in budgets if b.period == "annual")
    overall = _budget(s, None, "monthly", month.end)
    assert overall is not None and overall.amount == cat.OVERALL_TARGET
    rollover = {b.category_id for b in budgets if b.rollover}
    assert rollover == {ids[cat.ROLLOVER]}
    assert any(
        len([b for b in budgets if b.category_id == ids[name]]) >= 2 for name in ("Groceries",)
    )
    over, near, under = [], [], []
    for name in cat.MONTHLY_BUDGETS:
        amount, carry = _available(s, ids[name], month)
        available = amount + carry
        used = spending.amount_for(ids[name]) / available
        (over if used > 1 else near if used >= Decimal("0.8") else under).append((name, used))
    assert [n for n, _ in over] == [cat.OVERSPENT]
    assert [n for n, _ in near] == [cat.NEARLY_FULL]
    assert Decimal("0.83") <= near[0][1] <= Decimal("0.87")
    for name in cat.ANNUAL_BUDGETS:
        annual = _budget(s, ids[name], "annual", month.end)
        monthly = _budget(s, ids[name], "monthly", month.end)
        assert annual is not None and annual.amount and annual.effective_from == dt.date(2026, 1, 1)
        assert monthly is None or monthly.amount is None


def test_budget_alerts_match_this_months_spending(s):
    ids = _category_ids(s)
    keys = set(s.scalars(select(AlertEvent.dedupe_key)))
    start = month_of(TODAY).start.isoformat()
    over, near = ids[cat.OVERSPENT], ids[cat.NEARLY_FULL]
    assert {f"budget:{over}:{start}:80", f"budget:{over}:{start}:100"} <= keys
    assert f"budget:{near}:{start}:80" in keys and f"budget:{near}:{start}:100" not in keys
    budget_keys = {k for k in keys if k.startswith("budget:") and k.endswith(f"{start}:80")}
    assert budget_keys <= {f"budget:{over}:{start}:80", f"budget:{near}:{start}:80"}


# --------------------------------------------------------------------------- connection


def test_the_item_is_healthy_with_consent_due_in_25_days(s, created):
    item = s.scalars(select(PlaidItem)).one()
    clock.set_override(now=local_time(TODAY, 23, 59))
    conn = status.connection_status(s)
    assert conn.state == "healthy" and conn.consent_warning and not conn.stale
    assert conn.primary.consent_days_left == 25
    assert item.institution_id == "ins_10" and item.liabilities_status == "ok"
    assert set(item.consented_products) == {"transactions", "liabilities"}
    assert item.history_complete_at and item.transactions_cursor
    assert item.error_code is None and item.status == "healthy"
    keys = set(s.scalars(select(AlertEvent.dedupe_key)))
    assert f"consent:{item.id}:{conn.primary.consent_due_at.astimezone().date()}" in keys


def test_sync_runs_every_six_hours_with_one_transient_failure(s):
    runs = list(s.scalars(select(SyncRun).order_by(SyncRun.started_at)))
    failed = [r for r in runs if r.status == "error"]
    assert len(failed) == 1 and failed[0].error_code == "INSTITUTION_NOT_RESPONDING"
    assert all(r.status == "success" for r in runs if r is not failed[0])
    scheduled = [r.started_at for r in runs if r.trigger == "schedule"]
    gaps = {round((b - a).total_seconds() / 3600) for a, b in itertools.pairwise(scheduled)}
    assert gaps == {6} and len(scheduled) >= 12
    item = s.scalars(select(PlaidItem)).one()
    last_ok = [r for r in runs if r.status == "success"][-1]
    assert item.last_sync_success_at == last_ok.finished_at
    day = failed[0].started_at.astimezone().date()
    key = f"sync-error:{item.id}:INSTITUTION_NOT_RESPONDING:{day.isoformat()}"
    assert s.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == key))


def test_the_summary_counts_the_rollover(created, s):
    """``demo create`` prints the money available with the carry-in, as the Budgets page will."""
    ids = _category_ids(s)
    amount, carry = _available(s, ids[cat.ROLLOVER], month_of(TODAY))
    (line,) = [x for x in created.month_spending if x.name == cat.ROLLOVER]
    assert carry != ZERO and line.budget == amount and line.carry_in == carry
    assert line.available == amount + carry
    printed = [x for x in created.lines() if x.strip().startswith(cat.ROLLOVER)]
    assert printed and "rolled over" in printed[0]
    assert f"{line.spent / line.available:.0%}" in printed[0]


# --------------------------------------------------------------------------- realism


@pytest.fixture(scope="module", params=[(1, dt.date(2026, 10, 29)), (SEED, TODAY)], ids=str)
def plan(request):
    seed, today = request.param
    return build_plan(seed, today, local_time(today, 23, 59))


def _away(plan) -> set[dt.date]:
    days = set()
    for first, last in plan.trips.values():
        days |= {first + dt.timedelta(days=i) for i in range((last - first).days + 1)}
    return days


def test_nobody_shops_at_home_while_away(plan):
    """In-person charges in San Francisco never fall on trip days (bills still charge)."""
    away = _away(plan)
    assert away
    at_home = [
        r
        for r in plan.rows
        if r.merchant is not None
        and cat.is_home_store(r.merchant)
        and r.auth in away
        and not (r.scheduled and r.merchant.key == "equinox")  # the monthly membership bill
    ]
    assert at_home == []


def test_trips_eat_at_different_places(plan):
    for trip in plan.trips:
        meals = Counter(
            r.merchant.key
            for r in plan.rows
            if r.trip == trip and r.kind == "purchase" and r.category == "Dining & Drinks"
        )
        assert meals and max(meals.values()) <= 2, (trip, meals)
        if trip != "tahoe":  # a two-night ski weekend
            assert len(meals) >= 5, (trip, meals)


def test_the_holidays_and_the_seasons_show(plan):
    months: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    first_full = plan.rows[0].auth.replace(day=1) + dt.timedelta(days=32)
    for r in plan.rows:
        if r.category in ("Shopping", "Gifts & Donations") and r.auth >= first_full.replace(day=1):
            months[(r.auth.year, r.auth.month)] += r.amount
    current = (plan.today.year, plan.today.month)
    usual = statistics.median(v for k, v in months.items() if k[1] not in (11, 12) and k != current)
    for (year, month), total in months.items():
        if month == 12 and (year, month) != current:
            assert total > usual * Decimal("1.3"), (year, month, total, usual)
    pge = {(r.auth.year, r.auth.month): r.amount for r in plan.rows if r.merchant_name == "PG&E"}
    pairs = [
        (pge[(y, 1)], pge[(y, 7)]) for y in {y for y, _ in pge} if (y, 1) in pge and (y, 7) in pge
    ]
    assert pairs and all(winter > summer for winter, summer in pairs)


def test_nothing_is_stored_before_it_reached_plaid():
    """Made at 10 am: today's coffee and lunch reach the app with the 12:15 sync, not before."""
    now = local_time(TODAY, 10, 0)
    morning = build_plan(SEED, TODAY, now)
    last_sync = max(r.started_at for r in morning.sync_runs if r.status == "success")
    assert last_sync < now
    for r in morning.rows:
        if r.kind == "purchase" and r.source == "plaid" and not r.scheduled:
            assert local_time(r.auth, r.arrival_hour) <= last_sync, (r.name, r.auth)
    today = [r for r in morning.rows if r.auth == TODAY and r.kind == "purchase"]
    assert all(r.scheduled for r in today)  # bills charged overnight, nothing else yet
    assert sum(r.pending for r in morning.rows) >= 2  # yesterday's charges are still pending
    evening = build_plan(SEED, TODAY, local_time(TODAY, 23, 59))
    assert any(r.auth == TODAY and not r.scheduled for r in evening.rows)


@pytest.mark.parametrize("seed", range(1, 13))
def test_every_seed_has_the_documented_shape(seed):
    """Agents pick their own seeds: the README's promises must not depend on luck."""
    today = dt.date(2026, 10, 29)
    plan = build_plan(seed, today, local_time(today, 23, 59))
    assert sum(1 for r in plan.rows if r.splits) == 3
    assert sum(1 for r in plan.rows if r.split_amount is not None) == 1  # the needs_review row
    assert sum(r.suspect for r in plan.rows) == 2 and len(plan.statements) == 12
    assert any(r.pending for r in plan.rows)


# --------------------------------------------------------------------------- any day of the month


@pytest.mark.parametrize(
    "today",
    [dt.date(2026, 11, 1), dt.date(2026, 11, 3), dt.date(2027, 2, 28), dt.date(2026, 12, 31)],
)
def test_the_plan_keeps_its_shape_on_any_day(today):
    plan = build_plan(SEED, today, local_time(today, 23, 59))
    month = today.replace(day=1)
    spent: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for row in plan.rows:
        if row.auth >= month and not row.suspect:
            spent[row.category] += row.amount
    for name, amount in cat.MONTHLY_BUDGETS.items():
        used = spent[name] / amount
        if name == cat.OVERSPENT:
            assert used > 1
        elif name == cat.NEARLY_FULL:
            assert Decimal("0.84") <= used <= Decimal("0.86")
        else:
            assert used <= Decimal("0.75")
    assert len(plan.statements) == 12 and sum(r.suspect for r in plan.rows) == 2
    assert any(r.pending for r in plan.rows)


# --------------------------------------------------------------------------- gateway and command


def test_the_demo_gateway_refuses_every_plaid_call_politely():
    gateway = DemoPlaidGateway()
    assert isinstance(gateway, PlaidGateway) and gateway.plaid_env == "sandbox"
    assert gateway_methods(gateway) == ALLOWED_METHODS
    calls = {
        "create_link_token": (),
        "exchange_public_token": ("public-sandbox-x",),
        "transactions_sync": ("access-sandbox-x", None),
        "transactions_refresh": ("access-sandbox-x",),
        "get_item": ("access-sandbox-x",),
        "get_accounts": ("access-sandbox-x",),
        "get_liabilities": ("access-sandbox-x",),
        "search_institutions": ("American Express", ["liabilities"]),
        "sandbox_create_public_token": ("ins_10",),
        "sandbox_reset_login": ("access-sandbox-x",),
        "sandbox_create_transactions": ("access-sandbox-x", "acc", []),
    }
    assert set(calls) == ALLOWED_METHODS
    for method, args in calls.items():
        with pytest.raises(PlaidError) as info:
            getattr(gateway, method)(*args)
        err = info.value
        assert err.display_message == DEMO_MESSAGE and DEMO_MESSAGE in err.error_message
        assert not err.needs_reconnect and not err.retryable and not err.restart_pagination


def test_the_command_refuses_production(capsys):
    assert cli.main(["demo", "create", "x", "--env", "production"]) == 2
    assert "Sandbox-only" in capsys.readouterr().err


def test_the_command_checks_names_and_actions(capsys):
    assert cli.main(["demo", "create", "Bad-Name"]) == 2
    assert "not a demo name" in capsys.readouterr().err
    assert cli.main(["demo", "drop"]) == 2
    assert "needs a NAME" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["demo", "explode"])
    with pytest.raises(SystemExit):
        cli.main(["demo", "create", "x", "--today", "yesterday"])


def test_the_command_lists_drops_and_creates(created, capsys, monkeypatch):
    assert cli.main(["demo", "list"]) == 0
    assert f"{NAME}  (budgetapp_test_demo_{NAME})" in capsys.readouterr().out
    assert cli.main(["demo", "drop", "nosuchdemo"]) == 0
    assert "No demo named 'nosuchdemo'" in capsys.readouterr().out
    seen = {}

    def fake_create(name, *, seed, today):
        seen.update(name=name, seed=seed, today=today)
        return created

    monkeypatch.setattr(demo, "create_demo", fake_create)
    assert cli.main(["demo", "create", "preview", "--seed", "3", "--today", "2026-10-15"]) == 0
    assert seen == {"name": "preview", "seed": 3, "today": TODAY}
    out = capsys.readouterr().out
    assert "is ready" in out and "./run serve --demo preview --port 8800" in out
