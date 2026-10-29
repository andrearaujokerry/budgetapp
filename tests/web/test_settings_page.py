"""Settings page (/settings): persistence, validation, the test notification and the read-only
facts about this app (BUD-2, ALR-1..3, SYNC-6, REL-3; never a secret)."""

from __future__ import annotations

import datetime as dt
import html as html_lib
import re
from decimal import Decimal

import pytest

from budgetapp import secrets as app_secrets
from budgetapp.config import load_settings
from budgetapp.models import Account, get_app_settings
from budgetapp.services import agents, app_settings
from tests.factories import make_account, make_item, make_statement, set_app_settings
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems

PLAID_SECRET = "plaid-sandbox-secret-0123456789abcdef"


def toasts(html: str) -> list[str]:
    found = re.findall(r'<div class="toast__text">([^<]*)</div>', html)
    return [html_lib.unescape(text) for text in found]


def form(**changes: str | None) -> dict[str, str]:
    """A valid settings form (the defaults); ``name=None`` leaves a field out (an unticked box)."""
    data = {
        "budget_period_mode": "calendar",
        "budget_cycle_account_id": "",
        "include_pending_in_budgets": "1",
        "budget_alerts_enabled": "1",
        "alert_thresholds": "80, 100",
        "sync_alerts_enabled": "1",
        "large_charge_threshold": "",
        "consent_reminder_days": "30",
        "statement_settle_days": "3",
    }
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def stored(db):
    db.expire_all()
    return get_app_settings(db)


def field_error(html: str, field_id: str) -> str | None:
    """The error text shown under a field (``forms._messages``)."""
    pattern = rf'id="{re.escape(field_id)}-error"><svg[^>]*>.*?</svg><span>([^<]*)</span>'
    match = re.search(pattern, html, re.S)
    return html_lib.unescape(match.group(1)) if match else None


def settings_form(html: str) -> str:
    start = html.index('<form id="settings-form"')
    return html[start : html.index("</form>", start)]


@pytest.fixture
def card(db):
    """A linked Blue Cash ··3005 (closing day unknown)."""
    return make_account(db, make_item(db), name="Blue Cash Preferred", mask="3005")


# =========================================================================== the page


class TestPage:
    def test_defaults_on_an_empty_database(self, client):
        response = client.get("/settings")
        assert response.status_code == 200 and response.template.name == "settings/index.html"
        html = response.text
        assert csp_problems(html, rendered=True) == []
        assert response.context["values"] == {
            "budget_period_mode": "calendar",
            "budget_cycle_account_id": "",
            "include_pending_in_budgets": True,
            "budget_alerts_enabled": True,
            "alert_thresholds": "80, 100",
            "sync_alerts_enabled": True,
            "large_charge_threshold": "",
            "consent_reminder_days": "30",
            "statement_settle_days": "3",
        }
        assert 'name="budget_period_mode" value="calendar" checked' in html
        assert "No cards yet. Link your Amex" in html
        assert "AI assistant" in html and "Not available in this version" in html
        assert '<link rel="stylesheet" href="/static/css/pages/settings.css">' in html
        assert client.get("/static/css/pages/settings.css").status_code == 200

    def test_every_control_has_a_label(self, client, card):
        html = settings_form(client.get("/settings").text)
        ids = re.findall(r'<(?:input|select|textarea)[^>]*\sid="([^"]+)"', html)
        assert len(ids) >= 9
        for control in ids:
            assert f'for="{control}"' in html, control

    def test_cards_and_their_closing_days(self, client, db, card):
        manual = make_account(db, manual=True, name="Gold Card", mask="1009")
        manual.statement_closing_day = 12
        make_statement(db, card, "2026-10-19")
        make_statement(db, card, "2026-09-19")
        hidden = make_account(db, make_item(db), name="Old card", mask="0001", is_hidden=True)
        db.flush()
        response = client.get("/settings")
        view = response.context["view"]
        assert [(c.label, c.closing_day, c.statements, c.linked) for c in view.cards] == [
            ("Blue Cash Preferred ··3005", None, 2, True),
            ("Gold Card ··1009", 12, 0, False),
        ]
        html = response.text
        assert "Linked through Plaid · 2 statement dates stored" in html
        assert "From CSV imports · no statement dates yet" in html
        assert f'name="closing_day_{manual.id}" type="number" value="12"' in html
        assert f"closing_day_{hidden.id}" not in html

    def test_a_hidden_card_stays_listed_while_it_sets_the_cycle(self, client, db, card):
        hidden = make_account(db, make_item(db), name="Old card", mask="0001", is_hidden=True)
        set_app_settings(db, budget_period_mode="statement", budget_cycle_account_id=hidden.id)
        labels = [c.label for c in client.get("/settings").context["view"].cards]
        assert labels == ["Blue Cash Preferred ··3005", "Old card ··0001"]


