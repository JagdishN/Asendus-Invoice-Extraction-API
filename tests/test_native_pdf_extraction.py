from app.models.schemas import ConfidenceBand
from app.services.invoice_grouping import group_pages_into_invoices
from app.services.native_pdf_extraction import (
    INVOICE_TOTAL_LABELS,
    _extract_amount_field,
    _extract_invoice_number,
    extract_invoice_group_fields,
    extract_page_invoice_numbers,
)
from tests.pdf_builders import SAMPLE_IRN, build_blank_pdf_bytes, build_invoice_pdf_bytes

SAMPLE_INVOICE = dict(
    invoice_number="INV/2026/001",
    invoice_date="14/07/2026",
    party_name="Sundar Traders Pvt Ltd",
    gstin="27ABCDE1234F1Z5",
    items=[
        ("Industrial Bearings Set", "8482", "10", "850.00", "8500.00", "765.00", "765.00", "10030.00"),
    ],
    subtotal="8500.00",
    total="10030.00",
)


def test_finds_labeled_invoice_number_but_review_confidence_when_not_irn_shaped():
    # invoice_number is now specifically the IRN (64-hex-char GST string) --
    # a vendor-style number like "INV/2026/001" is still found via the
    # label, but graded REVIEW rather than HIGH since it doesn't match the
    # IRN shape.
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    results = extract_page_invoice_numbers(pdf_bytes)

    assert len(results) == 1
    assert results[0].invoice_number == "INV/2026/001"
    assert results[0].confidence == ConfidenceBand.REVIEW
    assert results[0].has_text_layer is True


def test_valid_irn_shaped_invoice_number_gets_high_confidence():
    invoice = dict(SAMPLE_INVOICE, invoice_number=SAMPLE_IRN)
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    results = extract_page_invoice_numbers(pdf_bytes)

    assert results[0].invoice_number == SAMPLE_IRN
    assert results[0].confidence == ConfidenceBand.HIGH


def test_blank_page_has_no_text_layer_and_no_invoice_number():
    pdf_bytes = build_blank_pdf_bytes(page_count=1)
    results = extract_page_invoice_numbers(pdf_bytes)

    assert results[0].has_text_layer is False
    assert results[0].invoice_number is None
    assert results[0].confidence == ConfidenceBand.NOT_FOUND


def test_page_numbers_param_restricts_scan_to_given_pages():
    invoices = [
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/001"),
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/002"),
    ]
    pdf_bytes = build_invoice_pdf_bytes(invoices)
    results = extract_page_invoice_numbers(pdf_bytes, page_numbers=[2])

    assert len(results) == 1
    assert results[0].page_number == 2
    assert results[0].invoice_number == "INV/2026/002"


def test_multi_page_single_invoice_groups_into_one():
    invoice = dict(
        SAMPLE_INVOICE,
        pages=2,
        continuation_items=[
            ("Steel Bracket 20cm", "7326", "8", "120.00", "960.00", "86.40", "86.40", "1132.80"),
        ],
    )
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    page_results = extract_page_invoice_numbers(pdf_bytes)
    groups = group_pages_into_invoices(page_results)

    assert len(groups) == 1
    assert groups[0].source_page_list == [1, 2]
    assert groups[0].invoice_number == "INV/2026/001"


def test_multi_invoice_document_groups_separately():
    invoices = [
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/002"),
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/003"),
        dict(SAMPLE_INVOICE, invoice_number="INV/2026/004"),
    ]
    pdf_bytes = build_invoice_pdf_bytes(invoices)
    page_results = extract_page_invoice_numbers(pdf_bytes)
    groups = group_pages_into_invoices(page_results)

    assert len(groups) == 3
    assert [g.invoice_number for g in groups] == ["INV/2026/002", "INV/2026/003", "INV/2026/004"]


def test_extract_invoice_group_fields_reads_header_and_line_items():
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    header_fields, header_field_confidences, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["invoice_number"] == "INV/2026/001"
    assert header_fields["invoice_date"] == "14/07/2026"
    # party_name/party_gstin represent the VENDOR (found via the "For
    # <company>" signature line the builder draws), NOT the "Bill To"
    # value -- that now lands in buyer_name instead.
    assert header_fields["party_name"] == "Test Vendor Co"
    assert header_fields["buyer_name"] == "Sundar Traders Pvt Ltd"
    # party_gstin must be the VENDOR's GSTIN (drawn near the "For <vendor>"
    # signature) -- NOT "27ABCDE1234F1Z5", which is the BUYER's GSTIN drawn
    # right under "Bill To" a few lines above it. Asserting the vendor's
    # own distinct value is what actually proves the two are being told
    # apart, not just that "a GSTIN" was found somewhere.
    assert header_fields["party_gstin"] == "29VENDR5678C1Z9"
    assert header_fields["subtotal_taxable"] == 8500.0
    assert header_fields["invoice_total"] == 10030.0
    assert header_field_confidences["invoice_number"] == ConfidenceBand.REVIEW  # not IRN-shaped
    assert header_field_confidences["party_name"] == ConfidenceBand.HIGH
    assert header_field_confidences["party_gstin"] == ConfidenceBand.HIGH

    assert len(line_items) == 1
    item = line_items[0]
    assert item.item_description == "Industrial Bearings Set"
    assert item.hsn_sac == "8482"
    assert item.quantity == 10.0
    assert item.unit_rate == 850.0
    assert item.taxable_value == 8500.0
    assert item.cgst_amount == 765.0
    assert item.sgst_amount == 765.0
    assert item.source_page == 1
    # Table-derived fields are heuristic, never HIGH.
    assert item.field_confidences["taxable_value"] == ConfidenceBand.REVIEW


def test_extract_amount_field_is_not_found_when_label_absent():
    value, confidence = _extract_amount_field(
        "Some unrelated page text with no totals section at all.", INVOICE_TOTAL_LABELS
    )
    assert value is None
    assert confidence == ConfidenceBand.NOT_FOUND


def test_extract_invoice_number_is_not_found_when_no_label_present():
    value, confidence = _extract_invoice_number("This page has no recognizable invoice label at all.")
    assert value is None
    assert confidence == ConfidenceBand.NOT_FOUND


def test_table_without_ruling_lines_returns_empty_line_items_not_a_guess():
    # items=[] means no bordered table is drawn on the page at all.
    invoice = dict(SAMPLE_INVOICE, items=[])
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert line_items == []
