"""
GET /api/jobs/{job_id}/debug/raw-text and GET /api/jobs/{job_id}/debug/table-parsing
-- diagnostic-only routes (see app/core/debug_store.py), 404 unless
settings.debug_mode is on. Verifies both the gating (off by default) and
that the diagnostic data actually gets recorded and served when it's on.
"""
from app.core.config import settings
from tests.conftest import requires_tesseract
from tests.pdf_builders import build_alt_layout_invoice_pdf_bytes, build_invoice_pdf_bytes, build_valid_png_bytes

SAMPLE_INVOICE = dict(
    invoice_number="INV/2026/001",
    invoice_date="01/01/2026",
    party_name="Acme Corp",
    gstin="27AAAAA0000A1Z5",
    items=[],
    subtotal="0",
    total="0",
)

SAMPLE_INVOICE_WITH_RULED_TABLE = dict(
    SAMPLE_INVOICE,
    items=[("Industrial Bearings Set", "8482", "10", "850.00", "8500.00", "765.00", "765.00", "10030.00")],
)


def test_debug_raw_text_404s_when_debug_mode_off(client, monkeypatch):
    monkeypatch.setattr(settings, "debug_mode", False)
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/debug/raw-text").status_code == 404


def test_debug_raw_text_404s_for_unknown_job_even_when_debug_mode_on(client, monkeypatch):
    import uuid

    monkeypatch.setattr(settings, "debug_mode", True)
    assert client.get(f"/api/jobs/{uuid.uuid4()}/debug/raw-text").status_code == 404


def test_debug_raw_text_returns_native_page_text_when_debug_mode_on(client, monkeypatch):
    monkeypatch.setattr(settings, "debug_mode", True)
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/raw-text")
    assert debug_resp.status_code == 200
    pages = debug_resp.json()["pages"]
    # PDF page 1 must be recorded exactly ONCE -- the cheap per-page
    # invoice-number pass (extract_page_invoice_numbers) and the heavier
    # per-group field pass (extract_invoice_group_fields) share a single
    # page_cache (see app/routers/upload.py), so the second pass reuses
    # the first's already-read result instead of re-reading the page and
    # recording it again. Regression test for a real duplicate-extraction
    # bug: this used to be 2, not 1, for every native-text page.
    assert len(pages) == 1
    assert pages[0]["source"] == "native_pdf_text_layer"
    assert "INV/2026/001" in pages[0]["text"]


def test_debug_raw_text_one_entry_per_page_on_a_multi_page_pdf(client, monkeypatch):
    """
    Regression test for the real duplicate-extraction bug reported against
    a 4-page PDF: the raw-text debug output showed 8 page entries, pages
    1-4 each appearing twice with identical text, because the cheap
    per-page pass and the heavier per-group pass never shared results and
    each independently re-read/re-extracted every page. Uses a 4-page
    single invoice (continuation pages), same as the real report.
    """
    monkeypatch.setattr(settings, "debug_mode", True)
    invoice = dict(
        SAMPLE_INVOICE,
        pages=4,
        continuation_items=[("Widget", "1234", "1", "10.00", "10.00", "0.90", "0.90", "11.80")],
    )
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/raw-text")
    assert debug_resp.status_code == 200
    pages = debug_resp.json()["pages"]

    page_numbers = [p["page_number"] for p in pages]
    assert page_numbers == [1, 2, 3, 4]  # exactly one entry per page, in order -- not [1,1,2,2,3,3,4,4]
    assert len(set(page_numbers)) == len(page_numbers)  # no duplicates


@requires_tesseract
def test_debug_raw_text_returns_ocr_page_text_when_debug_mode_on(client, monkeypatch):
    monkeypatch.setattr(settings, "debug_mode", True)
    png_bytes = build_valid_png_bytes(text="IRN No: NOTAREALIRN123")
    resp = client.post(
        "/api/jobs",
        files={"file": ("x.png", png_bytes, "image/png")},
        data={"confirmed_no_split": "true"},
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/raw-text")
    assert debug_resp.status_code == 200
    pages = debug_resp.json()["pages"]
    assert len(pages) == 1
    assert pages[0]["source"] == "ocr"
    assert pages[0]["text"]  # non-empty -- OCR actually ran and text got recorded


def test_debug_table_parsing_404s_when_debug_mode_off(client, monkeypatch):
    monkeypatch.setattr(settings, "debug_mode", False)
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE_WITH_RULED_TABLE])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/debug/table-parsing").status_code == 404


