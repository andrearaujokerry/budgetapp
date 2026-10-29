"""Writes a :class:`~budgetapp.devtools.demo_plan.Plan` through the models into a migrated database.

Every transaction is categorized by the real :class:`~budgetapp.services.categorize.Categorizer`
(payments → Card Payments as ``system``, rules set ``rule_id``, refunds follow the refund rule);
refunds are categorized last, once the purchases they look back at are stored. User edits
(categories, notes, tags, splits) are applied the way the app stores them, and the one split whose
amount Plaid later changed goes through ``userstate.fit_splits_to_amount`` (rescaled, flagged
``needs_review``). Budget alerts for the current period come from ``services.spending``.
The caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp.db import ledger_lock
from budgetapp.devtools import demo_catalog as cat
from budgetapp.devtools.demo_plan import BudgetPlan, Plan, Row, entity_id, local_time
from budgetapp.models import (
    Account,
    AlertEvent,
    Budget,
    Category,
    CsvImport,
    PlaidItem,
    RecurringOverride,
    Rule,
    Statement,
    SyncRun,
    Transaction,
    TransactionSplit,
    get_app_settings,
)
from budgetapp.money import ZERO, format_money
from budgetapp.periods import Period, month_of, previous_period, year_of
from budgetapp.services import userstate
from budgetapp.services.categorize import Categorizer, apply_decision
from budgetapp.services.spending import period_spending


def write_plan(session: Session, plan: Plan) -> None:
    """Store ``plan`` (flushes; the caller commits)."""
    ledger_lock(session)
    categories = _categories(session)
    rules = _rules(session, categories)
    item = PlaidItem(**plan.item)
    session.add(item)
    session.flush()
    account = Account(item_id=item.id, **plan.account)
    session.add(account)
    session.flush()
    csv_import = CsvImport(account_id=account.id, **plan.csv_import)
    session.add(csv_import)
    session.flush()
    stored = _transactions(session, plan, categories, account.id, csv_import.id)
    _link_rule_origin(rules, plan, stored)
    _statements(session, plan, account.id)
    _budgets(session, plan, categories)
    _sync_runs(session, plan, item.id)
    _alerts(session, plan, item.id, categories)
    for key in plan.recurring_ignored:
        session.add(RecurringOverride(merchant_key=entity_id(key), status="ignored"))
    app = get_app_settings(session)
    for name, value in plan.settings.items():
        setattr(app, name, value)
    app.budget_cycle_account_id = account.id
    session.flush()


# --------------------------------------------------------------------------- reference data


def _categories(session: Session) -> dict[str, Category]:
    name, color, icon = cat.CUSTOM_CATEGORY
    session.add(Category(name=name, kind="expense", color=color, icon=icon, sort_order=145))
    session.flush()
    return {c.name: c for c in session.scalars(select(Category))}


def _rules(session: Session, categories: dict[str, Category]) -> dict[str, Rule]:
    rules = {}
    for spec in cat.RULES:
        rule = Rule(
            name=spec.name,
            priority=spec.priority,
            is_active=spec.is_active,
            merchant_pattern=spec.merchant_pattern,
            description_pattern=spec.description_pattern,
            match_type=spec.match_type,
            amount_min=spec.amount_min,
            amount_max=spec.amount_max,
            category_id=categories[spec.category].id,
        )
        session.add(rule)
        rules[spec.name] = rule
    session.flush()
    return rules


def _link_rule_origin(rules: dict[str, Rule], plan: Plan, stored: dict[int, Transaction]) -> None:
    """One-click rules remember the transaction they were made from."""
    for spec in cat.RULES:
        if spec.from_merchant is None:
            continue
        origin = next(
            (
                r
                for r in plan.rows
                if r.merchant is not None
                and r.merchant.key == spec.from_merchant
                and r.source == "plaid"
                and r.auth > plan.linked_at.date()
            ),
            None,
        )
        if origin is not None:
            rules[spec.name].source_transaction_id = stored[origin.seq].id


# --------------------------------------------------------------------------- transactions


def _columns(row: Row, account_id: int, csv_import_id: int) -> dict[str, Any]:
    common = {
        "account_id": account_id,
        "amount": row.amount,
        "iso_currency_code": "USD",
        "csv_reference": row.csv_reference,
        "csv_import_id": csv_import_id if row.csv_reference else None,
        "category_id": 0,  # set by the Categorizer before the row is flushed
        "category_source": "default",
    }
    if row.source == "csv":
        return common | {
            "source": "csv",
            "pending": False,
            "date": row.csv_date,
            "authorized_date": None,
            "posted_date": row.posted,
            "name": row.csv_description,
            "original_description": row.raw,
            "external_category": row.amex or None,
            "account_owner": row.account_owner,
            "duplicate_status": "suspect" if row.suspect else "none",
        }
    merchant = row.merchant
    return common | {
        "source": "plaid",
        "plaid_transaction_id": row.plaid_id,
        "pending_transaction_id": row.pending_id,
        "pending": row.pending,
        "date": row.posted or row.auth,
        "authorized_date": row.auth,
        "posted_date": row.posted,
        "name": row.name,
        "merchant_name": row.merchant_name,
        "original_description": row.raw,
        "pfc_primary": row.pfc[0] if row.pfc else None,
        "pfc_detailed": row.pfc[1] if row.pfc else None,
        "pfc_confidence": row.confidence if row.pfc else None,
        "pfc_version": "v2" if row.pfc else None,
        "transaction_code": row.code,
        "merchant_category_code": merchant.mcc if merchant else None,
        "merchant_entity_id": entity_id(merchant.key) if merchant else None,
        "payment_channel": row.channel,
        "website": merchant.website if merchant else None,
        "location": row.location,
    }


def _transactions(
    session: Session,
    plan: Plan,
    categories: dict[str, Category],
    account_id: int,
    csv_import_id: int,
) -> dict[int, Transaction]:
    categorizer = Categorizer.load(session)
    stored: dict[int, Transaction] = {}
    refunds: list[tuple[Row, Transaction]] = []
    for row in plan.rows:
        txn = Transaction(**_columns(row, account_id, csv_import_id))
        stored[row.seq] = txn
        if row.kind == "refund":
            refunds.append((row, txn))
            continue
        apply_decision(txn, categorizer.decide(txn))
        session.add(txn)
    session.flush()
    for row in plan.rows:
        if row.kind != "refund":
            _apply_user_state(stored[row.seq], row, categories)
    session.flush()
    # Refunds look back at the merchant's purchases (and their final categories).
    categorizer = Categorizer.load(session)
    for row, txn in refunds:
        apply_decision(txn, categorizer.decide(txn))
        session.add(txn)
        _apply_user_state(txn, row, categories)
    session.flush()
    return stored


def _apply_user_state(txn: Transaction, row: Row, categories: dict[str, Category]) -> None:
    if row.user_category is not None:
        txn.category_id = categories[row.user_category].id
        txn.category_source = "user"
        txn.rule_id = None
    if row.notes:
        txn.notes = row.notes
    if row.tags:
        txn.tags = userstate.normalize_tags(row.tags)
    for name, amount, note in row.splits:
        txn.splits.append(
            TransactionSplit(category_id=categories[name].id, amount=amount, note=note)
        )
    if row.split_amount is not None and row.splits:
        # The splits were made for the first amount; Plaid then changed it (incidentals).
        userstate.fit_splits_to_amount(txn)
    if row.edited_at is not None and (row.user_category or row.notes or row.tags or row.splits):
        txn.user_edited_at = row.edited_at


# --------------------------------------------------------------------------- statements, budgets


def _statements(session: Session, plan: Plan, account_id: int) -> None:
    for s in plan.statements:
        session.add(
            Statement(
                account_id=account_id,
                closing_date=s.closing_date,
                closing_date_source="liabilities",
                plaid_issue_date=s.closing_date,
                period_start_override=s.period_start_override,
                reported_new_balance=s.new_balance,
                reported_new_balance_source="liabilities",
                reported_previous_balance=s.previous_balance,
                reported_payments_credits=s.payments_credits,
                reported_new_charges=s.new_charges,
                reported_fees=s.fees,
                reported_interest=s.interest,
                checked_at=s.checked_at,
                notes=s.notes,
            )
        )


def _budgets(session: Session, plan: Plan, categories: dict[str, Category]) -> None:
    for b in plan.budgets:
        session.add(
            Budget(
                scope="overall" if b.category is None else "category",
                category_id=None if b.category is None else categories[b.category].id,
                period=b.period,
                amount=b.amount,
                rollover=b.rollover,
                effective_from=b.effective_from,
            )
        )


def _sync_runs(session: Session, plan: Plan, item_id: int) -> None:
    for run in plan.sync_runs:
        session.add(
            SyncRun(
                item_id=item_id,
                trigger=run.trigger,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                added_count=run.added,
                modified_count=run.modified,
                removed_count=run.removed,
                update_status="HISTORICAL_UPDATE_COMPLETE" if run.status == "success" else None,
                error_code=run.error_code,
                error_message=run.error_message,
                created_at=run.started_at,
                updated_at=run.finished_at,
            )
        )


# --------------------------------------------------------------------------- alerts


def _event(
    kind: str,
    key: str,
    title: str,
    message: str,
    created_at: dt.datetime,
    *,
    category_id: int | None = None,
    item_id: int | None = None,
) -> AlertEvent:
    return AlertEvent(
        kind=kind,
        dedupe_key=key,
        title=title,
        message=message,
        category_id=category_id,
        item_id=item_id,
        delivered=True,
        delivered_at=created_at + dt.timedelta(seconds=2),
        created_at=created_at,
        updated_at=created_at + dt.timedelta(seconds=2),
    )


def _alerts(session: Session, plan: Plan, item_id: int, categories: dict[str, Category]) -> None:
    for alert in plan.alerts:
        session.add(
            _event(
                alert.kind,
                alert.dedupe_key.replace("{item_id}", str(item_id)),
                alert.title,
                alert.message,
                alert.created_at,
                item_id=item_id,
            )
        )
    session.flush()
    for event in budget_alerts(session, plan, categories):
        session.add(event)


def _amount_for(
    budgets: list[BudgetPlan], category: str | None, period: Period
) -> BudgetPlan | None:
    """The budget row in force for ``period`` (latest ``effective_from <= period.end``)."""
    kind = "annual" if period.kind == "year" else "monthly"
    rows = [
        b
        for b in budgets
        if b.category == category and b.period == kind and b.effective_from <= period.end
    ]
    return max(rows, key=lambda b: b.effective_from) if rows else None


def _available(
    session: Session, budgets: list[BudgetPlan], category: Category, period: Period
) -> Decimal | None:
    """Budget amount plus the rollover carry-in (ARCHITECTURE §6 "Budget math")."""
    row = _amount_for(budgets, category.name, period)
    if row is None or row.amount is None:
        return None
    if not row.rollover:
        return row.amount
    chain = [period]
    while True:
        earlier = previous_period(chain[-1])
        version = _amount_for(budgets, category.name, earlier)
        if version is None or version.amount is None or not version.rollover:
            break
        chain.append(earlier)
    carry = ZERO
    for p in reversed(chain[1:]):
        version = _amount_for(budgets, category.name, p)
        assert version is not None and version.amount is not None
        spent = period_spending(session, p).amount_for(category.id)
        carry = version.amount + carry - spent
    return row.amount + carry


def budget_alerts(
    session: Session, plan: Plan, categories: dict[str, Category]
) -> list[AlertEvent]:
    """The current period's budget threshold alerts (ALR-1), as ``evaluate_budgets`` would."""
    thresholds = plan.settings["alert_thresholds"]
    month = month_of(plan.today)
    year = year_of(plan.today)
    spent_month = period_spending(session, month)
    spent_year = period_spending(session, year)
    annual = set(cat.ANNUAL_BUDGETS)
    lines: list[tuple[str, int | None, Period, Decimal, Decimal]] = []
    for name in cat.MONTHLY_BUDGETS:
        category = categories[name]
        available = _available(session, plan.budgets, category, month)
        if available is not None:
            lines.append((name, category.id, month, available, spent_month.amount_for(category.id)))
    for name in cat.ANNUAL_BUDGETS:
        category = categories[name]
        row = _amount_for(plan.budgets, name, year)
        if row is not None and row.amount is not None:
            lines.append((name, category.id, year, row.amount, spent_year.amount_for(category.id)))
    overall = _amount_for(plan.budgets, None, month)
    if overall is not None and overall.amount is not None:
        spent = sum((r.amount for r in spent_month.categories if r.name not in annual), ZERO)
        lines.append(("Overall", None, month, overall.amount, spent))
    events = []
    for name, category_id, period, available, spent in lines:
        for threshold in thresholds:
            if spent <= 0 or spent < available * threshold / 100:
                continue
            key = f"budget:{category_id or 'overall'}:{period.start.isoformat()}:{threshold}"
            label = "your overall budget" if category_id is None else name
            when = "this year" if period.kind == "year" else f"in {period.label}"
            if threshold >= 100:
                title = f"{name} is over budget" if category_id else "Over your overall budget"
            else:
                title = f"{name} has used {threshold}% of its budget"
                if category_id is None:
                    title = f"You've used {threshold}% of your overall budget"
            message = (
                f"You've spent {format_money(spent)} of {format_money(available)} on {label} "
                f"{when}."
            )
            created = _crossing_time(
                plan, None if category_id is None else name, period, available * threshold / 100
            )
            events.append(_event("budget", key, title, message, created, category_id=category_id))
    return events


def _crossing_time(plan: Plan, category: str | None, period: Period, level: Decimal) -> dt.datetime:
    """Roughly when spending reached ``level``: the evening sync after the crossing charge."""
    skip = {"Card Payments", "Credits & Rewards", *cat.ANNUAL_BUDGETS}
    total = ZERO
    day = plan.today
    for row in sorted(plan.rows, key=lambda r: (r.auth, r.seq)):
        name = row.user_category or row.category
        counts = name == category if category is not None else name not in skip
        if counts and not row.suspect and period.start <= row.auth <= period.end:
            total += row.amount
            if total >= level:
                day = row.auth
                break
    return min(local_time(day, 18, 15) + dt.timedelta(seconds=25), plan.now)
