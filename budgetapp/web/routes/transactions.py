"""Transactions page (TXN-1, TXN-2, TXN-3 display, TXN-4, TXN-5, one-click rules).

``GET /transactions`` renders the full page, or only ``#results`` for the HTMX filter bar,
pagination and sort links. Every row edit is an HTMX ``POST`` that answers with the re-rendered
``<tr>`` and a toast; the dialogs (details, notes and tags, split, change category, one-click
rule) are fetched into ``#txn-dialog`` and open themselves. Each dialog URL also renders as a full
page with a plain form, so every link works without JavaScript too. Plain form posts redirect
(303) to the ``next`` page they came from.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from budgetapp import clock
from budgetapp.money import parse_money_input
from budgetapp.services import alerts, categorize
from budgetapp.services import transactions as txns
from budgetapp.services.transactions import TransactionFilter
from budgetapp.web.deps import FormDep, NotifierDep, SessionDep, SessionFactoryDep
from budgetapp.web.flash import flash_oob, is_local_path, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

NAV = "transactions"
LIST_PATH = "/transactions"
FILTER_FORM_ID = "txn-filters"

#: kind → (form template, dialog title, dialog size, page title)
FORMS: dict[str, tuple[str, str, str | None]] = {
    "edit": ("transactions/_edit_form.html", "Notes and tags", None),
    "split": ("transactions/_split_form.html", "Split this charge", "lg"),
    "category": ("transactions/_category_form.html", "Change category", "sm"),
    "rule": ("transactions/_rule_form.html", "Always categorize like this", None),
}


# --------------------------------------------------------------------------- helpers


def _query(params: list[tuple[str, str]]) -> str:
    return f"{LIST_PATH}?{urlencode(params)}" if params else LIST_PATH


def _list_urls(filt: TransactionFilter, sort: str, page: int = 1) -> dict[str, Any]:
    """Canonical URLs for the list: pagination, sort headers, export, the "show" tabs."""
    sort_param = [("sort", sort)] if sort != txns.DEFAULT_SORT else []
    base = filt.to_params()
    page_param = [("page", str(page))] if page > 1 else []
    return {
        "page": _query(base + sort_param),
        "sort": _query(base),
        "canonical": _query(base + sort_param + page_param),
        "export": f"{LIST_PATH}/export.csv"
        + (f"?{urlencode(base + sort_param)}" if base + sort_param else ""),
        "show": {
            show: _query(filt.replace(show=show).to_params() + sort_param) for show in txns.SHOWS
        },
        "reset": LIST_PATH,
    }


def _list_next(value: Any, default: str) -> str:
    """``value`` when it is a page of this section (a safe local path), else ``default``."""
    if isinstance(value, str) and is_local_path(value):
        path = value.split("?", 1)[0]
        if path == LIST_PATH or path.startswith(f"{LIST_PATH}/"):
            return value
    return default


def _came_from(request: Request) -> str:
    """The list page (with its filters) an HTMX dialog was opened from (``HX-Current-URL``)."""
    current = request.headers.get("hx-current-url", "")
    if current:
        parts = urlsplit(current)
        candidate = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.path == LIST_PATH:
            return _list_next(candidate, LIST_PATH)
    return LIST_PATH


def _row_or_404(session: Session, transaction_id: int, **kwargs: Any) -> txns.TransactionRow:
    try:
        return txns.get_row(session, transaction_id, **kwargs)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None


def _row_context(session: Session) -> dict[str, Any]:
    """What ``_row.html`` needs besides the row itself."""
    return {
        "categories": list(txns.load_categories(session).values()),
        "show_members": txns.several_card_members(session),
        "today": clock.today(),
        "conn": None,
    }


def _row_response(
    request: Request,
    session: Session,
    row: txns.TransactionRow,
    message: str,
    *,
    level: str = "success",
    close_dialog: bool = False,
) -> Response:
    context = _row_context(session) | {"t": row}
    headers = {"HX-Trigger": "dialog:close"} if close_dialog else None
    return render(
        request,
        "transactions/_row.html",
        context,
        active_nav=NAV,
        headers=headers,
        oob=flash_oob(level, message),
    )


def _form_response(
    request: Request,
    kind: str,
    context: Mapping[str, Any],
    *,
    status_code: int = 200,
    next_url: str | None = None,
) -> Response:
    """A dialog (HTMX GET), the form alone (HTMX validation error: swapped in place), or a full
    page with the same form (plain requests)."""
    template, title, size = FORMS[kind]
    htmx = is_htmx(request)
    ctx = dict(context)
    ctx.setdefault("errors", {})
    ctx["form_template"] = template
    ctx["in_dialog"] = htmx
    if next_url is None:
        if htmx:
            next_url = _came_from(request)
        else:
            next_url = _list_next(request.query_params.get("next"), LIST_PATH)
    ctx["next"] = next_url
    if htmx:
        ctx["conn"] = None
        if status_code == 422:
            headers = {"HX-Retarget": f"#txn-{kind}-form", "HX-Reswap": "outerHTML"}
            return render(request, template, ctx, active_nav=NAV, status_code=422, headers=headers)
        ctx |= {"dialog_id": f"txn-{kind}-dialog", "dialog_title": title, "dialog_size": size}
        return render(request, "transactions/_dialog.html", ctx, active_nav=NAV)
    ctx["page_title"] = title
    return render(
        request, "transactions/form_page.html", ctx, active_nav=NAV, status_code=status_code
    )


def _done(
    request: Request,
    session: Session,
    row: txns.TransactionRow,
    message: str,
    next_url: str,
    *,
    close_dialog: bool = False,
) -> Response:
    """After a committed change: the new row (HTMX) or a redirect with the toast."""
    if is_htmx(request):
        return _row_response(request, session, row, message, close_dialog=close_dialog)
    return redirect(request, next_url, "success", message)


# --------------------------------------------------------------------------- the list


@router.get("/transactions")
def transactions_page(request: Request, session: SessionDep) -> Response:
    params = request.query_params
    filt = TransactionFilter.from_params(params)
    sort = txns.parse_sort(params.get("sort"))
    categories = txns.load_categories(session)
    result = txns.search(
        session, filt, page=txns.parse_page(params.get("page")), sort=sort, categories=categories
    )
    urls = _list_urls(filt, sort, result.page)
    context: dict[str, Any] = {
        "filt": filt,
        "sort": sort,
        "page": result,
        "categories": list(categories.values()),
        "category_names": {c.id: c.name for c in categories.values()},
        "show_members": txns.several_card_members(session),
        "attention": txns.attention_counts(session),
        "any_transactions": result.total_count > 0 or txns.has_transactions(session),
        "today": clock.today(),
        "urls": urls,
    }
    if is_htmx(request):
        headers = {}
        if request.headers.get("hx-trigger") == FILTER_FORM_ID:
            headers["HX-Replace-Url"] = urls["canonical"]
        context["conn"] = None
        return render(
            request, "transactions/_results.html", context, active_nav=NAV, headers=headers
        )
    context["options"] = txns.filter_options(session)
    context["uncategorized_id"] = categorize.system_category_id(session, "uncategorized")
    return render(request, "transactions/index.html", context, active_nav=NAV)


@router.get("/transactions/export.csv")
def export_transactions(request: Request, session_factory: SessionFactoryDep) -> Response:
    """The filtered rows as a CSV download (TXN-5), in the list's order, without paging."""
    params = request.query_params
    filt = TransactionFilter.from_params(params)
    sort = txns.parse_sort(params.get("sort"))
    today = clock.today()
    with session_factory() as session:
        filename = txns.export_filename(filt, txns.resolve_dates(session, filt, today), today)

    def stream() -> Iterator[str]:
        with session_factory() as export_session:
            yield from txns.export_csv(export_session, filt, sort=sort, today=today)

    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- details


