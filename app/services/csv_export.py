"""
CSV export for a Job's invoice_groups: one .csv per InvoiceGroup, zipped
together when a job has more than one. Generated files are persisted via
export_storage.py (local disk for now) so they can be served again without
regenerating -- see JobExportMetadata in schemas.py for what gets recorded.

Client-confirmed: the CSV body is the line-items TABLE ONLY -- no header
block (invoice number, buyer/party details, tax-bracket summary, etc.),
no blank separator row. Columns are derived from InvoiceLineItem's own
fields so they can't drift out of sync with schemas.py. header_fields is
still read from InvoiceGroup elsewhere (filename generation -- see
_invoice_filename_seed below) even though it no longer appears in the CSV
content itself; the file NAMING convention is unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import datetime, timezone

from app.models.schemas import (
    ConfidenceBand,
    ExportedFile,
    InvoiceGroup,
    InvoiceLineItem,
    Job,
    JobExportMetadata,
    JobStatus,
)
from app.services.export_storage import export_storage
from app.services.filename_safety import resolve_unique_filenames, sanitize_filename

CSV_MEDIA_TYPE = "text/csv"
ZIP_MEDIA_TYPE = "application/zip"

# Line-item columns are derived from the model itself (minus the one field
# that isn't spreadsheet-shaped) so a schema change can't silently drift
# out of sync with the exported columns.
_EXCLUDED_LINE_ITEM_FIELDS = {"field_confidences"}
_LINE_ITEM_COLUMN_LABELS = {
    "line_number": "Line #",
    "item_description": "Item Description",
    "pack": "Pack",
    "hsn_sac": "HSN/SAC",
    "batch_number": "Batch Number",
    "expiry_date": "Expiry Date",
    "mfg_date": "Mfg Date",
    "quantity": "Quantity",
    "quantity_sold": "Qty Sold",
    "quantity_free": "Qty Free",
    "quantity_total": "Qty Total",
    "uom": "UOM",
    "unit_rate": "Unit Rate",
    "mrp": "MRP",
    "ptr": "PTR",
    "rate_pts": "Rate Pts",
    "taxable_value": "Taxable Value",
    "discount_rate": "Discount %",
    "discount_amount": "Discount",
    "cgst_rate": "CGST Rate",
    "cgst_amount": "CGST Amount",
    "sgst_rate": "SGST Rate",
    "sgst_amount": "SGST Amount",
    "igst_rate": "IGST Rate",
    "igst_amount": "IGST Amount",
    "line_total": "Line Total",
    "source_page": "Source Page",
}

_LINE_ITEM_FIELDS = [
    name for name in InvoiceLineItem.model_fields if name not in _EXCLUDED_LINE_ITEM_FIELDS
]

# The exportable line-item column list a client-facing "choose which
# columns to export" dropdown renders from -- field_name is what the
# export routes' `columns` query param expects back; label is the
# display text. Order matches the CSV's own column order. Derived
# entirely from _LINE_ITEM_FIELDS/_LINE_ITEM_COLUMN_LABELS so it can't
# drift out of sync with what actually gets written.
LINE_ITEM_EXPORT_COLUMNS = [
    {"field_name": f, "label": _LINE_ITEM_COLUMN_LABELS.get(f, f.replace("_", " ").title())}
    for f in _LINE_ITEM_FIELDS
]


def resolve_selected_line_item_fields(columns: list[str] | None) -> list[str] | None:
    """
    Validates/filters a caller-supplied list of line-item field names
    against the real set (_LINE_ITEM_FIELDS), preserving the CANONICAL
    column order regardless of what order the caller sent them in -- so a
    CSV's column order stays predictable no matter how a frontend
    multi-select happens to serialize its selection.

    Returns None (meaning "no filter -- use every column") when `columns`
    is None/empty, OR when none of the given names matched a real field
    (a safe fallback for a malformed/stale request rather than exporting
    zero columns). A partial match filters down to just the recognized
    subset, in canonical order -- deliberately not an all-or-nothing
    validation, since dropping one unrecognized name shouldn't blank the
    rest of a deliberate selection.
    """
    if not columns:
        return None
    selected = set(columns)
    resolved = [f for f in _LINE_ITEM_FIELDS if f in selected]
    return resolved or None


def _build_invoice_csv_bytes(group: InvoiceGroup, selected_fields: list[str] | None = None) -> bytes:
    fields = selected_fields if selected_fields is not None else _LINE_ITEM_FIELDS
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([_LINE_ITEM_COLUMN_LABELS.get(f, f.replace("_", " ").title()) for f in fields])
    for item in group.line_items:
        writer.writerow([getattr(item, field_name) for field_name in fields])

    # utf-8-sig (BOM) so Excel/Windows correctly detect UTF-8 when a user
    # just double-clicks a downloaded CSV instead of importing it explicitly.
    return buffer.getvalue().encode("utf-8-sig")


def _invoice_filename_seed(job: Job, group: InvoiceGroup, generated_at: datetime) -> str:
    """
    <CustomerName>_<DateOfInvoice>_<last5CharsOfIRN> -- per the confirmed
    naming convention. Sanitization and collision-uniqueness are handled by
    resolve_unique_filenames (caller appends the .csv extension).

    CustomerName is buyer_name (the invoice recipient), falling back to
    "Customer_Unspecified" when it wasn't extracted -- deliberately NOT
    party_name, which represents the vendor/seller (see
    native_pdf_extraction.py's "FOR <company>" signature-line extraction).

    DateOfInvoice uses the extracted invoice_date when available, falling
    back to the export-generation date only if extraction failed (kept as
    a fallback rather than leaving the filename component blank).
    """
    customer_name = group.header_fields.get("buyer_name") or "Customer_Unspecified"
    invoice_date = group.header_fields.get("invoice_date") or generated_at.strftime("%Y%m%d")
    irn = group.invoice_number or ""
    irn_suffix = irn[-5:] if len(irn) >= 5 else (irn or "NoIRN")
    return f"{customer_name}_{invoice_date}_{irn_suffix}"


def generate_and_persist_exports(job: Job) -> JobExportMetadata:
    """
    Generates one CSV per invoice_group (+ a zip if there's more than one),
    persists them via export_storage, and returns the metadata to record on
    the job. Callers should check job.export first and only call this if
    it's missing, so already-generated files are served rather than
    regenerated.
    """
    generated_at = datetime.now(timezone.utc)
    filenames = resolve_unique_filenames(
        [_invoice_filename_seed(job, group, generated_at) for group in job.invoice_groups],
        fallback_prefix="invoice",
    )

    invoice_files: dict[str, ExportedFile] = {}
    csv_payloads: list[tuple[str, bytes]] = []

    for group, base_filename in zip(job.invoice_groups, filenames):
        filename = f"{base_filename}.csv"
        content = _build_invoice_csv_bytes(group)
        saved_path = export_storage.save_bytes(job.job_id, filename, content)
        invoice_files[str(group.group_id)] = ExportedFile(
            filename=filename,
            file_path=saved_path,
            size_bytes=len(content),
            sha256_checksum=hashlib.sha256(content).hexdigest(),
        )
        csv_payloads.append((filename, content))

    zip_file: ExportedFile | None = None
    if len(job.invoice_groups) > 1:
        zip_filename = f"invoices_{job.job_id}_{generated_at.strftime('%Y%m%d')}.zip"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_archive:
            for filename, content in csv_payloads:
                zip_archive.writestr(filename, content)
        zip_bytes = zip_buffer.getvalue()

        saved_zip_path = export_storage.save_bytes(job.job_id, zip_filename, zip_bytes)
        zip_file = ExportedFile(
            filename=zip_filename,
            file_path=saved_zip_path,
            size_bytes=len(zip_bytes),
            sha256_checksum=hashlib.sha256(zip_bytes).hexdigest(),
        )

    return JobExportMetadata(invoice_files=invoice_files, zip_file=zip_file, generated_at=generated_at)


# ---------------------------------------------------------------------------
# On-demand, column-filtered downloads -- deliberately NOT persisted via
# export_storage/job.export the way generate_and_persist_exports's output
# is: a column selection varies per request, while the cached artifact
# (job.export) must stay the stable "every column" default that repeated
# GET /export calls keep re-serving (see history.py's _ensure_exports_
# generated). Reuses _build_invoice_csv_bytes/_invoice_filename_seed so
# there's exactly one CSV-row-building/naming implementation either way.
# ---------------------------------------------------------------------------


def build_invoice_csv_bytes(group: InvoiceGroup, selected_fields: list[str] | None = None) -> bytes:
    """Public entry point for building one group's CSV outside the
    persisted-export flow (the export routes' column-filtered branch)."""
    return _build_invoice_csv_bytes(group, selected_fields)


def invoice_csv_filename(job: Job, group: InvoiceGroup) -> str:
    """Filename for a single group's on-demand CSV -- same naming
    convention as the persisted path (_invoice_filename_seed), computed
    fresh since there's no ExportedFile record to read it back from.
    Sanitized the same way resolve_unique_filenames sanitizes the
    persisted path's filenames: the seed can contain characters that are
    invalid in a filename (a raw invoice number routinely contains '/',
    e.g. "INV/2026/001") or, unsanitized, would land unescaped in a
    Content-Disposition header."""
    seed = _invoice_filename_seed(job, group, datetime.now(timezone.utc))
    return f"{sanitize_filename(seed, fallback_index=1, fallback_prefix='invoice')}.csv"


def build_filtered_zip_bytes(job: Job, selected_fields: list[str] | None) -> tuple[bytes, str]:
    """On-demand zip build for a column-filtered multi-invoice download --
    mirrors generate_and_persist_exports's zip step but returns bytes
    directly instead of persisting anything."""
    generated_at = datetime.now(timezone.utc)
    filenames = resolve_unique_filenames(
        [_invoice_filename_seed(job, group, generated_at) for group in job.invoice_groups],
        fallback_prefix="invoice",
    )
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_archive:
        for group, base_filename in zip(job.invoice_groups, filenames):
            zip_archive.writestr(f"{base_filename}.csv", _build_invoice_csv_bytes(group, selected_fields))
    zip_filename = f"invoices_{job.job_id}_{generated_at.strftime('%Y%m%d')}.zip"
    return zip_buffer.getvalue(), zip_filename


def find_group_by_invoice_number(job: Job, invoice_number: str) -> InvoiceGroup | None:
    """
    First match by invoice_number within the job. NOTE: invoice_number is
    not guaranteed unique within a job (split-range hard-fencing can
    produce two separate groups with the same number) -- this returns the
    first one found rather than disambiguating further. Confirm whether a
    group_id-based lookup is needed instead for that case.
    """
    for group in job.invoice_groups:
        if group.invoice_number == invoice_number:
            return group
    return None


# ---------------------------------------------------------------------------
# TEMPORARY -- dummy data for exercising the export pipeline before real
# OCR/PDF extraction is wired in for every file type (see README
# "Explicitly NOT yet implemented"). Delete this section, and the debug
# endpoint in app/routers/history.py that calls it, once extraction covers
# every upload path.
# ---------------------------------------------------------------------------


def populate_job_with_dummy_data(job: Job) -> Job:
    """TEMPORARY: mutates `job` in place with 3 fake InvoiceGroups so the
    upload -> export flow can be tested end to end without real extraction.
    header_fields uses the same canonical snake_case keys as
    native_pdf_extraction.py (party_name, invoice_date, etc.) so dummy and
    real data behave identically downstream (e.g. filename generation)."""
    job.invoice_groups = [
        InvoiceGroup(
            invoice_number="INV/2026/001",
            invoice_number_confidence=ConfidenceBand.HIGH,
            source_page_list=[1, 2],
            needs_user_review=False,
            header_fields={
                "party_name": "Sundar Traders Pvt Ltd",  # vendor/seller
                "invoice_date": "14/07/2026",
                "party_gstin": "27ABCDE1234F1Z5",
                "buyer_name": "Krishna Pharma Retail",
                "subtotal_taxable": 15500.0,
                "invoice_total": 18285.0,
            },
            header_field_confidences={
                "party_name": ConfidenceBand.HIGH,
                "invoice_date": ConfidenceBand.HIGH,
                "party_gstin": ConfidenceBand.HIGH,
                "buyer_name": ConfidenceBand.HIGH,
                "subtotal_taxable": ConfidenceBand.HIGH,
                "invoice_total": ConfidenceBand.HIGH,
            },
            line_items=[
                InvoiceLineItem(
                    line_number=1,
                    item_description="Industrial Bearings (Set of 4)",
                    hsn_sac="8482",
                    quantity=10,
                    uom="NOS",
                    unit_rate=850.0,
                    taxable_value=8500.0,
                    cgst_amount=765.0,
                    sgst_amount=765.0,
                    line_total=10030.0,
                    source_page=1,
                ),
                InvoiceLineItem(
                    line_number=2,
                    item_description="Hydraulic Hose 10m",
                    hsn_sac="4009",
                    quantity=5,
                    uom="NOS",
                    unit_rate=1400.0,
                    taxable_value=7000.0,
                    cgst_amount=630.0,
                    sgst_amount=630.0,
                    line_total=8260.0,
                    source_page=2,
                ),
            ],
        ),
        InvoiceGroup(
            invoice_number="INV/2026/002",
            invoice_number_confidence=ConfidenceBand.HIGH,
            source_page_list=[3],
            needs_user_review=False,
            header_fields={
                "party_name": "Krishna Electricals",  # vendor/seller
                "invoice_date": "18/07/2026",
                "party_gstin": "29XYZAB5678C1Z2",
                "buyer_name": "Om Hardware Store",
                "subtotal_taxable": 4200.0,
                "invoice_total": 4956.0,
            },
            header_field_confidences={
                "party_name": ConfidenceBand.HIGH,
                "invoice_date": ConfidenceBand.HIGH,
                "party_gstin": ConfidenceBand.HIGH,
                "buyer_name": ConfidenceBand.HIGH,
                "subtotal_taxable": ConfidenceBand.HIGH,
                "invoice_total": ConfidenceBand.HIGH,
            },
            line_items=[
                InvoiceLineItem(
                    line_number=1,
                    item_description="LED Panel Light 40W",
                    hsn_sac="9405",
                    quantity=12,
                    uom="NOS",
                    unit_rate=350.0,
                    taxable_value=4200.0,
                    igst_amount=756.0,
                    line_total=4956.0,
                    source_page=3,
                ),
            ],
        ),
        InvoiceGroup(
            invoice_number=None,
            invoice_number_confidence=ConfidenceBand.NOT_FOUND,
            source_page_list=[4, 5],
            is_non_contiguous_merge=False,
            needs_user_review=True,
            header_fields={
                "party_name": "(not detected - needs review)",
            },
            header_field_confidences={
                "party_name": ConfidenceBand.REVIEW,
            },
            line_items=[
                InvoiceLineItem(
                    line_number=1,
                    item_description="Assorted Fasteners - Box",
                    quantity=3,
                    uom="BOX",
                    unit_rate=220.0,
                    taxable_value=660.0,
                    line_total=660.0,
                    source_page=4,
                    field_confidences={"item_description": ConfidenceBand.LOW},
                ),
            ],
        ),
    ]
    job.status = JobStatus.READY_TO_EXPORT
    job.updated_at = datetime.now(timezone.utc)
    return job
