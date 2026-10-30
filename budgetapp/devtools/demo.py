"""Demo databases: ``budgetapp_test_demo_<NAME>`` filled with realistic, made-up card data.

A demo database lives in the dedicated cluster next to the test databases and is reached the same
way: as the non-superuser ``budgetapp_test`` over the private socket (peer authentication), with
test-environment settings. It never touches the Sandbox or production databases, Keychain or Plaid.

* :func:`create_demo` drops and recreates the database, migrates it to head and fills it with
  about 24 months of data (see ``demo_plan`` and ``README.md``). Deterministic for
  ``(seed, today)``; sync timestamps never lie in the future, so for today's date they stop at
  the current time.
* :func:`demo_settings` gives the settings ``./run serve --demo NAME`` serves with.
* :func:`drop_demo` and :func:`list_demos`.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy import exc as sa_exc
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.config import ConfigError, Settings, load_settings
from budgetapp.db import (
    code_head,
    current_revision,
    drop_database,
    make_engine,
    make_session_factory,
    run_migrations,
    transaction,
)
from budgetapp.devtools import demo_catalog as cat
from budgetapp.devtools.demo_plan import Plan, build_plan, local_time
from budgetapp.devtools.demo_writer import write_plan
from budgetapp.models import (
    Account,
    AlertEvent,
    Budget,
    Category,
    CategoryMapping,
    CsvImport,
    PlaidItem,
    RecurringOverride,
    Rule,
    Statement,
    SyncRun,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
)
from budgetapp.periods import Period, month_of, previous_period
from budgetapp.services.spending import period_spending

log = logging.getLogger(__name__)

DEMO_DB_PREFIX = "budgetapp_test_demo_"
DEMO_PORT = 8800
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_]{0,19}")
COUNTED_TABLES = (
    PlaidItem,
    Account,
    Category,
    CategoryMapping,
    Rule,
    CsvImport,
    Transaction,
    TransactionSplit,
    TransactionTombstone,
    Budget,
    Statement,
    SyncRun,
    AlertEvent,
    RecurringOverride,
)


class DemoError(Exception):
    """A demo database cannot be created, found or reached (the message says what to do)."""


@dataclass(frozen=True, slots=True)
class CategoryLine:
    """This month's spending in one category, with its monthly budget when it has one.

    ``carry_in`` is what rolled over from earlier months (BUD-4; may be negative), so the money
    available this month is ``budget + carry_in``.
    """

    name: str
    spent: Decimal
    budget: Decimal | None
    carry_in: Decimal = Decimal("0.00")

    @property
    def available(self) -> Decimal | None:
        return None if self.budget is None else self.budget + self.carry_in


@dataclass(frozen=True, slots=True)
class DemoSummary:
    """What :func:`create_demo` made."""

    name: str
    db_name: str
    seed: int
    today: dt.date
    seconds: float
    counts: dict[str, int]
    transactions: dict[str, int]
    first_date: dt.date
    last_date: dt.date
    plaid_history_start: dt.date
    statements: tuple[dt.date, dt.date]
    month_label: str
    month_spending: tuple[CategoryLine, ...]
    month_total: Decimal

    def lines(self) -> list[str]:
        """A short human report (the ``demo create`` output)."""
        t = self.transactions
        out = [
            f"Demo data '{self.name}' is ready in {self.seconds:.1f} s "
            f"(database {self.db_name}, seed {self.seed}, today {self.today.isoformat()}).",
            f"  {t['total']:,} transactions from {_day(self.first_date)} to "
            f"{_day(self.last_date)}: {t['plaid']:,} from Plaid ({t['pending']} pending), "
            f"{t['csv']:,} from an Amex CSV ({t['suspect']} suspect duplicates); "
            f"Plaid history starts {_day(self.plaid_history_start)}.",
            f"  {t['split']} split, {t['user_edited']} edited by the user, "
            f"{t['needs_review']} needs review; statements {self.statements[0]:%b %Y} – "
            f"{self.statements[1]:%b %Y}.",
            "  Rows per table: "
            + ", ".join(f"{table} {count:,}" for table, count in self.counts.items()),
            f"This month ({self.month_label}), spending by category (services.spending):",
        ]
        width = max((len(line.name) for line in self.month_spending), default=10)
        for line in self.month_spending:
            budget = ""
            available = line.available
            if line.budget and available is not None:
                used = f"{line.spent / available:>5.0%}" if available > 0 else "    –"
                budget = f"  of {_money(available):>9}  {used}"
                if line.carry_in:
                    budget += f"  (incl. {_money(line.carry_in)} rolled over)"
            out.append(f"  {line.name:<{width}}  {_money(line.spent):>10}{budget}")
        out.append(f"  {'Total':<{width}}  {_money(self.month_total):>10}")
        return out


def _day(day: dt.date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _money(amount: Decimal) -> str:
    sign = "−" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


# --------------------------------------------------------------------------- names and settings


def demo_db_name(name: str) -> str:
    """``budgetapp_test_demo_<name>``; ``name`` must match ``[a-z0-9][a-z0-9_]{0,19}``."""
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise DemoError(
            f"{name!r} is not a demo name: use 1–20 lowercase letters, digits or _ "
            "(starting with a letter or digit), e.g. ./run demo create budgets"
        )
    return DEMO_DB_PREFIX + name


def _settings(name: str, port: int = DEMO_PORT) -> Settings:
    try:
        return load_settings("test", db_name=demo_db_name(name), web_port=port)
    except ConfigError as exc:
        raise DemoError(str(exc)) from exc


def _maintenance(settings: Settings) -> psycopg.Connection:
    """Autocommit connection to ``postgres`` as ``budgetapp_test`` (never a superuser)."""
    try:
        return psycopg.connect(
            host=str(settings.pg_socket_dir),
            port=settings.pg_port,
            dbname="postgres",
            user=settings.db_role,
            autocommit=True,
            connect_timeout=10,
            application_name="budgetapp-demo",
        )
    except psycopg.OperationalError as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise DemoError(
            f"Can't reach the budgetapp database cluster on port {settings.pg_port} ({detail}). "
            "Start it with ./run db start."
        ) from exc


def _exists(conn: psycopg.Connection, db_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)).fetchone()
    return row is not None


def _drop(conn: psycopg.Connection, db_name: str) -> None:
    if not db_name.startswith(DEMO_DB_PREFIX):  # pragma: no cover - names are validated
        raise DemoError(f"refusing to drop {db_name!r}")
    drop_database(conn, db_name)


def demo_settings(name: str, *, port: int = DEMO_PORT) -> Settings:
    """Test-environment settings for serving demo ``name`` on 127.0.0.1:``port``.

    Raises :class:`DemoError` when the database is missing ("run ./run demo create NAME first")
    or was made for another schema version.
    """
    settings = _settings(name, port)
    with _maintenance(settings) as conn:
        if not _exists(conn, settings.db_name):
            raise DemoError(
                f"There is no demo database {name!r}: run ./run demo create {name} first."
            )
    engine = make_engine(settings)
    try:
        revision = current_revision(engine)
    except sa_exc.DBAPIError as exc:
        raise DemoError(f"Can't read demo database {name!r}: {exc.orig or exc}") from exc
    finally:
        engine.dispose()
    if revision != code_head():
        raise DemoError(
            f"Demo database {name!r} was made for schema {revision or 'nothing'}, but this code "
            f"is at {code_head()}: run ./run demo create {name} again."
        )
    return settings


# --------------------------------------------------------------------------- create, drop, list


def create_demo(name: str, *, seed: int = 1, today: dt.date | None = None) -> DemoSummary:
    """(Re)create demo ``name``: drop, create as ``budgetapp_test``, migrate, fill.

    Deterministic for ``(seed, today)``; ``today`` defaults to ``clock.today()``. A failure
    drops the half-made database again.
    """
    settings = _settings(name)
    started = time.monotonic()
    real_today = clock.today()
    today = today or real_today
    now = clock.now() if today == real_today else local_time(today, 23, 59)
    plan = build_plan(seed, today, now)
    with _maintenance(settings) as conn:
        _drop(conn, settings.db_name)
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.db_name)))
    engine = make_engine(settings)
    try:
        run_migrations(engine)
        factory = make_session_factory(engine)
        with transaction(factory) as session:
            write_plan(session, plan)
        with factory() as session:
            summary = _summary(session, name, settings.db_name, seed, today, plan)
    except BaseException:
        engine.dispose()
        try:
            with _maintenance(settings) as conn:
                _drop(conn, settings.db_name)
        except Exception:  # keep the original error
            log.warning("demo: could not drop the half-made %s", settings.db_name, exc_info=True)
        raise
    finally:
        engine.dispose()
    summary = replace(summary, seconds=time.monotonic() - started)
    log.info("demo: created %s (%d transactions)", settings.db_name, summary.transactions["total"])
    return summary


def drop_demo(name: str) -> bool:
    """Drop demo ``name``; ``True`` when it existed."""
    settings = _settings(name)
    with _maintenance(settings) as conn:
        existed = _exists(conn, settings.db_name)
        _drop(conn, settings.db_name)
    return existed


def list_demos() -> list[str]:
    """Names of the demo databases in the cluster, sorted."""
    settings = load_settings("test")
    with _maintenance(settings) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY datname",
            (DEMO_DB_PREFIX.replace("_", r"\_") + "%",),
        ).fetchall()
    return [row[0].removeprefix(DEMO_DB_PREFIX) for row in rows]


# --------------------------------------------------------------------------- summary


def _monthly_budget(session: Session, category_id: int, on: dt.date) -> Budget | None:
    """The category's monthly budget row in force on ``on`` (versioned by ``effective_from``)."""
    return session.scalars(
        select(Budget)
        .where(
            Budget.category_id == category_id,
            Budget.period == "monthly",
            Budget.effective_from <= on,
        )
        .order_by(Budget.effective_from.desc())
        .limit(1)
    ).first()


