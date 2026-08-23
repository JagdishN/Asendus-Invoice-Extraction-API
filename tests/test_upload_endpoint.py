from tests.conftest import requires_tesseract
from tests.pdf_builders import SAMPLE_IRN, build_blank_pdf_bytes, build_invoice_pdf_bytes, build_valid_png_bytes

SAMPLE_INVOICE = dict(
    # A real (IRN-shaped) invoice_number here so the happy-path upload test
    # gets HIGH confidence -> needs_user_review=False -> ready_to_export,
    # per the confirmed IRN confidence rules in native_pdf_extraction.py.
    invoice_number=SAMPLE_IRN,
    invoice_date="01/01/2026",
    party_name="Acme Corp",
    gstin="27AAAAA0000A1Z5",
    items=[("Widget", "1234", "1", "100.00", "100.00", "9.00", "9.00", "118.00")],
    subtotal="100.00",
    total="118.00",
)


def _upload(client, file_bytes, filename="invoice.pdf", content_type="application/pdf", **form):
    form.setdefault("confirmed_no_split", "true")
    return client.post(
        "/api/jobs",
        files={"file": (filename, file_bytes, content_type)},
        data=form,
    )


def test_upload_requires_split_confirmation_when_no_split_value_given(client):
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    resp = client.post("/api/jobs", files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")})
    assert resp.status_code == 409


def test_upload_rejects_content_that_isnt_a_real_pdf(client):
    resp = _upload(client, b"not a real pdf at all")
    assert resp.status_code == 400


def test_upload_native_pdf_runs_extraction_and_grouping_synchronously(client):
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    resp = _upload(client, pdf_bytes)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "ready_to_export"

    detail = client.get(f"/api/jobs/{body['job_id']}").json()
    assert len(detail["invoice_groups"]) == 1
    group = detail["invoice_groups"][0]
    assert group["invoice_number"] == SAMPLE_IRN
    # "Acme Corp" was drawn via "Bill To:" -> buyer_name, not party_name
    # (the vendor, via the "For <company>" signature line).
    assert group["header_fields"]["buyer_name"] == "Acme Corp"
    assert group["header_fields"]["party_name"] == "Test Vendor Co"
    assert len(group["line_items"]) == 1


def test_upload_multi_invoice_pdf_produces_multiple_groups(client):
    invoices = [
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/011"),
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/012"),
    ]
    pdf_bytes = build_invoice_pdf_bytes(invoices)
    resp = _upload(client, pdf_bytes)

    detail = client.get(f"/api/jobs/{resp.json()['job_id']}").json()
    assert len(detail["invoice_groups"]) == 2
    assert {g["invoice_number"] for g in detail["invoice_groups"]} == {"INV/2026/011", "INV/2026/012"}


@requires_tesseract
def test_upload_scanned_pdf_has_no_text_leaves_status_review_required(client):
    # A blank page has no text layer, so this now also exercises the OCR
    # fallback path (render -> OCR the blank page -> still finds nothing)
    # rather than skipping straight to "give up" as before OCR existed.
    pdf_bytes = build_blank_pdf_bytes(page_count=1)
    resp = _upload(client, pdf_bytes)
    assert resp.json()["status"] == "review_required"


@requires_tesseract
def test_upload_image_file_runs_ocr_and_never_reaches_high_confidence(client):
    png_bytes = build_valid_png_bytes(text="IRN No: NOTAREALIRN123")
    resp = _upload(client, png_bytes, filename="x.png", content_type="image/png")
    assert resp.status_code == 201
    body = resp.json()

    detail = client.get(f"/api/jobs/{body['job_id']}").json()
    assert len(detail["invoice_groups"]) == 1
    group = detail["invoice_groups"][0]
    # OCR-sourced confidence must never be HIGH; a non-64-hex "IRN" match
    # is specifically graded LOW (not just REVIEW) per the OCR confidence
    # rules in native_pdf_extraction.py.
    assert group["invoice_number_confidence"] in ("review", "low", "not_found")
    assert group["needs_user_review"] is True


def test_history_lists_uploaded_jobs(client):
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    _upload(client, pdf_bytes)

    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_job_detail_404_for_unknown_job(client):
    import uuid

    resp = client.get(f"/api/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
