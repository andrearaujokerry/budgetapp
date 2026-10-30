"""Rules page (CAT-3) and "Re-apply rules to past transactions" (CAT-4).

* ``GET /rules``: the rules in run order (conditions, category, transactions, on/off, move up or
  down, edit, delete) and the re-apply panel.
* ``GET /rules/new`` (fields can be prefilled from the query string, e.g.
  ``?merchant_pattern=UBER&category_id=5``) · ``POST /rules``; ``GET /rules/{id}/edit`` ·
  ``POST /rules/{id}``. The form previews matching past transactions as you type
  (``GET /rules/preview``, HTMX, debounced).
* ``POST /rules/{id}/active`` · ``/move`` · ``/delete``: HTMX gets the row or the list plus a
  toast; plain posts are redirected.
* ``POST /rules/reapply``: ``action=preview`` (a dry run: counts and examples) then
  ``action=apply``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from budgetapp.models import Account, Rule, system_category
from budgetapp.services import categories, rules
from budgetapp.services.categories import CategoryRef
from budgetapp.services.categorize import RecategorizeResult, RuleValidationError
from budgetapp.services.rules import (
    MATCH_TYPE_LABELS,
    REAPPLY_SCOPES,
    ReapplyScopeError,
    RuleDraft,
    RuleNotFound,
)
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

NAV = "rules"


def _page(request: Request, template: str, context: dict[str, Any], **kw: Any) -> Response:
    return render(request, template, context, active_nav=NAV, **kw)


def _get_rule(session: Session, rule_id: int):
    try:
        return rules.get_rule(session, rule_id)
    except RuleNotFound:
        raise HTTPException(404) from None


def _list_context(session: Session) -> dict[str, Any]:
    return {"rule_rows": rules.list_rules(session)}


def _reapply_context(
    scope: str = "all",
    since: str = "",
    *,
    result: rules.ReapplyResult | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ra": {
            "scope": scope if scope in REAPPLY_SCOPES else "all",
            "since": since,
            "result": result,
            "errors": errors or {},
            "scope_options": list(REAPPLY_SCOPES.items()),
        }
    }


# --------------------------------------------------------------------------- the list


@router.get("/rules")
def rules_page(request: Request, session: SessionDep) -> Response:
    if is_htmx(request):
        return _page(request, "rules/_list.html", _list_context(session))
    return _page(request, "rules/index.html", {**_list_context(session), **_reapply_context()})


def _list_response(
    request: Request, session: Session, message: str, *, focus_id: str | None = None
) -> Response:
    """After a move or delete: HTMX gets the list (with ``focus_id`` focused) and a toast."""
    if not is_htmx(request):
        return redirect(request, "/rules", "success", message)
    return _page(
        request,
        "rules/_list.html",
        {**_list_context(session), "focus_id": focus_id},
        oob=flash_oob("success", message),
    )


@router.post("/rules/{rule_id:int}/active")
def set_active(request: Request, rule_id: int, session: SessionDep, form: FormDep) -> Response:
    rule = _get_rule(session, rule_id)
    active = bool(form.get("is_active"))
    rules.set_active(session, rule.id, active)
    session.commit()
    rows = rules.list_rules(session)
    row = next(r for r in rows if r.id == rule.id)
    message = f"Rule {row.position} is {'on' if active else 'off'}: “{row.title}”."
    if not is_htmx(request):
        return redirect(request, "/rules", "success", message)
    context = {"r": row, "rule_total": len(rows)}
    return _page(request, "rules/_row.html", context, oob=flash_oob("success", message))


@router.post("/rules/{rule_id:int}/move")
def move(request: Request, rule_id: int, session: SessionDep, form: FormDep) -> Response:
    rule = _get_rule(session, rule_id)
    direction = form.get("direction")
    if direction not in ("up", "down"):
        raise HTTPException(400, "Choose up or down.")
    moved = rules.move_rule(session, rule.id, str(direction))
    session.commit()
    rows = rules.list_rules(session)
    row = next(r for r in rows if r.id == rule.id)
    if moved:
        message = f"“{row.title}” now runs {_ordinal(row.position)}."
    else:
        message = f"“{row.title}” already runs {'first' if direction == 'up' else 'last'}."
    focus_id = _move_focus(row, len(rows), str(direction))
    return _list_response(request, session, message, focus_id=focus_id)


def _move_focus(row: rules.RuleRow, total: int, direction: str) -> str:
    """Keep focus on the button that was pressed, unless the move disabled it (the rule is now
    first or last): then the other move button, or Edit when it is the only rule."""
    at_edge = row.position == (1 if direction == "up" else total)
    if not at_edge:
        return f"rule-{row.id}-{direction}"
    if total > 1:
        return f"rule-{row.id}-{'down' if direction == 'up' else 'up'}"
    return f"rule-{row.id}-edit"


def _ordinal(n: int) -> str:
    if n == 1:
        return "first"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@router.post("/rules/{rule_id:int}/delete")
def delete(request: Request, rule_id: int, session: SessionDep) -> Response:
    rule = _get_rule(session, rule_id)
    index = next(i for i, r in enumerate(rules.ordered_rules(session)) if r.id == rule.id)
    deleted = rules.delete_rule(session, rule.id)
    session.commit()
    message = f"Deleted “{deleted.summary}”."
    if deleted.transaction_count:
        n = deleted.transaction_count
        message += (
            f" The {n:,} transaction{'s' if n != 1 else ''} it categorized keep"
            f"{'' if n != 1 else 's'} {'their' if n != 1 else 'its'} category until you re-apply"
            " rules."
        )
    # Focus moves to the rule that took its place (or the one before it, or "New rule").
    left = rules.ordered_rules(session)
    focus_id = f"rule-{left[min(index, len(left) - 1)].id}-edit" if left else "rules-empty-new"
    return _list_response(request, session, message, focus_id=focus_id)


# --------------------------------------------------------------------------- the form


def _account_options(session: Session, selected: str) -> list[tuple[str, str]]:
    options = []
    for account in session.scalars(select(Account).order_by(Account.id)):
        if not account.is_hidden or str(account.id) == selected:
            options.append((str(account.id), account.display_name))
    return options


def _preview_state(session: Session, draft: RuleDraft, rule_id: int | None) -> dict[str, Any]:
    """What the preview panel shows: ``blank`` (nothing typed), ``invalid`` (with the messages)
    or ``ready``. Without a category yet, the matches show but not what would change; with
    "Also apply to past transactions" unticked, saving changes no past transaction."""
    if draft.is_blank:
        return {"state": "blank"}
    chosen = bool(draft.category_id)
    probe = draft
    if not chosen:
        placeholder = system_category(session, "uncategorized").id
        probe = dataclasses.replace(draft, category_id=str(placeholder))
    # Saving changes past rows only with a category, "apply" ticked, and a rule that is on or an
    # edit (a new rule that is off is saved without touching past rows: categorize.create_rule).
    shows = chosen and draft.apply_to_existing and (draft.is_active or rule_id is not None)
    try:
        preview = rules.preview(session, probe, rule_id=rule_id, changes_first=shows)
    except RuleValidationError as err:
        return {"state": "invalid", "errors": err.errors}
    category = categories.get_category(session, int(probe.category_id))  # valid: it previewed
    return {
        "state": "ready",
        "preview": preview,
        "category_chosen": chosen,
        "category": CategoryRef.of(category) if chosen else None,
        "apply": draft.apply_to_existing,
        "shows_changes": shows,
    }


def _preview_status(pv: dict[str, Any]) -> str:
    """One sentence for screen readers each time the preview changes."""
    if pv["state"] == "blank":
        return "Add a condition to see the past transactions it matches."
    if pv["state"] == "invalid":
        return "Fix the highlighted fields to see the preview."
    p = pv["preview"]
    n = p.match_count
    text = f"{n:,} past transaction{'s' if n != 1 else ''} match{'es' if n == 1 else ''}."
    if pv["shows_changes"]:
        k = p.would_change_count
        text += f" Saving changes the category of {k:,}." if k else " Saving changes none of them."
    return text


def _form_context(
    session: Session,
    draft: RuleDraft,
    *,
    rule_id: int | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    refs = [CategoryRef.of(c) for c in categories.all_categories(session)]
    return {
        "f": {
            "draft": draft,
            "rule_id": rule_id,
            "is_new": rule_id is None,
            "action": "/rules" if rule_id is None else f"/rules/{rule_id}",
            "errors": errors or {},
            "categories": refs,
            "accounts": _account_options(session, draft.account_id),
            "match_options": list(MATCH_TYPE_LABELS.items()),
        },
        "pv": _preview_state(session, draft, rule_id),
    }


def _new_draft(session: Session, query: Any) -> RuleDraft:
    """A new rule: on, applied to past transactions, last in order; query fields prefill it."""
    defaults = {
        "is_active": "1",
        "apply_to_existing": "1",
        "priority": str(rules.next_priority(session)),
        "match_type": "contains",
    }
    fields = {
        k: v for k, v in query.items() if k in {f.name for f in dataclasses.fields(RuleDraft)}
    }
    return RuleDraft.from_form({**defaults, **fields})


@router.get("/rules/new")
def new_rule(request: Request, session: SessionDep) -> Response:
    draft = _new_draft(session, request.query_params)
    return _page(request, "rules/form.html", _form_context(session, draft))


@router.get("/rules/{rule_id:int}/edit")
def edit_rule(request: Request, rule_id: int, session: SessionDep) -> Response:
    rule = _get_rule(session, rule_id)
    draft = dataclasses.replace(RuleDraft.from_rule(rule), apply_to_existing=True)
    return _page(request, "rules/form.html", _form_context(session, draft, rule_id=rule.id))


def _existing_rule_id(session: Session, raw: str) -> int | None:
    """The id of a rule that exists, from a query value (anything else is no rule)."""
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    rule_id = int(text)
    if not categories.is_valid_id(rule_id) or session.get(Rule, rule_id) is None:
        return None
    return rule_id


@router.get("/rules/preview")
def preview(request: Request, session: SessionDep) -> Response:
    query = request.query_params
    rule_id = _existing_rule_id(session, query.get("rule_id", ""))
    if not is_htmx(request):
        if rule_id is not None:
            return redirect(request, f"/rules/{rule_id}/edit")
        return redirect(request, "/rules/new" + (f"?{request.url.query}" if query else ""))
    pv = _preview_state(session, RuleDraft.from_form(query), rule_id)
    status = Markup('<div hx-swap-oob="innerHTML:#rule-preview-status">{}</div>').format(
        _preview_status(pv)
    )
    return _page(request, "rules/_preview.html", {"pv": pv}, oob=status)


def _saved_message(verb: str, rule: Any, applied: RecategorizeResult | None) -> str:
    text = f"Rule {verb}."
    if not rule.is_active:
        return f"{text} It is off, so it doesn't categorize anything yet."
    if applied is None:
        return f"{text} It categorizes new transactions; re-apply rules to update past ones."
    if applied.changed:
        n = applied.changed
        text += f" {n:,} past transaction{'s' if n != 1 else ''} changed category"
    else:
        text += " No past transactions needed a change"
    if applied.skipped_user:
        n = applied.skipped_user
        text += f"; {n:,} you categorized yourself {'were' if n != 1 else 'was'} kept"
    return text + "."


@router.post("/rules")
def create_rule(request: Request, session: SessionDep, form: FormDep) -> Response:
    draft = RuleDraft.from_form(form)
    try:
        result = rules.create(session, draft)
    except RuleValidationError as err:
        context = _form_context(session, draft, errors=err.errors)
        return _page(request, "rules/form.html", context, status_code=422)
    session.commit()
    return redirect(
        request, "/rules", "success", _saved_message("saved", result.rule, result.applied)
    )


@router.post("/rules/{rule_id:int}")
def update_rule(request: Request, rule_id: int, session: SessionDep, form: FormDep) -> Response:
    rule = _get_rule(session, rule_id)
    draft = RuleDraft.from_form(form)
    try:
        result = rules.update_rule(session, rule.id, draft)
    except RuleValidationError as err:
        context = _form_context(session, draft, rule_id=rule.id, errors=err.errors)
        return _page(request, "rules/form.html", context, status_code=422)
    session.commit()
    return redirect(
        request, "/rules", "success", _saved_message("updated", result.rule, result.applied)
    )


# --------------------------------------------------------------------------- re-apply (CAT-4)


@router.post("/rules/reapply")
def reapply(request: Request, session: SessionDep, form: FormDep) -> Response:
    scope_key = str(form.get("scope", "all"))
    since = str(form.get("since", "")).strip()
    try:
        scope = rules.parse_scope(scope_key, since)
    except ReapplyScopeError as err:
        context = _reapply_context(scope_key, since, errors=err.errors)
        if is_htmx(request):
            status = _reapply_status(" ".join(err.errors.values()))
            return _page(request, "rules/_reapply.html", context, status_code=422, oob=status)
        return _page(
            request, "rules/index.html", {**_list_context(session), **context}, status_code=422
        )
    if form.get("action") == "apply":
        result = rules.reapply(session, scope, dry_run=False)
        session.commit()
        return redirect(request, "/rules", "success", _reapplied_message(result))
    result = rules.reapply(session, scope, dry_run=True)
    session.rollback()  # a dry run changes nothing; release the ledger lock now
    context = _reapply_context(scope.key, since, result=result)
    if is_htmx(request):
        status = _reapply_status(_preview_sentence(result))
        return _page(request, "rules/_reapply.html", context, oob=status)
    return _page(request, "rules/index.html", {**_list_context(session), **context})


def _reapply_status(text: str) -> Markup:
    """The re-apply panel's result, read out by screen readers (``#reapply-status`` stays on the
    page while the panel is swapped, so the change is announced)."""
    return Markup('<div hx-swap-oob="innerHTML:#reapply-status">{}</div>').format(text)


def _preview_sentence(result: rules.ReapplyResult) -> str:
    n, k = result.changed, result.skipped_user
    text = f"Preview for {result.scope.description}: "
    if n:
        text += f"{n:,} transaction{'s' if n != 1 else ''} would change category"
    else:
        text += "nothing would change"
    text += f"; {result.examined:,} checked"
    if k:
        text += f"; {k:,} you categorized yourself {'are' if k != 1 else 'is'} kept"
    return text + ". Nothing has changed yet." if n else text + "."


def _reapplied_message(result: rules.ReapplyResult) -> str:
    n = result.changed
    text = f"Re-applied rules to {result.scope.description}: "
    text += f"{n:,} transaction{'s' if n != 1 else ''} changed category" if n else "nothing changed"
    if result.skipped_user:
        k = result.skipped_user
        text += f"; {k:,} you categorized yourself {'were' if k != 1 else 'was'} kept"
    return text + "."