@router.get("/transactions/{transaction_id:int}")
def transaction_detail(request: Request, transaction_id: int, session: SessionDep) -> Response:
    try:
        found = txns.detail(session, transaction_id)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    context: dict[str, Any] = {"d": found, "t": found.row, "today": clock.today()}
    if is_htmx(request):
        context |= {"in_dialog": True, "next": _came_from(request), "conn": None}
        return render(request, "transactions/_detail_dialog.html", context, active_nav=NAV)
    context |= {
        "in_dialog": False,
        "next": _list_next(request.query_params.get("next"), LIST_PATH),
    }
    return render(request, "transactions/detail.html", context, active_nav=NAV)


# --------------------------------------------------------------------------- category


@router.get("/transactions/{transaction_id:int}/category")
def category_form(request: Request, transaction_id: int, session: SessionDep) -> Response:
    row = _row_or_404(session, transaction_id)
    context = {
        "t": row,
        "categories": list(txns.load_categories(session).values()),
        "values": {"category_id": row.category.id},
    }
    return _form_response(request, "category", context)


@router.post("/transactions/{transaction_id:int}/category")
def save_category(
    request: Request,
    transaction_id: int,
    session: SessionDep,
    form: FormDep,
    notifier: NotifierDep,
) -> Response:
    """The inline select in a row (and the "Change category" dialog on phones)."""
    in_dialog = form.get("dialog") == "1"
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    raw = str(form.get("category_id", ""))
    try:
        row = txns.update(session, transaction_id, category_id=raw)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    except txns.TransactionEditError as err:
        session.rollback()
        message = err.errors.get("category_id", "That category can't be used.")
        current = _row_or_404(session, transaction_id)
        if in_dialog or not is_htmx(request):
            context = {
                "t": current,
                "categories": list(txns.load_categories(session).values()),
                "values": {"category_id": raw},
                "errors": err.errors,
            }
            return _form_response(request, "category", context, status_code=422, next_url=next_url)
        # The inline select: put the row back as it was and say why.
        return _row_response(request, session, current, message, level="error")
    alerts.evaluate_budgets(session, notifier)
    session.commit()
    message = f"Saved: {row.title} is in {row.category.name}."
    return _done(request, session, row, message, next_url, close_dialog=in_dialog)


