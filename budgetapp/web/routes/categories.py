"""Categories page (CAT-1) and its "Plaid & Amex categories" tab (CAT-2).

* ``GET /categories``: your categories (usage, spending this period, hide switch, edit, merge).
* ``GET /categories/mappings``: Plaid and Amex CSV categories and where each one goes
  (``?source=plaid_pfc|amex_csv&show=used|all``); ``POST /categories/mappings`` changes one row.
* ``GET /categories/new`` · ``POST /categories``; ``GET /categories/{id}/edit`` ·
  ``POST /categories/{id}``; ``POST /categories/{id}/hidden``; ``GET /categories/{id}/merge``
  (``/merge/preview``) · ``POST /categories/{id}/merge``.

HTMX gets fragments (dialogs, the table, a row, a mapping group, the tab panel); plain requests
get full pages and redirects, so everything also works as ordinary links and forms.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from budgetapp.services import categories
from budgetapp.services.categories import (
    COLOR_NAMES,
    KIND_HELP,
    KIND_LABELS,
    NEUTRAL_COLOR,
    PALETTE,
    CategoryError,
    CategoryNotFound,
    CategoryRef,
    MappingError,
)
from budgetapp.web.deps import FormDep, SessionDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

NAV = "categories"
SOURCES = ("plaid_pfc", "amex_csv")
SHOWS = ("used", "all")
KIND_OPTIONS = [
    ("expense", "Spending"),
    ("income", "Credits & income (not spending)"),
    ("transfer", "Card payments & transfers (not spending)"),
]


def _page(request: Request, template: str, context: dict[str, Any], **kw: Any) -> Response:
    return render(request, template, context, active_nav=NAV, **kw)


def _refs(session: Session) -> list[CategoryRef]:
    return [CategoryRef.of(c) for c in categories.all_categories(session)]


def _kinds() -> list[dict[str, str]]:
    return [{"key": k, "label": KIND_LABELS[k], "help": KIND_HELP[k]} for k in KIND_LABELS]


def _base_context(session: Session, tab: str) -> dict[str, Any]:
    return {
        "tab": tab,
        "category_count": len(categories.all_categories(session)),
        "kinds": _kinds(),
    }


def _table_context(session: Session) -> dict[str, Any]:
    return {"overview": categories.list_categories(session)}


def _form_context(
    session: Session,
    category: Any = None,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    in_dialog: bool = True,
) -> dict[str, Any]:
    """The add/edit form; ``values`` are what the user typed (re-rendered after an error)."""
    if values is None:
        if category is not None:
            values = {
                "name": category.name,
                "kind": category.kind,
                "color": category.color or "",
                "icon": category.icon or "",
            }
        else:
            values = {"name": "", "kind": "expense", "color": categories.suggest_color(session)}
            values["icon"] = ""
    color = (values.get("color") or "").strip().lower()
    swatches = [{"value": slot, "name": COLOR_NAMES[slot]} for slot in (*PALETTE, NEUTRAL_COLOR)]
    if color and color not in COLOR_NAMES:
        swatches.append({"value": color, "name": f"Custom colour {color}"})
    ref = CategoryRef.of(category) if category is not None else None
    return {
        "category": ref,
        "data": values,  # not "values": f.values would be the dict method in Jinja
        "errors": errors or {},
        "swatches": swatches,
        "kind_options": KIND_OPTIONS,
        "in_dialog": in_dialog,
        "form_id": f"category-form-{ref.id}" if ref else "category-form-new",
        "action": f"/categories/{ref.id}" if ref else "/categories",
    }


def _posted(form: Any) -> dict[str, str]:
    def text(name: str) -> str:
        value = form.get(name)
        return value if isinstance(value, str) else ""

    return {k: text(k) for k in ("name", "kind", "color", "icon")}


def _get_category(session: Session, category_id: int):
    try:
        return categories.get_category(session, category_id)
    except CategoryNotFound:
        raise HTTPException(404) from None


# --------------------------------------------------------------------------- the two tabs


@router.get("/categories")
def categories_page(request: Request, session: SessionDep) -> Response:
    context = {**_base_context(session, "categories"), **_table_context(session)}
    if is_htmx(request):
        return _page(request, "categories/_view.html", context)
    return _page(request, "categories/index.html", context)


def _mappings_context(session: Session, source: str, show: str) -> dict[str, Any]:
    listing = categories.list_mappings(session, source, only_used=show == "used")
    refs = _refs(session)
    return {
        "source": source,
        "show": show,
        "listing": listing,
        "categories": refs,
        "category_by_id": {c.id: c for c in refs},
    }


def _source_and_show(source: str, show: str) -> tuple[str, str]:
    return (source if source in SOURCES else SOURCES[0], show if show in SHOWS else SHOWS[0])


@router.get("/categories/mappings")
def mappings_page(
    request: Request, session: SessionDep, source: str = "plaid_pfc", show: str = "used"
) -> Response:
    source, show = _source_and_show(source, show)
    context = {**_base_context(session, "mappings"), **_mappings_context(session, source, show)}
    if is_htmx(request):
        if request.headers.get("HX-Target") == "mappings-results":
            return _page(request, "categories/_mappings_results.html", context)
        return _page(request, "categories/_view.html", context)
    return _page(request, "categories/index.html", context)


@router.post("/categories/mappings")
def save_mapping(request: Request, session: SessionDep, form: FormDep) -> Response:
    source, show = _source_and_show(str(form.get("source", "")), str(form.get("show", "")))
    key = str(form.get("key", ""))
    raw = str(form.get("category_id", "")).strip()
    try:
        category_id = int(raw) if raw else None
        change = categories.set_mapping(session, source, key, category_id)
    except (ValueError, MappingError) as exc:
        message = str(exc) if isinstance(exc, MappingError) else "Choose a category."
        if is_htmx(request):
            raise HTTPException(409, message) from None
        return redirect(request, _mappings_url(source, show), "error", message)
    session.commit()
    names = {c.id: c.name for c in _refs(session)}
    message = _mapping_message(change, names)
    if not is_htmx(request):
        return redirect(request, _mappings_url(source, show), "success", message)
    context = _mappings_context(session, source, show)
    group = next(
        (g for g in context["listing"].groups if any(r.key == change.key for r in g.rows)), None
    )
    if group is None:  # the row left the filtered list: show the whole list again
        return _page(
            request,
            "categories/_mappings_results.html",
            context,
            headers={"HX-Retarget": "#mappings-results", "HX-Reswap": "innerHTML"},
            oob=flash_oob("success", message),
        )
    return _page(
        request,
        "categories/_mapping_group.html",
        {**context, "group": group},
        oob=flash_oob("success", message),
    )


def _mappings_url(source: str, show: str) -> str:
    return "/categories/mappings?" + urlencode({"source": source, "show": show})


def _mapping_message(change: categories.MappingChange, names: dict[int, str]) -> str:
    what = change.label + (" (credits)" if change.key.endswith("|credit") else "")
    if change.new_category_id is None:
        text = f"{what} is no longer mapped: it uses its group's category."
    else:
        text = f"{what} now goes to {names.get(change.new_category_id, 'the new category')}."
    if change.transaction_count:
        count = f"{change.transaction_count:,} past transaction"
        count += "s keep" if change.transaction_count != 1 else " keeps"
        text += f" {count} its category until you re-apply rules."
    return text


# --------------------------------------------------------------------------- add and edit


def _form_error(
    request: Request, session: SessionDep, category: Any, form: Any, err: CategoryError
) -> Response:
    values = _posted(form)
    if category is not None and category.system_key is not None:
        values["kind"] = category.kind
    if is_htmx(request):
        f = _form_context(session, category, values=values, errors=err.errors)
        return _page(
            request,
            "categories/_form.html",
            {"f": f},
            status_code=422,
            headers={"HX-Retarget": f"#{f['form_id']}", "HX-Reswap": "outerHTML"},
        )
    f = _form_context(session, category, values=values, errors=err.errors, in_dialog=False)
    return _page(request, "categories/form.html", {"f": f}, status_code=422)


def _saved(request: Request, session: SessionDep, message: str) -> Response:
    if not is_htmx(request):
        return redirect(request, "/categories", "success", message)
    return _page(
        request,
        "categories/_table.html",
        _table_context(session),
        headers={"HX-Trigger": "dialog:close"},
        oob=flash_oob("success", message),
    )


@router.get("/categories/new")
def new_category(request: Request, session: SessionDep) -> Response:
    if is_htmx(request):
        return _page(request, "categories/_form_dialog.html", {"f": _form_context(session)})
    return _page(request, "categories/form.html", {"f": _form_context(session, in_dialog=False)})


@router.post("/categories")
def create_category(request: Request, session: SessionDep, form: FormDep) -> Response:
    values = _posted(form)
    try:
        category = categories.create_category(
            session, values["name"], values["kind"], values["color"] or None, values["icon"]
        )
    except CategoryError as err:
        return _form_error(request, session, None, form, err)
    session.commit()
    return _saved(request, session, f"Added “{category.name}”.")


@router.get("/categories/{category_id:int}/edit")
def edit_category(request: Request, category_id: int, session: SessionDep) -> Response:
    category = _get_category(session, category_id)
    if is_htmx(request):
        return _page(
            request, "categories/_form_dialog.html", {"f": _form_context(session, category)}
        )
    return _page(
        request, "categories/form.html", {"f": _form_context(session, category, in_dialog=False)}
    )


@router.post("/categories/{category_id:int}")
def update_category(
    request: Request, category_id: int, session: SessionDep, form: FormDep
) -> Response:
    category = _get_category(session, category_id)
    values = _posted(form)
    changes: dict[str, Any] = {"name": values["name"], "icon": values["icon"]}
    if "color" in form:
        changes["color"] = values["color"] or None
    if "kind" in form:  # a built-in category's kind select is disabled and posts nothing
        changes["kind"] = values["kind"]
    try:
        categories.update_category(session, category.id, **changes)
    except CategoryError as err:
        return _form_error(request, session, category, form, err)
    session.commit()
    return _saved(request, session, f"Saved “{category.name}”.")


@router.post("/categories/{category_id:int}/hidden")
def set_hidden(request: Request, category_id: int, session: SessionDep, form: FormDep) -> Response:
    category = _get_category(session, category_id)
    hidden = bool(form.get("hidden"))
    try:
        categories.set_hidden(session, category.id, hidden)
    except CategoryError as err:
        if is_htmx(request):
            raise HTTPException(409, err.errors["is_hidden"]) from None
        return redirect(request, "/categories", "error", err.errors["is_hidden"])
    session.commit()
    if hidden:
        message = (
            f"“{category.name}” is hidden from pickers and budget lists. Its spending still counts."
        )
    else:
        message = f"“{category.name}” is shown again."
    if not is_htmx(request):
        return redirect(request, "/categories", "success", message)
    return _page(
        request,
        "categories/_row.html",
        {"c": categories.get_usage(session, category.id)},
        oob=flash_oob("success", message),
    )


# --------------------------------------------------------------------------- merge


def _merge_context(
    session: Session,
    source: Any,
    target_id: int | None,
    *,
    errors: dict[str, str] | None = None,
    in_dialog: bool = True,
) -> dict[str, Any]:
    preview = None
    errors = dict(errors or {})
    if target_id is not None and not errors:
        try:
            preview = categories.merge_preview(session, source.id, target_id)
        except CategoryError as err:
            errors.update(err.errors)
    return {
        "source": CategoryRef.of(source),
        "targets": [c for c in _refs(session) if c.id != source.id],
        "target_id": target_id,
        "preview": preview,
        "errors": errors,
        "in_dialog": in_dialog,
    }


def _target_id(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except TypeError, ValueError:
        return None


@router.get("/categories/{category_id:int}/merge")
def merge_dialog(
    request: Request, category_id: int, session: SessionDep, target_id: str = ""
) -> Response:
    source = _get_category(session, category_id)
    if source.system_key is not None:
        raise HTTPException(409, f"“{source.name}” is built in and can't be merged away.")
    if is_htmx(request):
        m = _merge_context(session, source, _target_id(target_id))
        return _page(request, "categories/_merge_dialog.html", {"m": m})
    m = _merge_context(session, source, _target_id(target_id), in_dialog=False)
    return _page(request, "categories/merge.html", {"m": m})


@router.get("/categories/{category_id:int}/merge/preview")
def merge_preview(
    request: Request, category_id: int, session: SessionDep, target_id: str = ""
) -> Response:
    source = _get_category(session, category_id)
    if not is_htmx(request):
        query = urlencode({"target_id": target_id}) if target_id else ""
        return redirect(request, f"/categories/{source.id}/merge" + (f"?{query}" if query else ""))
    return _page(
        request,
        "categories/_merge_preview.html",
        {"m": _merge_context(session, source, _target_id(target_id))},
    )


@router.post("/categories/{category_id:int}/merge")
def merge(request: Request, category_id: int, session: SessionDep, form: FormDep) -> Response:
    source = _get_category(session, category_id)
    target_id = _target_id(form.get("target_id"))
    try:
        result = categories.merge_categories(session, source.id, target_id)
    except CategoryError as err:
        session.rollback()
        source = _get_category(session, category_id)
        if is_htmx(request):
            m = _merge_context(session, source, target_id, errors=err.errors)
            return _page(request, "categories/_merge_form.html", {"m": m}, status_code=422)
        m = _merge_context(session, source, target_id, errors=err.errors, in_dialog=False)
        return _page(request, "categories/merge.html", {"m": m}, status_code=422)
    session.commit()
    moved = result.counts.transactions
    detail = f": {moved:,} transaction{'s' if moved != 1 else ''} moved" if moved else ""
    return redirect(
        request,
        "/categories",
        "success",
        f"Merged “{result.source.name}” into “{result.target.name}”{detail}.",
    )