class TestAbout:
    def test_environment_facts_without_secrets(self, client, secrets, settings):
        secrets.set(app_secrets.PLAID_CLIENT_ID, "client-id-0123456789")
        secrets.set(app_secrets.plaid_secret_key("sandbox"), PLAID_SECRET)
        response = client.get("/settings")
        about = response.context["about"]
        assert (about.env, about.database, about.role) == (
            "test",
            settings.db_name,
            "budgetapp_test",
        )
        assert about.plaid_keys == "saved"
        html = response.text
        assert "Sandbox · keys saved in Keychain" in html
        assert PLAID_SECRET not in html and "client-id-0123456789" not in html
        assert f"<code>{settings.db_name}</code> as <code>budgetapp_test</code>" in html
        assert "PostgreSQL on 127.0.0.1:5434" in html
        assert "<code>./run doctor</code>" in html

    def test_missing_keys_and_keychain_approval(self, client, secrets):
        assert "keys not saved" in client.get("/settings").text
        secrets.fail_on[("get", None)] = app_secrets.KeychainApprovalNeeded("plaid.client_id")
        html = client.get("/settings").text
        assert "Keychain approval needed" in html
        assert "<code>./run doctor --env production</code>" in html

    def test_the_newest_backup(self, client, settings, freeze_today):
        freeze_today(now=dt.datetime(2026, 10, 29, 15, 0, tzinfo=dt.UTC))
        assert "No backup yet: <code>./run backup</code>" in client.get("/settings").text
        folder = settings.backups_dir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{settings.db_name}-20261029T120000Z.dump").write_bytes(b"PGDMP")
        try:
            html = client.get("/settings").text
            assert "Newest Oct 29, 2026 (3 h ago)" in html
        finally:
            (folder / f"{settings.db_name}-20261029T120000Z.dump").unlink()

    def test_production_shows_live_and_the_install(self, make_app, make_client, tmp_path):
        prod = load_settings(
            "production",
            data_dir=tmp_path / "App Support" / "budgetapp",
            logs_dir=tmp_path / "Logs",
        )
        agents.write_stamp(
            agents.default_layout(prod), "abcdef0123456789" * 2 + "abcdefgh", "3.14.2"
        )
        page = make_client(make_app(prod)).get("/settings")
        assert page.context["about"].env == "production"
        html = page.text
        assert '<span class="badge badge--ok">Live</span>' in html
        assert "install at commit <code>abcdef012345</code>" in html
        assert "<code>./run doctor --env production</code>" in html
        assert "(the backup agent runs nightly)" in html

    def test_demo(self, make_app, make_client):
        app = make_app()
        app.state.demo_name = "ops"
        page = make_client(app).get("/settings")
        assert page.context["about"].plaid_keys == "demo"
        assert "Not used with demo data" in page.text
        assert "Demo data isn't backed up" in page.text
        assert '<span class="badge badge--warn">Demo data</span>' in page.text


# =========================================================================== saving


