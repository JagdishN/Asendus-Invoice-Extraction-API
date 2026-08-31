from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.auth import get_current_user
from app.core import debug_store
from app.core.config import settings
from app.core.job_store import get_job, list_jobs, save_job
from app.models.schemas import ExportedFile, Job, JobStatus
from app.services.csv_export import (
    CSV_MEDIA_TYPE,
    LINE_ITEM_EXPORT_COLUMNS,
    ZIP_MEDIA_TYPE,
    build_filtered_zip_bytes,
    build_invoice_csv_bytes,
    find_group_by_invoice_number,
    generate_and_persist_exports,
    invoice_csv_filename,
    populate_job_with_dummy_data,
    resolve_selected_line_item_fields,
)
from app.services.export_storage import export_storage

router = APIRouter(prefix="/api/jobs", tags=["history"])


@router.get("")
async def get_history(current_user: str = Depends(get_current_user)):
    """
    Requires login, but deliberately NOT filtered by current_user -- still
    returns all jobs, per the shared-within-org visibility model already
    agreed with the client (see design notes in README). This task was
    scoped to adding authentication, not changing that visibility model;
    revisit if per-user siloing is wanted later.
    """
    return list_jobs()


@router.get("/{job_id}")
async def get_job_detail(job_id: UUID, current_user: str = Depends(get_current_user)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.get("/{job_id}/debug/raw-text")
async def get_job_debug_raw_text(job_id: UUID, current_user: str = Depends(get_current_user)):
    """
    Diagnostic-only: the raw per-page text extracted BEFORE any
    label-matching runs (native PDF text layer or OCR, whichever was used
    for that page), for figuring out why fields did or didn't get found.
    404s unless settings.debug_mode is on (DEBUG=true env var) -- never
    exposed by default, and only ever has data for jobs uploaded while
    debug mode was on (see app/core/debug_store.py).
    """
    if not settings.debug_mode:
        raise HTTPException(status_code=404, detail="Not found.")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"job_id": str(job_id), "pages": debug_store.get_raw_text(job_id)}


@router.get("/{job_id}/debug/table-parsing")
async def get_job_debug_table_parsing(job_id: UUID, current_user: str = Depends(get_current_user)):
    """
    Diagnostic-only: what the line-item table extraction actually did for
    each invoice group in this job -- whether find_tables() (PyMuPDF's
    ruled-line detection) found anything usable, and if not, what the
    header-row-driven positional fallback did: whether a header row was
    detected at all, which configured column keywords matched and at what
    text/coordinates, the column boundaries computed from those
    positions, and for the first few data rows after the header, what
    values got assigned to which column given those boundaries -- shown
    even when the result is wrong or the final line_items list came back
    empty, since that's exactly the case this exists to debug. 404s
    unless settings.debug_mode is on (DEBUG=true env var) -- never
    exposed by default, and only ever has data for jobs uploaded while
    debug mode was on (see app/core/debug_store.py).
    """
    if not settings.debug_mode:
        raise HTTPException(status_code=404, detail="Not found.")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"job_id": str(job_id), "tables": debug_store.get_table_parsing_diagnostics(job_id)}


def _require_job_with_invoices(job_id: UUID) -> Job:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.invoice_groups:
        raise HTTPException(status_code=404, detail="Job has no invoice groups to export yet.")
    return job


def _ensure_exports_generated(job: Job) -> None:
    """Generates + persists CSV/zip artifacts once, the first time they're
    requested; subsequent requests reuse what's already on job.export."""
    if job.export is None:
        job.export = generate_and_persist_exports(job)
        job.status = JobStatus.EXPORTED
        save_job(job)


def _stream_bytes(content: bytes, filename: str, media_type: str) -> StreamingResponse:
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _serve_exported_file(job: Job, exported_file: ExportedFile, media_type: str) -> StreamingResponse:
    content = export_storage.read_bytes(job.job_id, exported_file.filename)
    return _stream_bytes(content, exported_file.filename, media_type)


def _parse_columns_param(columns: str | None) -> list[str] | None:
    if not columns:
        return None
    return [c.strip() for c in columns.split(",") if c.strip()]


_COLUMNS_QUERY_DESCRIPTION = (
    "Comma-separated line-item field names to include (see GET "
    "/api/jobs/export/columns for the available names) -- omit, or leave "
    "empty, for every column. A column-filtered download is always built "
    "fresh, never cached: repeated calls with no `columns` still reuse "
    "the same persisted 'every column' export."
)


