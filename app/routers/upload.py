"""
Upload endpoint.

Flow:
    1. Validate actual file type (not just extension) and size.
    2. Determine page count (PDF page count; images are always 1 page).
    3. Parse split value, if given. If blank, the frontend is expected to
       show the "proceed without split value?" confirmation BEFORE calling
       this endpoint -- but we also accept an explicit `confirmed_no_split`
       flag as a server-side backstop so this rule can't be bypassed by
       calling the API directly.
    4. Create the Job record (status=uploaded) and hand off for processing.

Resource limits (max file size / page count) are intentionally centralized
in app/core/config.py rather than hardcoded here, since these were flagged
as "needs a deliberate decision" during requirements review.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.job_store import save_job
from app.core import debug_store
from app.models.schemas import Job, JobStatus, SplitRange, SupportedFileType
from app.services.file_validation import (
    FileValidationError,
    detect_and_validate_file_type,
    get_page_count,
)
from app.services import ocr_extraction
from app.services.invoice_grouping import PageInvoiceNumberResult, group_pages_into_invoices
from app.services.native_pdf_extraction import (
    ExtractionSource,
    extract_invoice_group_fields,
    extract_page_invoice_numbers,
)
from app.services.split_parser import SplitValueError, parse_split_value

router = APIRouter(prefix="/api/jobs", tags=["upload"])


def _pages_in_scope(split_ranges: list[SplitRange], page_count: int) -> list[int]:
    if not split_ranges:
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for split_range in split_ranges:
        pages.update(range(split_range.start_page, split_range.end_page + 1))
    return sorted(pages)


def _status_after_extraction(invoice_groups: list) -> JobStatus:
    return (
        JobStatus.REVIEW_REQUIRED
        if any(group.needs_user_review for group in invoice_groups)
        else JobStatus.READY_TO_EXPORT
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    split_value: str | None = Form(default=None),
    confirmed_no_split: bool = Form(default=False),
    current_user: str = Depends(get_current_user),
):
    contents = await file.read()

    try:
        file_type: SupportedFileType = detect_and_validate_file_type(
            filename=file.filename,
            contents=contents,
            max_size_bytes=settings.max_file_size_bytes,
        )
    except FileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    page_count = get_page_count(contents, file_type)
    if page_count > settings.max_pages_per_file:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This file has {page_count} pages, which exceeds the "
                f"{settings.max_pages_per_file}-page limit."
            ),
        )

    if not split_value and not confirmed_no_split:
        raise HTTPException(
            status_code=409,
            detail=(
                "No split value has been entered. Confirm you want to "
                "proceed without one, or provide a split value."
            ),
        )

    try:
        split_ranges = parse_split_value(split_value, page_count)
    except SplitValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = Job(
        user_id=current_user,
        original_filename=file.filename,
        file_type=file_type,
        page_count=page_count,
        split_value_raw=split_value,
        split_ranges=split_ranges,
        status=JobStatus.QUEUED,
    )

    # Debugging aids only (OFF unless settings.debug_mode -- see
    # app/core/config.py / app/core/debug_store.py). Never consulted by
    # extraction itself.
    on_page_text = (
        # Raw text extracted per page, before any label-matching --
        # GET /api/jobs/{job_id}/debug/raw-text.
        (lambda page_number, source, text: debug_store.record_page_text(job.job_id, page_number, source.value, text))
        if settings.debug_mode
        else None
    )
    on_table_diagnostics = (
        # Line-item table extraction decisions (header detection, column
        # boundaries, attempted row assignment) per invoice group --
        # GET /api/jobs/{job_id}/debug/table-parsing.
        (lambda diagnostics: debug_store.record_table_parsing_diagnostics(job.job_id, diagnostics))
        if settings.debug_mode
        else None
    )

    # TODO: hand off to async worker (Celery/RQ) instead of running inline.
    # Kept synchronous for now per agreed phase-1 scope.
    #
    # PDF: native text-layer extraction is tried first (cheap, HIGH-eligible);
    # any page flagged as having no usable text layer falls back to OCR
    # automatically (see native_pdf_extraction._get_page_text_cached) --
    # pages that don't need it are never OCR'd, for both speed and accuracy.
    # Plain images have no text-layer concept at all, so they always OCR.
    if file_type == SupportedFileType.PDF:
        scoped_pages = _pages_in_scope(split_ranges, page_count)

        # Shared across BOTH extraction passes below -- the cheap per-page
        # pass (extract_page_invoice_numbers) and the heavier per-group
        # pass (extract_invoice_group_fields, once per invoice group) used
        # to each independently read/OCR every page from scratch, so a
        # single-invoice job would read every page TWICE (confirmed via
        # the raw-text debug endpoint: a 4-page PDF produced 8 page
        # entries). Passing the same page_cache dict into both means the
        # second pass reuses whatever the first already read instead of
        # repeating the work -- see native_pdf_extraction._get_page_text_cached.
        page_cache: dict = {}

        page_results = extract_page_invoice_numbers(
            contents,
            scoped_pages,
            ocr_page_text_fn=ocr_extraction.extract_text_from_image_bytes,
            on_page_text=on_page_text,
            page_cache=page_cache,
        )
        invoice_groups = group_pages_into_invoices(page_results, split_ranges)

        for group in invoice_groups:
            header_fields, header_field_confidences, line_items = extract_invoice_group_fields(
                contents,
                group.source_page_list,
                ocr_page_text_fn=ocr_extraction.extract_text_from_image_bytes,
                on_page_text=on_page_text,
                page_cache=page_cache,
                on_table_diagnostics=on_table_diagnostics,
            )
            group.header_fields = header_fields
            group.header_field_confidences = header_field_confidences
            group.line_items = line_items

        job.invoice_groups = invoice_groups
        job.status = _status_after_extraction(invoice_groups)

    elif file_type in (SupportedFileType.JPEG, SupportedFileType.PNG, SupportedFileType.WEBP):
        # No text-layer concept for a plain image -- always OCR. OCR'd once
        # (not once for the invoice number and again for the rest of the
        # fields) and the resulting text reused for both, via the
        # *_from_text variants. Always exactly one "page", so grouping is
        # trivial, but still goes through the same
        # group_pages_into_invoices() used by the PDF path rather than
        # hand-building an InvoiceGroup, so both paths produce
        # identically-shaped results.
        ocr_text = ocr_extraction.extract_text_from_image_bytes(contents)
        if on_page_text is not None:
            on_page_text(1, ExtractionSource.OCR, ocr_text)

        invoice_number, confidence = ocr_extraction.extract_page_invoice_number_from_text(ocr_text)
        page_result = PageInvoiceNumberResult(
            page_number=1, invoice_number=invoice_number, confidence=confidence, has_text_layer=False
        )
        invoice_groups = group_pages_into_invoices([page_result])

        for group in invoice_groups:
            header_fields, header_field_confidences, line_items = ocr_extraction.extract_invoice_fields_from_text(
                ocr_text
            )
            group.header_fields = header_fields
            group.header_field_confidences = header_field_confidences
            group.line_items = line_items

        job.invoice_groups = invoice_groups
        job.status = _status_after_extraction(invoice_groups)

    save_job(job)

    return {"job_id": str(job.job_id), "status": job.status, "page_count": page_count}
