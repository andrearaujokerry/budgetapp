"""``./run sync`` and the Sandbox helpers (ARCHITECTURE §8, §10; SYNC-2, REL-1).

Commands run with an injected ``Runtime``: the test session factory, ``FakePlaidGateway``, the
in-memory secret store and the recording notifier. Nothing builds a real engine or gateway, reads
the Keychain or calls Plaid. Production paths use ``load_settings("production", <temp paths>)``.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from budgetapp import cli
from budgetapp import db as app_db
from budgetapp import secrets as app_secrets
from budgetapp.commands import sandbox as sandbox_cmd
from budgetapp.commands import sync as sync_cmd
from budgetapp.config import load_settings
from budgetapp.models import PlaidItem, SyncRun, Transaction
from budgetapp.services import sync
from budgetapp.services.events import SyncOutcome
from tests.factories import make_item

NOW = dt.datetime(2026, 10, 20, 15, 0, tzinfo=dt.UTC)
Runtime = sync_cmd.Runtime  # the real class (some tests monkeypatch the module's name)


@pytest.fixture(autouse=True)
def at_now(freeze_today):
    freeze_today(now=NOW)


@pytest.fixture
def unattended_calls(monkeypatch) -> list[bool]:
    calls: list[bool] = []

    def enable() -> bool:
        calls.append(True)
        return True

    monkeypatch.setattr(app_secrets, "enable_unattended_mode", enable)
    return calls


@pytest.fixture
def state_settings(settings, tmp_path):
    """Test settings with a state folder of this test's own (daily markers)."""
    return settings.replace(state_dir=tmp_path / "state")


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setattr(app_secrets, "fallback_recovery_dir", lambda: tmp_path / "fallback")
    return load_settings(
        "production",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        install_dir=tmp_path / "venv",
    )


class Out(list):
    """Collects what a command prints."""

    def __call__(self, line: str) -> None:
        self.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self)


@pytest.fixture
def out() -> Out:
    return Out()


@pytest.fixture
def make_rt(session_factory, fake_plaid, secrets, notifier, settings, out):
    """A Runtime on the test database and fakes; ``slept`` records the delays."""

    def make(app_settings=None, **kwargs: Any) -> Runtime:
        slept: list[float] = []
        kwargs.setdefault("session_factory", session_factory)
        kwargs.setdefault("gateway", fake_plaid)
        kwargs.setdefault("store", secrets)
        kwargs.setdefault("notifier", notifier)
        kwargs.setdefault("sleep", slept.append)
        kwargs.setdefault("out", out)
        rt = Runtime(settings if app_settings is None else app_settings, **kwargs)
        rt.slept = slept  # type: ignore[attr-defined]
        return rt

    return make


def args(**values: Any) -> Any:
    defaults = {"if_due": False, "min_age_hours": 5.0, "unattended": False, "item": None}
    defaults.update(values)
    return type("Args", (), defaults)()


def link(db, fake_plaid, secrets, settings, *, item_id: str = "item-amex", **values: Any):
    """A fake Plaid Item linked in the test database, with its token saved."""
    fake_item = fake_plaid.add_item(item_id=item_id)
    app_secrets.store_access_token(settings, item_id, fake_item.access_token, store=secrets)
    values.setdefault("last_sync_success_at", None)
    return fake_item, make_item(db, plaid_item_id=item_id, **values)


def runs(db) -> list[SyncRun]:
    db.expire_all()
    return list(db.scalars(select(SyncRun).order_by(SyncRun.id)))


# --------------------------------------------------------------------------- ./run sync


def test_sync_with_nothing_linked_says_so(make_rt, out):
    assert sync_cmd.sync_command(args(), make_rt()) == 0
    assert out == ["Nothing is linked yet. Link your Amex card on the Connection page."]


def test_sync_syncs_every_item_and_reports_the_counts(
    db, make_rt, out, fake_plaid, secrets, settings
):
    fake_item, item = link(db, fake_plaid, secrets, settings)
    fake_plaid.add_transaction(fake_item, name="Coffee", amount=Decimal("4.50"))
    fake_plaid.add_transaction(fake_item, name="Lunch", amount=Decimal("12.00"))
    assert sync_cmd.sync_command(args(), make_rt()) == 0
    assert out == [
        "American Express: 2 new, 0 changed, 0 removed. Plaid has now sent the full history."
    ]
    (run,) = runs(db)
    assert (run.trigger, run.status, run.added_count) == ("cli", "success", 2)
    out.clear()
    assert sync_cmd.sync_command(args(), make_rt()) == 0
    assert out == ["American Express: no changes."]