@router.post("/transactions/{transaction_id:int}/reset")
def reset_category(
    request: Request,
    transaction_id: int,
    session: SessionDep,
    form: FormDep,
    notifier: NotifierDep,
) -> Response:
    try:
        row = txns.reset_to_automatic(session, transaction_id)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    alerts.evaluate_budgets(session, notifier)
    session.commit()
    message = f"Reset: {row.title} is in {row.category.name} ({row.category_source_label})."
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    return _done(request, session, row, message, next_url)


@router.post("/transactions/{transaction_id:int}/reviewed")
def mark_reviewed(
    request: Request, transaction_id: int, session: SessionDep, form: FormDep
) -> Response:
    try:
        row = txns.clear_review(session, transaction_id)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    session.commit()
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    return _done(request, session, row, "Marked as reviewed.", next_url)


# --------------------------------------------------------------------------- notes and tags


@router.get("/transactions/{transaction_id:int}/edit")
def edit_form(request: Request, transaction_id: int, session: SessionDep) -> Response:
    row = _row_or_404(session, transaction_id)
    context = {
        "t": row,
        "values": {"notes": row.notes or "", "tags": list(row.tags)},
        "suggestions": txns.all_tags(session),
    }
    return _form_response(request, "edit", context)


@router.post("/transactions/{transaction_id:int}/edit")
def save_edit(
    request: Request, transaction_id: int, session: SessionDep, form: FormDep
) -> Response:
    notes = str(form.get("notes", ""))
    tags = [str(t) for t in form.getlist("tags") if isinstance(t, str) and t.strip()]
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    try:
        row = txns.update(session, transaction_id, notes=notes, tags=tags)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    except txns.TransactionEditError as err:
        session.rollback()
        context = {
            "t": _row_or_404(session, transaction_id),
            "values": {"notes": notes, "tags": tags},
            "suggestions": txns.all_tags(session),
            "errors": err.errors,
        }
        return _form_response(request, "edit", context, status_code=422, next_url=next_url)
    session.commit()
    return _done(request, session, row, "Notes and tags saved.", next_url, close_dialog=True)