class TestSave:
    def test_a_plain_post_saves_and_redirects_with_a_toast(self, client, db, card):
        data = form(
            budget_period_mode="statement",
            budget_cycle_account_id=str(card.id),
            include_pending_in_budgets=None,
            alert_thresholds="100, 75%  90",
            sync_alerts_enabled=None,
            large_charge_threshold="1,250",
            consent_reminder_days="45",
            statement_settle_days="0",
            **{f"closing_day_{card.id}": "19"},
        )
        response = client.post("/settings", data=data, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/settings"
        row = stored(db)
        assert row.budget_period_mode == "statement"
        assert row.budget_cycle_account_id == card.id
        assert row.include_pending_in_budgets is False
        assert row.budget_alerts_enabled is True
        assert row.alert_thresholds == [75, 90, 100]
        assert row.sync_alerts_enabled is False
        assert row.large_charge_threshold == Decimal("1250.00")
        assert (row.consent_reminder_days, row.statement_settle_days) == (45, 0)
        assert db.get(Account, card.id).statement_closing_day == 19
        page = client.get("/settings")
        assert toasts(page.text) == ["Settings saved"]
        assert page.context["values"]["alert_thresholds"] == "75, 90, 100"
        assert page.context["values"]["large_charge_threshold"] == "1250.00"

    def test_htmx_swaps_the_form_with_a_toast(self, client, db):
        response = client.post("/settings", data=form(statement_settle_days="5"), headers=HTMX)
        assert response.status_code == 200 and response.template.name == "settings/_form.html"
        assert '<form id="settings-form"' in response.text and "<html" not in response.text
        assert toasts(response.text) == ["Settings saved"]
        assert csp_problems(response.text, rendered=True) == []
        assert stored(db).statement_settle_days == 5

    def test_unticked_boxes_and_blank_large_charge_turn_things_off(self, client, db):
        set_app_settings(db, large_charge_threshold=Decimal("500"))
        data = form(budget_alerts_enabled=None, alert_thresholds="", sync_alerts_enabled=None)
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200
        row = stored(db)
        assert row.budget_alerts_enabled is False and row.sync_alerts_enabled is False
        assert row.alert_thresholds == [80, 100]  # kept for when they are turned on again
        assert row.large_charge_threshold is None

    def test_statement_mode_with_stored_statement_dates_needs_no_closing_day(
        self, client, db, card
    ):
        make_statement(db, card, "2026-10-19")
        data = form(budget_period_mode="statement", budget_cycle_account_id=str(card.id))
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200
        assert stored(db).budget_period_mode == "statement"

    def test_a_cleared_closing_day_is_saved_as_unknown(self, client, db, card):
        card.statement_closing_day = 19
        db.flush()
        data = form(**{f"closing_day_{card.id}": ""})
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200
        db.expire_all()
        assert db.get(Account, card.id).statement_closing_day is None

    def test_a_card_missing_from_the_form_keeps_its_closing_day(self, client, db, card):
        card.statement_closing_day = 19
        db.flush()
        data = form(budget_period_mode="statement", budget_cycle_account_id=str(card.id))
        assert f"closing_day_{card.id}" not in data
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200
        db.expire_all()
        assert db.get(Account, card.id).statement_closing_day == 19
        assert stored(db).budget_period_mode == "statement"
        data["statement_settle_days"] = "99"  # after an error the kept day is shown again
        values = client.post("/settings", data=data).context["values"]
        assert values[f"closing_day_{card.id}"] == "19"

    def test_calendar_mode_keeps_the_chosen_card(self, client, db, card):
        data = form(budget_cycle_account_id=str(card.id), **{f"closing_day_{card.id}": "3"})
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200
        row = stored(db)
        assert (row.budget_period_mode, row.budget_cycle_account_id) == ("calendar", card.id)


# =========================================================================== validation


INVALID = [
    ("alert_thresholds", "80, abc", "Use whole percentages separated by commas, e.g. 80, 100."),
    ("alert_thresholds", "0, 100", "Each percentage must be from 1 to 200, e.g. 80, 100."),
    ("alert_thresholds", "250", "Each percentage must be from 1 to 200, e.g. 80, 100."),
    ("alert_thresholds", "10 20 30 40 50 60", "Use at most 5 percentages."),
    ("alert_thresholds", " ", "Enter at least one percentage, e.g. 80, 100."),
    ("large_charge_threshold", "-5", "Enter an amount above $0, or leave it blank"),
    ("large_charge_threshold", "0", "Enter an amount above $0, or leave it blank"),
    ("large_charge_threshold", "lots", None),
    ("large_charge_threshold", "99999999999999", "Amounts must be under"),
    ("consent_reminder_days", "0", "Enter a number of days from 1 to 120."),
    ("consent_reminder_days", "121", "Enter a number of days from 1 to 120."),
    ("consent_reminder_days", "", "Enter a number of days from 1 to 120."),
    ("consent_reminder_days", "2.5", "Enter a number of days from 1 to 120."),
    ("statement_settle_days", "15", "Enter a number of days from 0 to 14."),
    ("statement_settle_days", "-1", "Enter a number of days from 0 to 14."),
    ("budget_cycle_account_id", "999999", "Choose a card from the list."),
    ("budget_cycle_account_id", "abc", "Choose a card from the list."),
]


class TestValidation:
    @pytest.mark.parametrize(("name", "value", "message"), INVALID)
    def test_each_field_reports_its_problem_and_nothing_is_saved(
        self, client, db, name, value, message
    ):
        before = (stored(db).consent_reminder_days, stored(db).alert_thresholds)
        data = form(consent_reminder_days="45")
        data[name] = value
        response = client.post("/settings", data=data)
        assert response.status_code == 422 and response.template.name == "settings/index.html"
        errors = response.context["errors"]
        assert list(errors) == [name]
        if message:
            assert errors[name].startswith(message)
        html = response.text
        assert f'id="f-{name}"' in html and 'aria-invalid="true"' in html
        assert field_error(html, f"f-{name}") == errors[name]
        assert "Check these settings." in html and "Fix the 1 setting marked below" in html
        assert response.context["values"][name] == value  # kept as typed
        assert (stored(db).consent_reminder_days, stored(db).alert_thresholds) == before

    def test_closing_days(self, client, db, card):
        for value in ("0", "32", "x", "1.5"):
            data = form(**{f"closing_day_{card.id}": value})
            response = client.post("/settings", data=data, headers=HTMX)
            assert response.status_code == 422, value
            assert response.template.name == "settings/_form.html"
            assert response.context["errors"] == {
                f"closing_day_{card.id}": "Enter a day from 1 to 31, or leave it blank."
            }
        assert db.get(Account, card.id).statement_closing_day is None

    def test_statement_mode_needs_a_card(self, client, db, card):
        response = client.post("/settings", data=form(budget_period_mode="statement"))
        assert response.status_code == 422
        assert response.context["errors"] == {
            "budget_cycle_account_id": "Choose the card whose statement cycle sets your budget "
            "periods."
        }
        assert stored(db).budget_period_mode == "calendar"

    def test_statement_mode_without_any_card(self, client):
        response = client.post("/settings", data=form(budget_period_mode="statement"))
        assert response.context["errors"]["budget_cycle_account_id"].startswith(
            "Statement cycles need a card: link your Amex or import a CSV first."
        )

    def test_statement_mode_needs_the_cards_closing_day(self, client, db, card):
        data = form(budget_period_mode="statement", budget_cycle_account_id=str(card.id))
        response = client.post("/settings", data=data)
        assert response.status_code == 422
        message = response.context["errors"][f"closing_day_{card.id}"]
        assert message.startswith("Enter this card's statement closing day (1–31)")
        assert field_error(response.text, f"f-closing_day_{card.id}") == message
        data[f"closing_day_{card.id}"] = "19"
        assert client.post("/settings", data=data, headers=HTMX).status_code == 200

    def test_an_unknown_period_mode(self, client):
        response = client.post("/settings", data=form(budget_period_mode="weekly"))
        assert response.status_code == 422
        assert "Choose Calendar month or Statement cycle." in response.text

    def test_several_problems_at_once_keep_every_value(self, client):
        data = form(alert_thresholds="x", statement_settle_days="99", large_charge_threshold="0")
        response = client.post("/settings", data=data, headers=HTMX)
        assert response.status_code == 422
        assert set(response.context["errors"]) == {
            "alert_thresholds",
            "statement_settle_days",
            "large_charge_threshold",
        }
        assert "Fix the 3 settings marked below" in response.text
        assert 'value="x"' in response.text and 'value="99"' in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_unticked_boxes_stay_unticked_after_an_error(self, client):
        data = form(include_pending_in_budgets=None, statement_settle_days="99")
        values = client.post("/settings", data=data).context["values"]
        assert values["include_pending_in_budgets"] is False
        assert values["budget_alerts_enabled"] is True


class TestServiceRules:
    def test_thresholds(self):
        assert app_settings.parse_thresholds("100,80;80 90%") == (80, 90, 100)
        with pytest.raises(ValueError):
            app_settings.parse_thresholds("")

    def test_save_never_commits(self, db):
        view = app_settings.save(db, form(statement_settle_days="7"))
        assert view.statement_settle_days == 7
        assert db.in_transaction()


# =========================================================================== test notification


class TestNotification:
    def test_htmx_sends_and_explains(self, client, notifier):
        response = client.post("/settings/test-notification", headers=HTMX)
        assert response.status_code == 200 and response.template.name == "settings/_notify.html"
        assert [(n.title, n.subtitle) for n in notifier.sent] == [
            ("Budget Tracker", "test environment")
        ]
        assert notifier.sent[0].message.startswith("Test notification")
        assert toasts(response.text) == [
            "Test notification sent. If none appeared, follow the steps on this page."
        ]
        assert "Sent just now. Nothing on screen? Do steps 2 and 3" in response.text
        assert csp_problems(response.text, rendered=True) == []

    def test_plain_post_redirects(self, client, notifier):
        response = client.post("/settings/test-notification", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/settings"
        assert len(notifier.sent) == 1

    def test_a_failure_is_an_error_toast(self, client, notifier):
        notifier.fail_with = "osascript: execution error"
        response = client.post("/settings/test-notification", headers=HTMX)
        assert toasts(response.text) == [
            "macOS didn't accept the notification. Try ./run notify-test."
        ]
        assert "Sent just now" not in response.text

    def test_the_page_explains_the_script_editor_step(self, client):
        html = client.get("/settings").text
        assert "System Settings → Notifications → Script Editor" in html
        assert 'hx-post="/settings/test-notification"' in html

    def test_a_demo_sends_nothing(self, make_app, make_client, notifier):
        app = make_app()
        app.state.demo_name = "ops"
        response = make_client(app).post("/settings/test-notification", headers=HTMX)
        assert notifier.sent == []
        assert toasts(response.text) == ["Demo data: no notification is sent."]

    def test_cross_site_posts_are_refused(self, raw_client, notifier):
        response = raw_client.post(
            "/settings/test-notification", headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403 and notifier.sent == []
        response = raw_client.post(
            "/settings", data=form(), headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403