def test_unattended_runs_are_scheduled_runs_without_keychain_prompts(
    db, make_rt, fake_plaid, secrets, settings, unattended_calls
):
    link(db, fake_plaid, secrets, settings)
    assert sync_cmd.sync_command(args(unattended=True), make_rt()) == 0
    assert unattended_calls == [True]
    assert [r.trigger for r in runs(db)] == ["schedule"]


def test_if_due_skips_items_synced_recently(db, make_rt, out, fake_plaid, secrets, settings):
    link(db, fake_plaid, secrets, settings, last_sync_success_at=NOW - dt.timedelta(hours=2))
    assert sync_cmd.sync_command(args(if_due=True), make_rt()) == 0
    assert out == ["American Express: synced recently; not due yet."]
    assert runs(db) == [] and fake_plaid.calls_to("transactions_sync") == []
    out.clear()
    assert sync_cmd.sync_command(args(if_due=True, min_age_hours=1.0), make_rt()) == 0
    assert [r.status for r in runs(db)] == ["success"]


def test_one_item_by_id_and_an_unknown_id(db, make_rt, out, fake_plaid, secrets, settings):
    _, first = link(db, fake_plaid, secrets, settings, item_id="one")
    _, second = link(db, fake_plaid, secrets, settings, item_id="two")
    assert sync_cmd.sync_command(args(item=second.id), make_rt()) == 0
    assert [r.item_id for r in runs(db)] == [second.id]
    assert sync_cmd.sync_command(args(item=999999), make_rt()) == 2
    assert "there is no Item 999999" in out.text


def test_a_negative_age_is_a_usage_error(make_rt):
    assert sync_cmd.sync_command(args(min_age_hours=-1.0), make_rt()) == 2


def test_a_failed_sync_exits_1_and_says_what_to_do(db, make_rt, out, fake_plaid, secrets, settings):
    fake_item, item = link(db, fake_plaid, secrets, settings)
    fake_plaid.set_item_error(fake_item, "ITEM_LOGIN_REQUIRED")
    assert sync_cmd.sync_command(args(), make_rt()) == 1
    assert "failed (ITEM_LOGIN_REQUIRED)" in out.text and "click Reconnect" in out.text
    db.expire_all()
    assert db.get(PlaidItem, item.id).status == "needs_reconnect"


def test_an_unexpected_failure_exits_1(
    db, make_rt, out, fake_plaid, secrets, settings, monkeypatch
):
    link(db, fake_plaid, secrets, settings)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(sync._Applier, "apply_upserts", boom)
    fake_plaid.add_transaction(fake_plaid.items["item-amex"], name="X")
    assert sync_cmd.sync_command(args(), make_rt()) == 1
    assert "failed unexpectedly" in out.text
    assert runs(db)[0].error_code == "INTERNAL"


def test_keychain_approval_notifies_once_a_day_when_unattended(
    db, make_rt, out, fake_plaid, secrets, state_settings, notifier, unattended_calls
):
    link(db, fake_plaid, secrets, state_settings)
    key = app_secrets.sandbox_access_token_key("item-amex")
    secrets.fail_on[("get", key)] = app_secrets.KeychainApprovalNeeded(key, -25308)
    for _ in range(2):
        assert sync_cmd.sync_command(args(unattended=True), make_rt(state_settings)) == 75
    assert notifier.titles == ["Budget Tracker needs Keychain approval"]
    assert out == []  # launchd: the notification is the message
    assert sync_cmd.sync_command(args(), make_rt(state_settings)) == 75
    assert "./run doctor --env production" in out.text
    assert len(notifier.titles) == 1


class Unavailable(Runtime):
    def ensure_database(self) -> None:
        raise app_db.DatabaseUnavailable("connection refused")


class Upgrade(Runtime):
    def ensure_database(self) -> None:
        raise app_db.SchemaMismatch("0002_seed_defaults", "0003_f1_verification_fixes")


def test_a_database_that_is_not_ready_ends_an_unattended_run_quietly(
    session_factory, fake_plaid, secrets, notifier, settings, out
):
    deps = {"gateway": fake_plaid, "store": secrets, "notifier": notifier, "out": out}
    rt = Unavailable(settings, session_factory=session_factory, **deps)
    assert sync_cmd.sync_command(args(unattended=True), rt) == 75
    assert out == [] and notifier.titles == []
    assert sync_cmd.sync_command(args(), rt) == 1
    assert out == ["The database isn't responding. Start it with: ./run db start"]


