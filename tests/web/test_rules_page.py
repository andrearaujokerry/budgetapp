"""Rules page (CAT-3) and "Re-apply rules to past transactions" (CAT-4): pages, HTMX partials,
forms and their edge cases. Services are tested in tests/test_rules.py."""

from __future__ import annotations

import datetime as dt
import html
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from budgetapp.models import Rule
from tests.factories import (
    card_payments,
    get_category,
    make_account,
    make_rule,
    make_transaction,
    uncategorized,
)
from tests.web.conftest import HTMX
from tests.web.csp import csp_problems


def toasts(text: str) -> list[str]:
    return [html.unescape(t) for t in re.findall(r'<div class="toast__text">([^<]*)</div>', text)]


def state(db, txn) -> tuple[int, str, int | None]:
    db.refresh(txn)
    return txn.category_id, txn.category_source, txn.rule_id


@pytest.fixture
def world(db):
    """Three rules (the last one off) and past rows an UBER rule would touch."""
    account = make_account(db, name="Blue Cash Preferred", mask="3005")
    dining = get_category(db, "Dining & Drinks")
    transportation = get_category(db, "Transportation")
    travel = get_category(db, "Travel")
    food = make_rule(db, dining, merchant_pattern="EATS", name="Food delivery", priority=10)
    rides = make_rule(db, transportation, merchant_pattern="LYFT", priority=20)
    flights = make_rule(
        db, travel, description_pattern="DELTA", amount_min="100", priority=30, is_active=False
    )
    return SimpleNamespace(
        account=account,
        dining=dining,
        transportation=transportation,
        travel=travel,
        food=food,
        rides=rides,
        flights=flights,
        ride=make_transaction(db, account, name="UBER *TRIP", merchant_name="Uber", amount="23.18"),
        eats=make_transaction(
            db, account, name="UBER *EATS", merchant_name="Uber Eats", amount="31.00"
        ),
        mine=make_transaction(
            db, account, name="UBER *TRIP", amount="12.00", category=dining, category_source="user"
        ),
        payment=make_transaction(
            db, account, name="UBER PAYMENT RECEIVED - THANK YOU", amount="-50.00"
        ),
    )


def uber_form(world, **extra) -> dict[str, str]:
    values = {
        "merchant_pattern": "uber",
        "match_type": "contains",
        "category_id": str(world.transportation.id),
        "is_active": "1",
        "apply_to_existing": "1",
    }
    values.update(extra)
    return values


# --------------------------------------------------------------------------- the list


class TestList:
    def test_rules_in_run_order_with_their_conditions(self, client, world):
        response = client.get("/rules")
        assert response.status_code == 200
        assert response.template.name == "rules/index.html"
        rows = response.context["rule_rows"]
        assert [r.id for r in rows] == [world.food.id, world.rides.id, world.flights.id]
        assert [r.position for r in rows] == [1, 2, 3]
        text = html.unescape(response.text)
        assert "Food delivery" in text and "Merchant contains EATS" in text
        assert "Description contains DELTA · amount ≥ $100" in text
        assert "Card payments always go to Card Payments" in text  # step 0, ARCHITECTURE §6
        assert 'id="reapply"' in response.text and "Re-apply rules to past transactions" in text
        assert csp_problems(response.text, rendered=True) == []

    def test_the_first_and_last_rule_cannot_move_further(self, client, world):
        page = client.get("/rules").text
        first = re.search(rf'<tr id="rule-row-{world.food.id}".*?</tr>', page, re.S).group(0)
        last = re.search(rf'<tr id="rule-row-{world.flights.id}".*?</tr>', page, re.S).group(0)
        assert re.search(r'aria-label="Move up: Food delivery"[^>]*disabled', first)
        assert not re.search(r'aria-label="Move down: Food delivery"[^>]*disabled', first)
        assert re.search(r'aria-label="Move down: [^"]*"[^>]*disabled', last)
        assert 'class="is-muted"' in last  # the rule that is off

    def test_empty_database(self, client):
        response = client.get("/rules")
        assert response.status_code == 200
        assert "No rules yet" in response.text
        assert response.context["rule_rows"] == []
        assert csp_problems(response.text, rendered=True) == []

    def test_htmx_gets_only_the_list(self, client, world):
        response = client.get("/rules", headers=HTMX)
        assert response.template.name == "rules/_list.html"
        assert "<html" not in response.text and 'id="rule-row-' in response.text