# --------------------------------------------------------------------------- splits


def _split_parts(row: txns.TransactionRow) -> list[dict[str, str]]:
    """The split form's starting rows: the current split, else the whole amount in the row's
    category plus an empty second part."""
    if row.splits:
        return [
            {"category_id": str(s.category.id), "amount": str(abs(s.amount)), "note": s.note or ""}
            for s in row.splits
        ]
    return [
        {"category_id": str(row.category.id), "amount": str(abs(row.amount)), "note": ""},
        {"category_id": "", "amount": "", "note": ""},
    ]


def _split_context(
    session: Session, row: txns.TransactionRow, parts: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    return {
        "t": row,
        "parts": parts,
        "categories": list(txns.load_categories(session).values()),
        "max_parts": txns.MAX_SPLIT_PARTS,
        "auto_first": not row.splits,
        **extra,
    }


@router.get("/transactions/{transaction_id:int}/split")
def split_form(request: Request, transaction_id: int, session: SessionDep) -> Response:
    row = _row_or_404(session, transaction_id)
    return _form_response(request, "split", _split_context(session, row, _split_parts(row)))


@router.post("/transactions/{transaction_id:int}/split")
def save_split(
    request: Request,
    transaction_id: int,
    session: SessionDep,
    form: FormDep,
    notifier: NotifierDep,
) -> Response:
    """Amounts arrive as positive numbers; a credit's parts are credits (the sign is the row's)."""
    row = _row_or_404(session, transaction_id)
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    columns = [
        [str(v) for v in form.getlist(name)]
        for name in ("part_category", "part_amount", "part_note")
    ]
    raw_parts: list[dict[str, str]] = []
    for i in range(max(len(c) for c in columns)):
        category, amount, note = (c[i] if i < len(c) else "" for c in columns)
        if category.strip() or amount.strip() or note.strip():
            raw_parts.append({"category_id": category, "amount": amount, "note": note})
    errors: dict[str, str] = {}
    sign = -1 if row.amount < 0 else 1
    parts: list[txns.SplitInput] = []
    for i, part in enumerate(raw_parts):
        value = None
        if not part["amount"].strip():
            errors[f"part-{i}-amount"] = "Enter an amount."
        else:
            try:
                value = parse_money_input(part["amount"])
            except ValueError as exc:
                errors[f"part-{i}-amount"] = str(exc)
            else:
                if value < 0:
                    errors[f"part-{i}-amount"] = "Enter the amount without a minus sign."
                    value = None
        parts.append(
            (part["category_id"], value * sign if value is not None else None, part["note"])
        )
    if not errors:
        try:
            row = txns.set_splits(session, transaction_id, parts)
        except txns.TransactionEditError as err:
            errors = err.errors
    if errors:
        session.rollback()
        current = _row_or_404(session, transaction_id)
        context = _split_context(
            session, current, raw_parts or _split_parts(current), errors=errors
        )
        context["auto_first"] = False
        return _form_response(request, "split", context, status_code=422, next_url=next_url)
    alerts.evaluate_budgets(session, notifier)
    session.commit()
    message = f"Split into {len(row.splits)} parts."
    return _done(request, session, row, message, next_url, close_dialog=True)


@router.post("/transactions/{transaction_id:int}/split/clear")
def clear_split(
    request: Request,
    transaction_id: int,
    session: SessionDep,
    form: FormDep,
    notifier: NotifierDep,
) -> Response:
    try:
        row = txns.clear_splits(session, transaction_id)
    except txns.TransactionNotFound:
        raise HTTPException(404) from None
    alerts.evaluate_budgets(session, notifier)
    session.commit()
    next_url = _list_next(form.get("next"), f"{LIST_PATH}/{transaction_id}")
    message = f"Split removed: the whole amount is in {row.category.name}."
    return _done(request, session, row, message, next_url, close_dialog=True)


# --------------------------------------------------------------------------- one-click rule


def _rule_context(
    session: Session, row: txns.TransactionRow, values: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    draft = txns.one_click_draft(session, row.id)
    preview = txns.preview_one_click_rule(
        session,
        row.id,
        merchant_pattern=str(values.get("merchant_pattern", "")),
        category_id=values.get("category_id"),
    )
    return {
        "t": row,
        "draft": draft,
        "values": values,
        "preview": preview,
        "preview_category": _category_name(session, values.get("category_id")),
        "categories": list(txns.load_categories(session).values()),
        **extra,
    }


def _category_name(session: Session, value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    ref = txns.load_categories(session).get(int(text))
    return ref.name if ref else None


@router.get("/transactions/{transaction_id:int}/rule")
def rule_form(request: Request, transaction_id: int, session: SessionDep) -> Response:
    row = _row_or_404(session, transaction_id)
    draft = txns.one_click_draft(session, transaction_id)
    values = {
        "merchant_pattern": draft.merchant_pattern,
        "category_id": draft.category_id,
        "apply_past": True,
    }
    return _form_response(request, "rule", _rule_context(session, row, values))


@router.get("/transactions/{transaction_id:int}/rule/preview")
def rule_preview(request: Request, transaction_id: int, session: SessionDep) -> Response:
    row = _row_or_404(session, transaction_id)
    params = request.query_params
    values = {
        "merchant_pattern": params.get("merchant_pattern", ""),
        "category_id": params.get("category_id", ""),
    }
    context = _rule_context(session, row, values) | {"conn": None}
    return render(request, "transactions/_rule_preview.html", context, active_nav=NAV)


@router.post("/transactions/{transaction_id:int}/rule")
def save_rule(
    request: Request,
    transaction_id: int,
    session: SessionDep,
    form: FormDep,
    notifier: NotifierDep,
) -> Response:
    """Creates the rule; the list then reloads (many rows may have moved)."""
    row = _row_or_404(session, transaction_id)
    values = {
        "merchant_pattern": str(form.get("merchant_pattern", "")),
        "category_id": str(form.get("category_id", "")),
        "apply_past": form.get("apply_past") == "1",
    }
    next_url = _list_next(form.get("next"), LIST_PATH)
    try:
        result = txns.create_one_click_rule(
            session,
            transaction_id,
            merchant_pattern=values["merchant_pattern"],
            category_id=values["category_id"],
            apply_to_past=values["apply_past"],
        )
    except categorize.RuleValidationError as err:
        session.rollback()
        context = _rule_context(session, row, values, errors=err.errors)
        return _form_response(request, "rule", context, status_code=422, next_url=next_url)
    if result.changed:
        alerts.evaluate_budgets(session, notifier)
    session.commit()
    category = txns.load_categories(session)[result.category_id].name
    pattern = " ".join(values["merchant_pattern"].split())
    message = f"Rule saved: merchants containing “{pattern}” go to {category}."
    if result.applied:
        moved = (
            "1 past transaction" if result.changed == 1 else f"{result.changed:,} past transactions"
        )
        message += f" {moved} moved"
        if result.skipped_user:
            message += f"; {result.skipped_user:,} you categorized by hand stayed as they were"
        message += "."
    return redirect(request, next_url, "success", message)