def test_debug_table_parsing_shows_find_tables_succeeding_no_positional_fallback(client, monkeypatch):
    """A ruled-line (vector grid) table -- find_tables() should find it
    directly, so the positional fallback parser never runs at all."""
    monkeypatch.setattr(settings, "debug_mode", True)
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE_WITH_RULED_TABLE])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/table-parsing")
    assert debug_resp.status_code == 200
    tables = debug_resp.json()["tables"]
    assert len(tables) == 1

    diag = tables[0]
    assert diag["group_pages"] == [1]
    assert diag["find_tables"]["pymupdf_find_tables_available"] is True
    assert diag["find_tables"]["pages"][0]["tables_found"] >= 1
    assert diag["find_tables"]["pages"][0]["tables_used"] >= 1
    assert diag["positional_parser_ran"] is False
    assert diag["positional_parser"] is None
    assert diag["final_line_item_count"] == 1


def test_debug_table_parsing_shows_header_detection_and_column_boundaries_for_positional_fallback(client, monkeypatch):
    """A plain-text table with no ruled grid lines -- find_tables() finds
    nothing, so the positional fallback parser runs; its diagnostics must
    show header detection, matched column positions, computed boundaries,
    and sample-row assignment."""
    monkeypatch.setattr(settings, "debug_mode", True)
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/table-parsing")
    assert debug_resp.status_code == 200
    tables = debug_resp.json()["tables"]
    assert len(tables) == 1

    diag = tables[0]
    assert diag["find_tables"]["pages"][0]["tables_found"] == 0
    assert diag["positional_parser_ran"] is True

    # positional_parser is now per-page (a document's own boilerplate/
    # header block reprinted on later pages must not bleed into an
    # earlier page's table-end detection -- see native_pdf_extraction.py)
    # -- this fixture is single-page, so everything lives in pages[0].
    positional = diag["positional_parser"]
    page_positional = positional["pages"][0]
    assert page_positional["header_detected"] is True
    assert page_positional["header_row_index"] is not None
    assert "Item" in page_positional["header_row_text"]

    matched_field_names = {c["field_name"] for c in page_positional["matched_columns"]}
    assert "item_description" in matched_field_names
    assert "line_total" in matched_field_names
    # Each matched column carries its own text + coordinates, not just a name.
    item_col = next(c for c in page_positional["matched_columns"] if c["field_name"] == "item_description")
    assert item_col["text"] == "Item"
    assert isinstance(item_col["x0"], float)

    # The deliberately-unrecognized "Tax" header word shows up as unmatched,
    # not silently dropped from the diagnostic output.
    unmatched_texts = {w["text"] for w in page_positional["unmatched_header_words"]}
    assert "Tax" in unmatched_texts

    assert len(page_positional["column_boundaries"]) == len(matched_field_names)
    for boundary in page_positional["column_boundaries"]:
        assert {"field_name", "left", "right", "header_text"} <= boundary.keys()

    # Sample rows show the actual attempted mapping -- raw words AND what
    # got assigned where -- for the rows right after the header.
    assert len(page_positional["sample_rows"]) >= 1
    first_sample = page_positional["sample_rows"][0]
    assert "words" in first_sample and "assigned_cells" in first_sample
    assert first_sample["assigned_cells"].get("item_description")

    assert page_positional["line_items_produced"] == positional["line_items_produced"] == diag["final_line_item_count"] == 2
    assert page_positional["stopped_reason"] is not None


def test_debug_table_parsing_shows_no_header_detected_when_table_extraction_fails_completely(client, monkeypatch):
    """No line-item table at all -- header_detected must be explicitly
    False with a stated reason, not just an empty/missing diagnostic."""
    monkeypatch.setattr(settings, "debug_mode", True)
    invoice_no_items = dict(SAMPLE_INVOICE, items=[])
    pdf_bytes = build_invoice_pdf_bytes([invoice_no_items])
    resp = client.post(
        "/api/jobs", files={"file": ("x.pdf", pdf_bytes, "application/pdf")}, data={"confirmed_no_split": "true"}
    )
    job_id = resp.json()["job_id"]

    debug_resp = client.get(f"/api/jobs/{job_id}/debug/table-parsing")
    assert debug_resp.status_code == 200
    diag = debug_resp.json()["tables"][0]

    assert diag["find_tables"]["pages"][0]["tables_found"] == 0
    assert diag["positional_parser_ran"] is True
    positional = diag["positional_parser"]
    page_positional = positional["pages"][0]
    assert page_positional["header_detected"] is False
    assert page_positional["stopped_reason"]
    assert positional["line_items_produced"] == 0