def _carry_in(session: Session, category_id: int, month: Period) -> Decimal:
    """What rolled into ``month`` (ARCHITECTURE §5: the previous period's available − spent,
    starting at 0 in the first rollover period; may be negative)."""
    current = _monthly_budget(session, category_id, month.end)
    if current is None or not current.rollover:
        return Decimal("0.00")
    chain: list[tuple[Period, Decimal]] = []
    period = previous_period(month)
    while (row := _monthly_budget(session, category_id, period.end)) is not None and (
        row.rollover and row.amount is not None
    ):
        chain.append((period, row.amount))
        period = previous_period(period)
    carry = Decimal("0.00")
    for period, amount in reversed(chain):
        carry = amount + carry - period_spending(session, period).amount_for(category_id)
    return carry


def _summary(
    session: Session, name: str, db_name: str, seed: int, today: dt.date, plan: Plan
) -> DemoSummary:
    counts = {
        model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
        for model in COUNTED_TABLES
    }
    t = Transaction

    def count(*conditions: Any) -> int:
        return session.scalar(select(func.count()).select_from(t).where(*conditions)) or 0

    transactions = {
        "total": counts["transactions"],
        "plaid": count(t.source == "plaid"),
        "csv": count(t.source == "csv"),
        "pending": count(t.pending.is_(True)),
        "suspect": count(t.duplicate_status == "suspect"),
        "split": session.scalar(select(func.count(func.distinct(TransactionSplit.transaction_id))))
        or 0,
        "user_edited": count(t.user_edited_at.is_not(None)),
        "needs_review": count(t.needs_review.is_(True)),
    }
    first, last = session.execute(
        select(func.min(func.coalesce(t.authorized_date, t.date)), func.max(t.date))
    ).one()
    month = month_of(today)
    spending = period_spending(session, month)
    budgets = cat.MONTHLY_BUDGETS
    ids = {c.name: c.id for c in session.scalars(select(Category))}

    def category_line(name: str, spent: Decimal) -> CategoryLine:
        budget = budgets.get(name)
        carry = _carry_in(session, ids[name], month) if budget is not None else Decimal("0.00")
        return CategoryLine(name, spent, budget, carry)

    lines = tuple(category_line(row.name, row.amount) for row in spending.categories)
    names = {line.name for line in lines}
    lines += tuple(
        category_line(n, Decimal("0.00")) for n in budgets if n not in names and n in ids
    )
    statements = sorted(s.closing_date for s in plan.statements)
    return DemoSummary(
        name=name,
        db_name=db_name,
        seed=seed,
        today=today,
        seconds=0.0,
        counts=counts,
        transactions=transactions,
        first_date=first,
        last_date=last,
        plaid_history_start=plan.plaid_history_start,
        statements=(statements[0], statements[-1]),
        month_label=month.label,
        month_spending=lines,
        month_total=spending.total,
    )