class TestSwitch:
    def test_htmx_turns_a_rule_off_and_on(self, client, db, world):
        response = client.post(f"/rules/{world.rides.id}/active", headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "rules/_row.html"
        assert response.text.lstrip().startswith(f'<tr id="rule-row-{world.rides.id}"')
        assert toasts(response.text) == ["Rule 2 is off: “Merchant contains LYFT”."]
        db.refresh(world.rides)
        assert world.rides.is_active is False
        response = client.post(
            f"/rules/{world.rides.id}/active", data={"is_active": "1"}, headers=HTMX
        )
        assert toasts(response.text) == ["Rule 2 is on: “Merchant contains LYFT”."]
        db.refresh(world.rides)
        assert world.rides.is_active is True
        assert csp_problems(response.text, rendered=True) == []

    def test_plain_post_redirects_with_the_toast(self, client, db, world):
        response = client.post(f"/rules/{world.food.id}/active", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/rules"
        assert toasts(client.get("/rules").text) == ["Rule 1 is off: “Food delivery”."]


class TestMove:
    def test_htmx_moves_and_renumbers(self, client, db, world):
        response = client.post(
            f"/rules/{world.flights.id}/move", data={"direction": "up"}, headers=HTMX
        )
        assert response.status_code == 200
        assert response.template.name == "rules/_list.html"
        assert [r.id for r in response.context["rule_rows"]] == [
            world.food.id,
            world.flights.id,
            world.rides.id,
        ]
        assert toasts(response.text) == [
            "“Description contains DELTA · amount ≥ $100” now runs 2nd."
        ]
        priorities = db.scalars(select(Rule.priority).order_by(Rule.priority)).all()
        assert priorities == [10, 20, 30]

    def test_moving_past_the_end_changes_nothing(self, client, world):
        response = client.post(
            f"/rules/{world.food.id}/move", data={"direction": "up"}, headers=HTMX
        )
        assert toasts(response.text) == ["“Food delivery” already runs first."]
        response = client.post(
            f"/rules/{world.flights.id}/move", data={"direction": "down"}, headers=HTMX
        )
        assert "already runs last" in toasts(response.text)[0]

    def test_a_bad_direction_is_refused(self, client, world):
        response = client.post(f"/rules/{world.food.id}/move", data={"direction": "left"})
        assert response.status_code == 400

    def test_plain_post_redirects(self, client, world):
        response = client.post(
            f"/rules/{world.rides.id}/move", data={"direction": "up"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert toasts(client.get("/rules").text) == ["“Merchant contains LYFT” now runs first."]


class TestDelete:
    def test_htmx_deletes_and_says_what_stays(self, client, db, world):
        lyft = make_transaction(
            db,
            world.account,
            name="LYFT RIDE",
            category=world.transportation,
            category_source="rule",
            rule_id=world.rides.id,
        )
        response = client.post(f"/rules/{world.rides.id}/delete", headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "rules/_list.html"
        assert world.rides.id not in [r.id for r in response.context["rule_rows"]]
        assert toasts(response.text) == [
            "Deleted “Merchant contains LYFT”. The 1 transaction it categorized keeps its"
            " category until you re-apply rules."
        ]
        assert state(db, lyft) == (world.transportation.id, "rule", None)

    def test_deleting_the_last_rule_shows_the_empty_state(self, client, db):
        rule = make_rule(db, get_category(db, "Travel"), merchant_pattern="DELTA")
        response = client.post(f"/rules/{rule.id}/delete", headers=HTMX)
        assert "No rules yet" in response.text
        assert toasts(response.text) == ["Deleted “Merchant contains DELTA”."]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/rules/{id}/active"),
        ("post", "/rules/{id}/move"),
        ("post", "/rules/{id}/delete"),
        ("get", "/rules/{id}/edit"),
        ("post", "/rules/{id}"),
    ],
)
def test_a_rule_that_is_gone_is_a_404(client, method, path):
    response = getattr(client, method)(path.format(id=10**9), headers=HTMX)
    assert response.status_code == 404


# --------------------------------------------------------------------------- the form


class TestForm:
    def test_new_rule_form_is_blank_and_goes_last(self, client, world):
        response = client.get("/rules/new")
        assert response.status_code == 200
        assert response.template.name == "rules/form.html"
        f = response.context["f"]
        assert f["is_new"] and f["draft"].priority == "40"  # after 10, 20, 30
        assert f["draft"].is_active and f["draft"].apply_to_existing
        assert response.context["pv"] == {"state": "blank"}
        assert "Nothing to match yet" in response.text
        form = re.search(r'<form[^>]*id="rule-form"[^>]*>', response.text).group(0)
        assert 'hx-get="/rules/preview"' in form
        # htmx checks a form's constraints before each request: without novalidate the
        # required category would silence the preview until a category is chosen.
        assert " novalidate" in form
        assert csp_problems(response.text, rendered=True) == []

    def test_query_fields_prefill_the_form_and_the_preview(self, client, world):
        response = client.get(
            f"/rules/new?merchant_pattern=UBER&category_id={world.transportation.id}&bogus=1"
        )
        assert response.status_code == 200
        pv = response.context["pv"]
        assert response.context["f"]["draft"].merchant_pattern == "UBER"
        assert pv["state"] == "ready" and pv["category_chosen"] and pv["shows_changes"]
        assert pv["preview"].match_count == 4  # ride, eats, mine, payment
        text = html.unescape(response.text)
        assert "4</strong> past transactions match" in text
        assert "1 transaction you categorized yourself stays as it is" in text
        assert "1 card payment stays in Card Payments" in text
        assert "1 transaction goes to a rule that runs first" in text  # Uber Eats → Food
        assert csp_problems(response.text, rendered=True) == []

    def test_hidden_cards_are_left_out_unless_chosen(self, client, db, world):
        hidden = make_account(db, name="Old card", mask="9999", is_hidden=True)
        response = client.get("/rules/new")
        assert ("Old card ··9999" in response.text) is False
        response = client.get(f"/rules/new?account_id={hidden.id}")
        assert "Old card ··9999" in response.text

    def test_edit_form_is_filled_in(self, client, world):
        response = client.get(f"/rules/{world.flights.id}/edit")
        assert response.status_code == 200
        f = response.context["f"]
        assert not f["is_new"] and f["action"] == f"/rules/{world.flights.id}"
        d = f["draft"]
        assert (d.description_pattern, d.amount_min, d.is_active) == ("DELTA", "100.00", False)
        assert d.apply_to_existing
        assert f'name="rule_id" value="{world.flights.id}"' in response.text
        assert csp_problems(response.text, rendered=True) == []


class TestPreview:
    def test_htmx_returns_the_preview_and_a_status_line(self, client, world):
        response = client.get("/rules/preview", params=uber_form(world), headers=HTMX)
        assert response.status_code == 200
        assert response.template.name == "rules/_preview.html"
        assert "<html" not in response.text
        p = response.context["pv"]["preview"]
        # the ride (→ Transportation), the payment (→ Card Payments, step 0) and Uber Eats
        # (→ Dining: "Food delivery" runs first); the row you categorized stays.
        assert (p.match_count, p.would_change_count) == (4, 3)
        assert p.changes_first  # the rows that change lead the list
        assert [item.proposed is not None for item in p.sample] == [True, True, True, False]
        status = re.search(
            r'<div hx-swap-oob="innerHTML:#rule-preview-status">([^<]*)</div>', response.text
        )
        assert status.group(1) == "4 past transactions match. Saving changes the category of 3."
        text = html.unescape(response.text)
        assert "3 transactions change category when you save" in text
        assert "A card payment: rules never change it." in text
        assert "You set this category: it never changes." in text
        assert "“Food delivery” runs first." in text
        assert csp_problems(response.text, rendered=True) == []

    def test_counts_match_what_saving_then_changes(self, client, db, world):
        preview = client.get("/rules/preview", params=uber_form(world), headers=HTMX)
        expected = preview.context["pv"]["preview"]
        response = client.post("/rules", data=uber_form(world), follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/rules"
        assert toasts(client.get("/rules").text) == [
            f"Rule saved. {expected.would_change_count} past transactions changed category;"
            f" {expected.skipped_user_count} you categorized yourself was kept."
        ]
        rule = db.scalars(select(Rule).where(Rule.merchant_pattern == "uber")).one()
        assert state(db, world.ride) == (world.transportation.id, "rule", rule.id)
        assert state(db, world.payment)[0] == card_payments(db).id  # step 0, not the rule
        assert state(db, world.mine) == (world.dining.id, "user", None)
        assert state(db, world.eats) == (world.dining.id, "rule", world.food.id)  # runs first

    def test_without_a_category_it_lists_matches_only(self, client, world):
        params = uber_form(world, category_id="")
        response = client.get("/rules/preview", params=params, headers=HTMX)
        pv = response.context["pv"]
        assert pv["state"] == "ready" and not pv["category_chosen"] and not pv["shows_changes"]
        assert "Choose a category to see what would change." in html.unescape(response.text)
        assert "change category when you save" not in response.text

    def test_with_apply_unticked_nothing_past_changes(self, client, world):
        params = uber_form(world)
        del params["apply_to_existing"]
        response = client.get("/rules/preview", params=params, headers=HTMX)
        assert not response.context["pv"]["shows_changes"]
        assert "“Also apply to past transactions” is off" in html.unescape(response.text)

    def test_an_inactive_new_rule_changes_nothing(self, client, world):
        params = uber_form(world)
        del params["is_active"]
        response = client.get("/rules/preview", params=params, headers=HTMX)
        assert "The rule is off: saving it changes nothing." in response.text

    def test_invalid_input_explains_itself(self, client, world):
        params = uber_form(world, merchant_pattern="(unclosed", match_type="regex")
        response = client.get("/rules/preview", params=params, headers=HTMX)
        assert response.status_code == 200
        pv = response.context["pv"]
        assert pv["state"] == "invalid" and "merchant_pattern" in pv["errors"]
        assert "Fix the form to see the preview" in response.text
        assert "Not a valid regular expression" in response.text

    def test_blank_preview(self, client, world):
        response = client.get("/rules/preview", params={"category_id": "1"}, headers=HTMX)
        assert response.context["pv"] == {"state": "blank"}
        assert "Add a condition to see the past transactions it matches." in response.text

    def test_editing_previews_against_the_saved_rule(self, client, world):
        params = uber_form(world, merchant_pattern="EATS", rule_id=str(world.food.id))
        params["category_id"] = str(world.dining.id)
        response = client.get("/rules/preview", params=params, headers=HTMX)
        p = response.context["pv"]["preview"]
        assert not p.is_new and p.match_count == 1  # Uber Eats, now by this very rule
        assert p.earlier_rule_count == 0

    def test_without_htmx_it_goes_to_the_form(self, client, world):
        response = client.get(
            "/rules/preview", params={"merchant_pattern": "uber"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/rules/new?merchant_pattern=uber"
        response = client.get(
            "/rules/preview", params={"rule_id": str(world.food.id)}, follow_redirects=False
        )
        assert response.headers["location"] == f"/rules/{world.food.id}/edit"


class TestSave:
    def test_without_conditions_the_form_comes_back_with_messages(self, client, db, world):
        before = db.scalar(select(func.count()).select_from(Rule))
        response = client.post(
            "/rules", data={"name": "Nothing", "category_id": "", "is_active": "1"}
        )
        assert response.status_code == 422
        assert response.template.name == "rules/form.html"
        errors = response.context["f"]["errors"]
        assert set(errors) >= {"conditions", "category_id"}
        text = html.unescape(response.text)
        assert "Add at least one condition (merchant, description, amount or card)." in text
        assert 'value="Nothing"' in response.text  # what was typed stays
        assert db.scalar(select(func.count()).select_from(Rule)) == before
        assert csp_problems(response.text, rendered=True) == []

    def test_field_errors_are_shown_next_to_their_fields(self, client, world):
        data = uber_form(
            world,
            match_type="regex",
            merchant_pattern="[",
            amount_min="50",
            amount_max="10",
            priority="soon",
        )
        response = client.post("/rules", data=data)
        assert response.status_code == 422
        errors = response.context["f"]["errors"]
        assert set(errors) == {"merchant_pattern", "amount_max", "priority"}
        assert 'aria-invalid="true"' in response.text
        assert "The maximum must be at least the minimum." in response.text

    def test_a_new_rule_without_apply_leaves_past_rows(self, client, db, world):
        data = uber_form(world)
        del data["apply_to_existing"]
        response = client.post("/rules", data=data)
        assert response.status_code == 200
        assert toasts(response.text) == [
            "Rule saved. It categorizes new transactions; re-apply rules to update past ones."
        ]
        assert state(db, world.ride)[0] == uncategorized(db).id

    def test_a_rule_saved_off(self, client, world):
        data = uber_form(world)
        del data["is_active"]
        response = client.post("/rules", data=data)
        assert toasts(response.text) == [
            "Rule saved. It is off, so it doesn't categorize anything yet."
        ]

    def test_update_with_apply(self, client, db, world):
        data = uber_form(world, merchant_pattern="LYFT", rule_id=str(world.rides.id))
        data["category_id"] = str(world.travel.id)
        lyft = make_transaction(db, world.account, name="LYFT RIDE", amount="8.00")
        response = client.post(f"/rules/{world.rides.id}", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert toasts(client.get("/rules").text) == [
            "Rule updated. 1 past transaction changed category."
        ]
        db.refresh(world.rides)
        assert world.rides.category_id == world.travel.id
        assert state(db, lyft) == (world.travel.id, "rule", world.rides.id)

    def test_update_errors_come_back_on_the_edit_form(self, client, world):
        response = client.post(
            f"/rules/{world.rides.id}", data={"category_id": str(world.travel.id)}
        )
        assert response.status_code == 422
        assert response.context["f"]["rule_id"] == world.rides.id
        assert "conditions" in response.context["f"]["errors"]


# --------------------------------------------------------------------------- re-apply (CAT-4)


@pytest.fixture
def stale(db):
    """Rows categorized before the DELTA rule existed, a user edit, and a stale payment."""
    account = make_account(db)
    travel = get_category(db, "Travel")
    make_rule(db, travel, merchant_pattern="DELTA")
    return SimpleNamespace(
        travel=travel,
        old=make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 3, 1)),
        new=make_transaction(db, account, name="DELTA AIR", date=dt.date(2026, 10, 1)),
        mine=make_transaction(
            db,
            account,
            name="DELTA AIR",
            category=get_category(db, "Shopping"),
            category_source="user",
            date=dt.date(2026, 10, 2),
        ),
        payment=make_transaction(
            db,
            account,
            amount="-100",
            name="PAYMENT RECEIVED - THANK YOU",
            category=get_category(db, "Other"),
            category_source="plaid",
            date=dt.date(2026, 10, 3),
        ),
    )


class TestReapply:
    def test_preview_is_a_dry_run_then_apply_changes_exactly_that(self, client, db, stale):
        response = client.post(
            "/rules/reapply", data={"scope": "all", "action": "preview"}, headers=HTMX
        )
        assert response.status_code == 200
        assert response.template.name == "rules/_reapply.html"
        result = response.context["ra"]["result"]
        assert (result.changed, result.skipped_user, result.dry_run) == (3, 1, True)
        assert state(db, stale.old)[0] == uncategorized(db).id  # nothing saved
        text = html.unescape(response.text)
        assert "Preview for all past transactions" in text and "Apply to 3 transactions" in text
        assert csp_problems(response.text, rendered=True) == []

        response = client.post(
            "/rules/reapply", data={"scope": "all", "action": "apply"}, headers=HTMX
        )
        assert response.status_code == 200 and response.headers["HX-Redirect"] == "/rules"
        assert toasts(client.get("/rules").text) == [
            "Re-applied rules to all past transactions: 3 transactions changed category;"
            " 1 you categorized yourself was kept."
        ]
        assert state(db, stale.old)[0] == stale.travel.id
        assert state(db, stale.payment)[0] == card_payments(db).id
        assert state(db, stale.mine)[1] == "user"

    def test_since_a_date(self, client, db, stale):
        data = {"scope": "since", "since": "2026-09-01", "action": "preview"}
        response = client.post("/rules/reapply", data=data, headers=HTMX)
        result = response.context["ra"]["result"]
        assert result.changed == 2  # the October flight and the payment
        assert 'name="since" value="2026-09-01"' in response.text
        assert "Preview for transactions since Sep 1, 2026" in response.text
        response = client.post("/rules/reapply", data={**data, "action": "apply"})
        assert toasts(response.text)[0].startswith(
            "Re-applied rules to transactions since Sep 1, 2026: 2 transactions changed category"
        )
        assert state(db, stale.old)[0] == uncategorized(db).id  # March: out of scope
        assert state(db, stale.new)[0] == stale.travel.id

    def test_only_uncategorized(self, client, db, stale):
        data = {"scope": "uncategorized", "action": "apply"}
        response = client.post("/rules/reapply", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert state(db, stale.payment)[0] == get_category(db, "Other").id  # not Uncategorized
        assert state(db, stale.new)[0] == stale.travel.id

    def test_a_since_scope_needs_a_date(self, client, stale):
        data = {"scope": "since", "since": "", "action": "preview"}
        response = client.post("/rules/reapply", data=data, headers=HTMX)
        assert response.status_code == 422
        assert response.template.name == "rules/_reapply.html"
        assert response.context["ra"]["errors"] == {"since": "Enter the first date to update."}
        response = client.post("/rules/reapply", data={**data, "action": "apply"})
        assert response.status_code == 422
        assert response.template.name == "rules/index.html"

    def test_an_unknown_scope_is_an_error_not_everything(self, client, db, stale):
        response = client.post("/rules/reapply", data={"scope": "everything", "action": "apply"})
        assert response.status_code == 422
        assert state(db, stale.old)[0] == uncategorized(db).id

    def test_nothing_to_change(self, client, db):
        response = client.post("/rules/reapply", data={"scope": "all"}, headers=HTMX)
        assert response.context["ra"]["result"].changed == 0
        assert "Nothing would change" in response.text
        assert 'value="apply"' not in response.text

    def test_plain_preview_renders_the_whole_page(self, client, stale):
        response = client.post("/rules/reapply", data={"scope": "all", "action": "preview"})
        assert response.status_code == 200
        assert response.template.name == "rules/index.html"
        assert response.context["ra"]["result"].changed == 3
        assert csp_problems(response.text, rendered=True) == []


# --------------------------------------------------------------------------- review fixes


def reapply_status(text: str) -> str:
    found = re.search(r'<div hx-swap-oob="innerHTML:#reapply-status">([^<]*)</div>', text)
    return html.unescape(found.group(1))


class TestKeyboardFocus:
    """htmx puts focus back on an element with the same id after a swap; when a move or a
    delete takes that element away (or disables it), the list names where focus goes."""

    def test_every_rule_control_has_a_stable_id(self, client, world):
        page = client.get("/rules").text
        for suffix in ("up", "down", "edit", "delete"):
            assert f'id="rule-{world.food.id}-{suffix}"' in page
        assert f'id="rule-on-{world.food.id}"' in page
        assert "autofocus" not in page

    def test_a_move_keeps_focus_on_its_button_unless_it_is_now_disabled(self, client, world):
        # Rides is 2nd: moving it up makes it first, so its Up button is disabled: Down instead.
        response = client.post(
            f"/rules/{world.rides.id}/move", data={"direction": "up"}, headers=HTMX
        )
        assert response.context["focus_id"] == f"rule-{world.rides.id}-down"
        down = re.search(rf'<button[^>]*id="rule-{world.rides.id}-down"[^>]*>', response.text)
        assert "autofocus" in down.group(0) and response.text.count("autofocus") == 1
        # Flights is 3rd: moving it up leaves it 2nd, so focus stays on its Up button.
        response = client.post(
            f"/rules/{world.flights.id}/move", data={"direction": "up"}, headers=HTMX
        )
        assert response.context["focus_id"] == f"rule-{world.flights.id}-up"
        # Food is now last: moving it down changes nothing, and Up gets focus.
        response = client.post(
            f"/rules/{world.food.id}/move", data={"direction": "down"}, headers=HTMX
        )
        assert response.context["focus_id"] == f"rule-{world.food.id}-up"

    def test_a_lone_rule_keeps_focus_on_edit(self, client, db):
        rule = make_rule(db, get_category(db, "Travel"), merchant_pattern="DELTA")
        response = client.post(f"/rules/{rule.id}/move", data={"direction": "up"}, headers=HTMX)
        assert response.context["focus_id"] == f"rule-{rule.id}-edit"

    def test_after_a_delete_focus_goes_to_the_rule_in_its_place(self, client, world):
        response = client.post(f"/rules/{world.food.id}/delete", headers=HTMX)
        assert response.context["focus_id"] == f"rule-{world.rides.id}-edit"
        response = client.post(f"/rules/{world.flights.id}/delete", headers=HTMX)  # the last one
        assert response.context["focus_id"] == f"rule-{world.rides.id}-edit"
        response = client.post(f"/rules/{world.rides.id}/delete", headers=HTMX)
        assert response.context["focus_id"] == "rules-empty-new"
        assert re.search(r'<a[^>]*id="rules-empty-new"[^>]*autofocus', response.text)

    def test_the_reapply_preview_is_announced_and_keeps_focus(self, client, stale):
        page = client.get("/rules").text
        assert '<p class="sr-only" id="reapply-status" role="status"></p>' in page
        data = {"scope": "all", "action": "preview"}
        response = client.post("/rules/reapply", data=data, headers=HTMX)
        assert 'id="reapply-preview"' in response.text  # the Preview button keeps focus
        assert reapply_status(response.text) == (
            "Preview for all past transactions: 3 transactions would change category; 3 checked;"
            " 1 you categorized yourself is kept. Nothing has changed yet."
        )
        response = client.post("/rules/reapply", data={**data, "scope": "since"}, headers=HTMX)
        assert response.status_code == 422
        assert reapply_status(response.text) == "Enter the first date to update."

    def test_nothing_to_change_is_announced_too(self, client, db):
        response = client.post("/rules/reapply", data={"scope": "all"}, headers=HTMX)
        assert reapply_status(response.text) == (
            "Preview for all past transactions: nothing would change; 0 checked."
        )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/rules/99999999999999999999/edit"),
        ("post", "/rules/99999999999999999999/active"),
        ("post", "/rules/99999999999999999999/move"),
        ("post", "/rules/99999999999999999999/delete"),
        ("post", "/rules/99999999999999999999"),
    ],
)
def test_ids_beyond_bigint_are_missing_not_server_errors(client, world, method, path):
    assert getattr(client, method)(path, headers=HTMX).status_code == 404


@pytest.mark.parametrize("raw", ["99999999999999999999999", "\u00b2", "-1", "1e3", ""])
def test_a_preview_for_a_rule_that_cannot_exist_previews_a_new_rule(client, world, raw):
    params = {
        "rule_id": raw,
        "merchant_pattern": "uber",
        "category_id": str(world.transportation.id),
    }
    response = client.get("/rules/preview", params=params, headers=HTMX)
    assert response.status_code == 200
    assert response.context["pv"]["preview"].is_new
