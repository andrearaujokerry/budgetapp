"""Import page (SYNC-8): upload an Amex CSV, preview it, import it; review held rows; attach.

* ``GET /import`` — the upload form and download instructions, or the preview of an upload
  (``?preview=<token>``, kept in memory for 30 minutes), the summary of an import just made
  (``?done=<id>``), rows held for review, the offer to attach a CSV-only account, and the history.
* ``POST /import/preview`` — multipart upload (read in memory, never written to disk) → 303 to the
  preview, or the page again with the problem (422).
* ``POST /import/commit`` / ``POST /import/discard`` — import or drop a preview → 303.
* ``POST /import/suspects/{id}/keep`` / ``…/delete`` — HTMX: the review card and a toast; plain
  form: 303 back to the page.
* ``POST /import/attach`` — move a CSV-only account onto a linked card → 303.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from budgetapp.config import Settings
from budgetapp.services import csv_import, reconcile
from budgetapp.web.deps import FormDep, NotifierDep, SessionDep, SettingsDep
from budgetapp.web.flash import flash_oob, redirect
from budgetapp.web.templating import is_htmx, render

router = APIRouter()

#: The whole request body may be at most this large (the file plus a few small fields).
MAX_BODY_BYTES = csv_import.MAX_FILE_BYTES + 64 * 1024
MAX_ERRORS_SHOWN = 20


# --------------------------------------------------------------------------- uploads, in memory


class _UploadTooLarge(Exception):
    pass


class InMemoryMultiPartParser(MultiPartParser):
    """Starlette's parser, but an uploaded file never rolls over to a temporary file on disk.

    Uploads are spooled in memory up to ``spool_max_size``; the body is capped below it.
    """

    spool_max_size = MAX_BODY_BYTES + 64 * 1024


async def _limited(stream: AsyncIterator[bytes], limit: int) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > limit:
            raise _UploadTooLarge
        yield chunk


@dataclass(frozen=True)
class Upload:
    """The posted upload form: text fields, and the file read into memory."""

    fields: dict[str, str] = field(default_factory=dict)
    filename: str | None = None
    data: bytes | None = None
    problem: str | None = None


async def read_upload(request: Request) -> Upload:
    """Parse the multipart form in memory (at most :data:`MAX_BODY_BYTES`, one file)."""
    too_large = Upload(
        problem=f"The file is larger than {csv_import.MAX_FILE_BYTES // 2**20} MB. An Amex "
        "download of two years is usually under 2 MB; check that you chose the CSV file."
    )
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return Upload(problem="Choose the CSV file you downloaded.")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY_BYTES:
        return too_large
    parser = InMemoryMultiPartParser(
        request.headers,
        _limited(request.stream(), MAX_BODY_BYTES),
        max_files=1,
        max_fields=20,
        max_part_size=64 * 1024,
    )
    try:
        form = await parser.parse()
    except _UploadTooLarge:
        return too_large
    except MultiPartException:
        return Upload(problem="The upload couldn’t be read. Choose the file and try again.")
    fields: dict[str, str] = {}
    filename = data = None
    try:
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                if key == "file" and data is None and value.filename:
                    filename, data = value.filename, await value.read()
            else:
                fields.setdefault(key, value)
    finally:
        await form.close()
    return Upload(fields, filename, data)


UploadDep = Annotated[Upload, Depends(read_upload)]


# --------------------------------------------------------------------------- the page


def _target_options(targets: csv_import.ImportTargets) -> list[tuple[str, str]]:
    return [(option.value, option.label) for option in targets.options]


def page_context(
    session: Session,
    settings: Settings,
    *,
    preview_token: str | None = None,
    done: int | None = None,
    account: str | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    targets = csv_import.import_targets(session, production=settings.is_production)
    preview = csv_import.get_preview(preview_token) if preview_token else None
    return {
        "targets": targets,
        "target_options": _target_options(targets),
        "account": account if account is not None else targets.default,
        "errors": errors or {},
        "steps": csv_import.DOWNLOAD_STEPS,
        "preview": preview,
        "preview_expired": bool(preview_token) and preview is None,
        "max_errors": MAX_ERRORS_SHOWN,
        "done": csv_import.import_record(session, done) if done else None,
        "suspects": csv_import.list_suspects(session),
        "offers": csv_import.attach_offers(session),
        "history": csv_import.import_history(session),
        "reference_dates": csv_import.reference_dates(session),
        "max_file_mb": csv_import.MAX_FILE_BYTES // 2**20,
    }


@router.get("/import")
def imports_page(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    preview: str | None = None,
    done: int | None = None,
) -> Response:
    context = page_context(session, settings, preview_token=preview, done=done)
    return render(request, "imports/index.html", context, active_nav="imports")


@router.post("/import/preview")
def preview_upload(
    request: Request, session: SessionDep, settings: SettingsDep, upload: UploadDep
) -> Response:
    """Parse the uploaded file and show its preview (nothing is imported yet)."""
    account = upload.fields.get("account", "")
    errors: dict[str, str] = {}
    if upload.problem:
        errors["file"] = upload.problem
    elif not upload.data:
        errors["file"] = "Choose the CSV file you downloaded."
    if not errors:
        try:
            target = csv_import.parse_target(account)
            result = csv_import.preview(
                session,
                upload.data or b"",
                upload.filename,
                target,
                production=settings.is_production,
            )
        except csv_import.ImportRefused as exc:
            errors["account" if exc.kind == "account" else "file"] = exc.message
        else:
            return redirect(request, f"/import?preview={result.token}")
    context = page_context(session, settings, account=account, errors=errors)
    return render(request, "imports/index.html", context, active_nav="imports", status_code=422)


def _import_message(outcome: Any, held: int) -> str:
    parts = [f"{outcome.inserted_count:,} added"]
    if held:
        parts[0] += f" ({held:,} held for review)"
    if outcome.matched_plaid_count:
        parts.append(f"{outcome.matched_plaid_count:,} merged with Plaid transactions")
    if outcome.duplicate_count:
        parts.append(f"{outcome.duplicate_count:,} already imported")
    if outcome.error_count:
        parts.append(f"{outcome.error_count:,} with errors skipped")
    return f"Imported {outcome.filename}: " + ", ".join(parts) + "."


@router.post("/import/commit")
def commit_import(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    notifier: NotifierDep,
    form: FormDep,
) -> Response:
    token = str(form.get("token", ""))
    try:
        outcome = csv_import.commit(
            session, token, notifier=notifier, production=settings.is_production
        )
    except csv_import.ImportRefused as exc:
        session.rollback()
        return redirect(request, "/import", "warning", exc.message)
    session.commit()
    record = csv_import.import_record(session, outcome.csv_import_id or 0)
    held = record.suspects if record is not None else 0
    return redirect(
        request,
        f"/import?done={outcome.csv_import_id}",
        "success",
        _import_message(outcome, held),
    )


@router.post("/import/discard")
def discard_preview(request: Request, form: FormDep) -> Response:
    csv_import.discard_preview(str(form.get("token", "")))
    return redirect(request, "/import")


# --------------------------------------------------------------------------- held for review


def _suspects_response(request: Request, session: Session, text: str) -> Response:
    if is_htmx(request):
        context = {"suspects": csv_import.list_suspects(session), "conn": None}
        return render(
            request,
            "imports/_suspects.html",
            context,
            active_nav="imports",
            oob=flash_oob("success", text),
        )
    return redirect(request, "/import", "success", text)


@router.post("/import/suspects/{transaction_id}/keep")
def keep_suspect(request: Request, transaction_id: int, session: SessionDep) -> Response:
    """The user says the charge is real: it counts from now on and is never flagged again."""
    if not csv_import.keep_suspect(session, transaction_id):
        raise HTTPException(404)
    session.commit()
    return _suspects_response(request, session, "Kept. It now counts in your totals.")


@router.post("/import/suspects/{transaction_id}/delete")
def delete_suspect(request: Request, transaction_id: int, session: SessionDep) -> Response:
    """The user says the charge duplicates one Plaid has: delete the CSV row."""
    if not csv_import.delete_suspect(session, transaction_id):
        raise HTTPException(404)
    session.commit()
    return _suspects_response(request, session, "Deleted the duplicate CSV row.")


# --------------------------------------------------------------------------- attaching


@router.post("/import/attach")
def attach_account(request: Request, session: SessionDep, form: FormDep) -> Response:
    """Move a CSV-only account's history onto a linked card, then reconcile."""
    manual = str(form.get("manual_id", ""))
    target = str(form.get("account_id", ""))
    if not (manual.isdigit() and target.isdigit()):
        return redirect(request, "/import", "error", "Choose the linked card to attach to.")
    try:
        result = reconcile.attach_manual_account(session, int(manual), int(target))
    except reconcile.AttachError as exc:
        session.rollback()
        return redirect(request, "/import", "error", str(exc))
    session.commit()
    paired = result.reconcile.paired
    text = f"Attached: {result.moved_transactions:,} transactions moved to the linked card"
    if paired:
        text += f", {paired:,} merged with the same Plaid transactions"
    if result.reconcile.suspects_flagged:
        text += f", {result.reconcile.suspects_flagged:,} held for review"
    return redirect(request, "/import", "success", text + ".")