def test_a_production_schema_upgrade_is_announced_once_a_day(
    session_factory, fake_plaid, secrets, notifier, production, out, unattended_calls
):
    deps = {"gateway": fake_plaid, "store": secrets, "notifier": notifier, "out": out}
    rt = Upgrade(production, session_factory=session_factory, **deps)
    assert sync_cmd.sync_command(args(unattended=True), rt) == 75
    assert sync_cmd.sync_command(args(unattended=True), rt) == 75
    assert notifier.titles == ["Budget Tracker needs an upgrade"]
    assert "./run live" in notifier.sent[0].message
    assert sync_cmd.sync_command(args(), rt) == 1
    assert out == ["The database needs an upgrade before syncing. Run: ./run live"]


def test_missing_plaid_keys_are_explained(
    db, session_factory, fake_plaid, secrets, notifier, settings, out
):
    link(db, fake_plaid, secrets, settings)

    class NoKeys(Runtime):
        @property
        def gateway(self):
            raise app_secrets.MissingSecretError(app_secrets.PLAID_CLIENT_ID)

    rt = NoKeys(
        settings, session_factory=session_factory, store=secrets, notifier=notifier, out=out
    )
    assert sync_cmd.sync_command(args(), rt) == 1
    assert out == ["The Plaid keys aren't saved yet. Save them with: ./run setup --keys-only"]
    assert fake_plaid.calls == []


def test_the_cli_runs_the_sync_command(
    db, monkeypatch, make_rt, fake_plaid, secrets, settings, capsys
):
    link(db, fake_plaid, secrets, settings)
    built: list[Runtime] = []

    def runtime(app_settings):
        rt = make_rt(app_settings, out=print)
        built.append(rt)
        return rt

    monkeypatch.setattr(sync_cmd, "Runtime", runtime)
    assert cli.main(["sync", "--item", str(db.scalars(select(PlaidItem.id)).one())]) == 0
    assert "American Express: no changes." in capsys.readouterr().out
    assert len(built) == 1


def test_describe_outcome_covers_every_status():
    base = {"item_id": 1, "trigger": "cli"}
    assert "skipped" in sync_cmd.describe_outcome("Amex", SyncOutcome(status="skipped", **base))
    assert "preparing" in sync_cmd.describe_outcome("Amex", SyncOutcome(status="not_ready", **base))
    loading = SyncOutcome(
        status="success", added=3, update_status="INITIAL_UPDATE_COMPLETE", **base
    )
    assert sync_cmd.describe_outcome("Amex", loading) == (
        "Amex: 3 new, 0 changed, 0 removed. Plaid is still loading older history."
    )
    failed = SyncOutcome(
        status="error", error_code="INSTITUTION_DOWN", error_type="INSTITUTION_ERROR", **base
    )
    assert "Amex isn't answering Plaid" in sync_cmd.describe_outcome("Amex", failed)


# --------------------------------------------------------------------------- Sandbox helpers


@pytest.mark.parametrize(
    ("command", "options"),
    [
        (sandbox_cmd.link_command, {"user": "custom", "institution": "ins_109508"}),
        (sandbox_cmd.refresh_command, {"item": None}),
        (sandbox_cmd.reset_login_command, {"item": None}),
    ],
    ids=["link", "refresh", "reset-login"],
)
def test_sandbox_helpers_refuse_production(make_rt, production, fake_plaid, out, command, options):
    assert command(args(**options), make_rt(production)) == 2
    assert "works only in the Sandbox" in out.text
    assert fake_plaid.calls == []


def test_the_custom_user_is_an_amex_like_card_with_known_totals():
    today = dt.date(2026, 11, 3)
    config = sandbox_cmd.custom_user_config(today)
    (account,) = config["override_accounts"]
    assert (account["type"], account["subtype"], account["meta"]["mask"]) == (
        "credit",
        "credit card",
        "1005",
    )
    rows = account["transactions"]
    assert [r["date_posted"] for r in rows][-1] == "2026-11-06"  # pending at creation
    assert rows[0] == {
        "date_transacted": "2026-09-18",
        "date_posted": "2026-09-19",
        "amount": Decimal("54.17"),
        "description": "WHOLE FOODS MARKET #10234 NEW YORK NY",
        "currency": "USD",
    }
    assert sandbox_cmd.custom_posted_total() == Decimal("793.61")
    assert sandbox_cmd.custom_pending_total() == Decimal("42.10")
    assert account["liability"]["purchase_apr"] == 0  # no interest: exact totals
    username, password = sandbox_cmd.credentials("custom", today)
    assert username == "user_custom"
    parsed = json.loads(password, parse_float=Decimal)  # exact amounts on the wire
    assert parsed["override_accounts"][0]["transactions"][3]["amount"] == Decimal("-89.99")
    assert sandbox_cmd.credentials("user_good", today) == ("user_good", "pass_good")
    with pytest.raises(ValueError):
        sandbox_cmd.credentials("user_bad", today)