@router.get("/export/columns")
async def get_export_columns(current_user: str = Depends(get_current_user)):
    """
    The line-item table's exportable columns (field_name + display label,
    in canonical CSV order) -- for a client-side "choose which columns to
    export" dropdown. Not job-specific: this is the same fixed set,
    schema-derived list for every job (see csv_export.LINE_ITEM_EXPORT_
    COLUMNS), so no job_id is needed. Default UI state should be every
    column selected -- pass all field_names (or omit `columns` entirely)
    on export to get that.
    """
    return {"columns": LINE_ITEM_EXPORT_COLUMNS}


@router.get("/{job_id}/export")
async def export_job(
    job_id: UUID,
    columns: str | None = Query(None, description=_COLUMNS_QUERY_DESCRIPTION),
    current_user: str = Depends(get_current_user),
):
    """Default download: the zip if the job has more than one invoice,
    otherwise the single CSV directly."""
    job = _require_job_with_invoices(job_id)
    selected_fields = resolve_selected_line_item_fields(_parse_columns_param(columns))

    if selected_fields is not None:
        if len(job.invoice_groups) > 1:
            content, filename = build_filtered_zip_bytes(job, selected_fields)
            return _stream_bytes(content, filename, ZIP_MEDIA_TYPE)
        only_group = job.invoice_groups[0]
        content = build_invoice_csv_bytes(only_group, selected_fields)
        return _stream_bytes(content, invoice_csv_filename(job, only_group), CSV_MEDIA_TYPE)

    _ensure_exports_generated(job)
    if job.export.zip_file is not None:
        return _serve_exported_file(job, job.export.zip_file, ZIP_MEDIA_TYPE)

    only_group = job.invoice_groups[0]
    exported_file = job.export.invoice_files[str(only_group.group_id)]
    return _serve_exported_file(job, exported_file, CSV_MEDIA_TYPE)


@router.get("/{job_id}/export/zip")
async def export_job_zip(
    job_id: UUID,
    columns: str | None = Query(None, description=_COLUMNS_QUERY_DESCRIPTION),
    current_user: str = Depends(get_current_user),
):
    """Explicitly returns the zip archive. 404s for single-invoice jobs,
    since no zip is generated for those (use /export or
    /export/{invoice_number} instead) -- this was a judgment call, not
    explicitly specified; confirm it's the behavior you want."""
    job = _require_job_with_invoices(job_id)
    if len(job.invoice_groups) <= 1:
        raise HTTPException(
            status_code=404,
            detail=(
                "This job has only one invoice, so no zip archive was generated. "
                "Use /export or /export/{invoice_number} instead."
            ),
        )

    selected_fields = resolve_selected_line_item_fields(_parse_columns_param(columns))
    if selected_fields is not None:
        content, filename = build_filtered_zip_bytes(job, selected_fields)
        return _stream_bytes(content, filename, ZIP_MEDIA_TYPE)

    _ensure_exports_generated(job)
    return _serve_exported_file(job, job.export.zip_file, ZIP_MEDIA_TYPE)


@router.get("/{job_id}/export/{invoice_number:path}")
async def export_invoice_csv(
    job_id: UUID,
    invoice_number: str,
    columns: str | None = Query(None, description=_COLUMNS_QUERY_DESCRIPTION),
    current_user: str = Depends(get_current_user),
):
    """
    Returns just one invoice's CSV, looked up by invoice number. Uses the
    ':path' converter (not a plain '{invoice_number}') because invoice
    numbers routinely contain '/' (e.g. "INV/2026/001") -- a plain path
    param stops at the first '/' and would silently 404 on most real
    invoice numbers.
    """
    job = _require_job_with_invoices(job_id)
    group = find_group_by_invoice_number(job, invoice_number)
    if group is None:
        raise HTTPException(
            status_code=404,
            detail=f"Invoice '{invoice_number}' was not found in this job.",
        )

    selected_fields = resolve_selected_line_item_fields(_parse_columns_param(columns))
    if selected_fields is not None:
        content = build_invoice_csv_bytes(group, selected_fields)
        return _stream_bytes(content, invoice_csv_filename(job, group), CSV_MEDIA_TYPE)

    _ensure_exports_generated(job)
    exported_file = job.export.invoice_files[str(group.group_id)]
    return _serve_exported_file(job, exported_file, CSV_MEDIA_TYPE)


# ---------------------------------------------------------------------------
# TEMPORARY -- lets you populate a real uploaded job with fake invoice data
# so the upload -> export flow can be exercised before real OCR extraction
# is wired in. Delete this endpoint once extraction lands (see
# app/services/csv_export.py for the matching dummy-data generator).
# ---------------------------------------------------------------------------
@router.post("/{job_id}/_debug/populate-dummy-data")
async def debug_populate_dummy_data(job_id: UUID, current_user: str = Depends(get_current_user)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    populate_job_with_dummy_data(job)
    job.export = None  # force re-generation against the new dummy data
    save_job(job)
    return job