def test_the_research_fixture_dates_are_reproduced_on_its_creation_day():
    rows = sandbox_cmd.custom_user_config(dt.date(2026, 10, 28))["override_accounts"][0]
    dates = [(r["date_transacted"], r["date_posted"]) for r in rows["transactions"]]
    assert dates[0] == ("2026-09-12", "2026-09-13") and dates[-1] == ("2026-10-27", "2026-10-31")


def test_sandbox_link_creates_an_item_and_syncs_its_history(db, make_rt, out, fake_plaid):
    rt = make_rt()
    assert sandbox_cmd.link_command(args(user="custom", institution="ins_109508"), rt) == 0
    (call,) = fake_plaid.calls_to("sandbox_create_public_token")
    assert call["override_username"] == "user_custom"
    assert call["initial_products"] == ("transactions", "liabilities")
    (item,) = db.scalars(select(PlaidItem)).all()
    assert item.institution_name == "First Platypus Bank"
    assert out[0] == (
        f"Created a Sandbox Item for custom at First Platypus Bank: Item {item.id} with 1 account."
    )
    assert out[-1] == "First Platypus Bank: no changes. Plaid has now sent the full history."
    assert [r.trigger for r in runs(db)] == ["link"]


def test_sandbox_link_elsewhere_asks_only_for_transactions(make_rt, fake_plaid):
    assert (
        sandbox_cmd.link_command(args(user="user_good", institution="ins_127287"), make_rt()) == 0
    )
    (call,) = fake_plaid.calls_to("sandbox_create_public_token")
    assert call["initial_products"] == ("transactions",)


def test_sandbox_link_reports_a_plaid_refusal(make_rt, out, fake_plaid):
    fake_plaid.queue_error(
        "sandbox_create_public_token", fake_plaid.plaid_error("INVALID_INSTITUTION")
    )
    assert sandbox_cmd.link_command(args(user="user_good", institution="ins_x"), make_rt()) == 1
    assert out[0].startswith("error: Plaid refused (INVALID_INSTITUTION)")


def test_sandbox_refresh_refreshes_then_syncs_four_times(
    db, make_rt, out, fake_plaid, secrets, settings
):
    fake_item, item = link(db, fake_plaid, secrets, settings)
    rt = make_rt()
    assert sandbox_cmd.refresh_command(args(), rt) == 0
    assert fake_item.refresh_count == 1
    assert rt.slept == list(sync.FOLLOW_UP_DELAYS)
    assert [r.trigger for r in runs(db)] == ["manual"] * 4
    assert out[0].startswith("American Express: asking Plaid to refresh")


def test_a_rate_limited_refresh_still_syncs(db, make_rt, out, fake_plaid, secrets, settings):
    link(db, fake_plaid, secrets, settings)
    fake_plaid.queue_error("transactions_refresh", fake_plaid.plaid_error("TRANSACTIONS_LIMIT"))
    assert sandbox_cmd.refresh_command(args(), make_rt()) == 0
    assert "Plaid refused the refresh (TRANSACTIONS_LIMIT); synced anyway." in out.text


def test_sandbox_refresh_needs_an_item(make_rt, out):
    assert sandbox_cmd.refresh_command(args(), make_rt()) == 1
    assert "./run sandbox-link" in out.text


def test_sandbox_reset_login_leaves_the_item_needing_reconnect(
    db, make_rt, out, fake_plaid, secrets, settings
):
    fake_item, item = link(db, fake_plaid, secrets, settings)
    assert sandbox_cmd.reset_login_command(args(item=item.id), make_rt()) == 0
    assert fake_plaid.calls_to("sandbox_reset_login") == [{"access_token": fake_item.access_token}]
    db.expire_all()
    assert db.get(PlaidItem, item.id).status == "needs_reconnect"
    assert "click Reconnect" in out.text
    assert sandbox_cmd.reset_login_command(args(item=999999), make_rt()) == 2


def test_sandbox_reset_login_without_a_token(db, make_rt, out, fake_plaid, secrets):
    make_item(db, plaid_item_id="tokenless")
    assert sandbox_cmd.reset_login_command(args(), make_rt()) == 1
    assert "No access token for Item tokenless" in out.text
    assert fake_plaid.calls_to("sandbox_reset_login") == []


def test_the_cli_runs_the_sandbox_commands(db, monkeypatch, make_rt, fake_plaid, capsys):
    monkeypatch.setattr(sandbox_cmd, "Runtime", lambda s: make_rt(s, out=print))
    assert cli.main(["sandbox-link", "--user", "user_good"]) == 0
    assert "Created a Sandbox Item for user_good" in capsys.readouterr().out
    assert cli.main(["sandbox-refresh"]) == 0
    assert cli.main(["sandbox-reset-login"]) == 0
    assert db.scalars(select(Transaction)).all() == []
