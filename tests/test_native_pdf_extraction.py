from app.models.schemas import ConfidenceBand
from app.services.invoice_grouping import group_pages_into_invoices
from app.services.native_pdf_extraction import (
    INVOICE_TOTAL_LABELS,
    _extract_amount_field,
    _extract_invoice_number,
    _normalize_short_date,
    _split_description_and_pack,
    extract_invoice_group_fields,
    extract_page_invoice_numbers,
)
from tests.pdf_builders import SAMPLE_IRN, build_blank_pdf_bytes, build_invoice_pdf_bytes, build_pharma_invoice_pdf_bytes

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


# ---------------------------------------------------------------------------
# item_description -> pack splitting (client-confirmed: the packing
# reference after a product name's trailing " -<pack>" is its own column,
# not part of the description) -- see _split_description_and_pack.
# ---------------------------------------------------------------------------


def test_split_description_and_pack_no_space_before_pack_token():
    description, pack = _split_description_and_pack("Nefrosave Forte Tablets -15s")
    assert description == "Nefrosave Forte Tablets"
    assert pack == "15s"


def test_split_description_and_pack_space_before_pack_token():
    description, pack = _split_description_and_pack("K Mac B6 Active Liquid - 200ml")
    assert description == "K Mac B6 Active Liquid"
    assert pack == "200ml"


def test_split_description_and_pack_no_hyphen_leaves_description_unchanged():
    description, pack = _split_description_and_pack("Industrial Bearings Set")
    assert description == "Industrial Bearings Set"
    assert pack is None


def test_split_description_and_pack_hyphen_with_no_preceding_space_is_not_a_pack_split():
    # A mid-word compound name (no space before the hyphen) is never
    # mistaken for a pack reference.
    description, pack = _split_description_and_pack("Anti-Inflammatory Tablets")
    assert description == "Anti-Inflammatory Tablets"
    assert pack is None


def test_split_description_and_pack_splits_at_the_last_hyphen_when_there_are_several():
    description, pack = _split_description_and_pack("Multi - Vitamin Syrup - 200ml")
    assert description == "Multi - Vitamin Syrup"
    assert pack == "200ml"


def test_pack_split_applied_end_to_end_to_extracted_line_items():
    line_item = dict(
        sr=1, description="Paracetamol 500mg Tab -15s", hsn="3004", batch="B2201", expiry="12/2027",
        sold=100, free="", total_qty=100, mrp="15.00", ptr="10.50", rate_pts="",
        total_amt="1050.00", discount="21.00", taxable="1029.00",
        cgst_rate="6%", cgst_amt="61.74", sgst_rate="6%", sgst_amt="61.74",
        igst_rate="", igst_amt="",
    )
    pdf_bytes = build_pharma_invoice_pdf_bytes(line_items=[line_item])
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 1
    assert line_items[0].item_description == "Paracetamol 500mg Tab"
    assert line_items[0].pack == "15s"


# ---------------------------------------------------------------------------
# expiry_date/mfg_date short-date normalization (client-confirmed:
# "DD-MMM-YYYY", or "MMM-YYYY" when the source has no day at all).
# ---------------------------------------------------------------------------


def test_normalize_short_date_numeric_day_month_year():
    assert _normalize_short_date("01/09/2026") == "01-Sep-2026"
    assert _normalize_short_date("14-07-2026") == "14-Jul-2026"
    assert _normalize_short_date("5.3.2028") == "05-Mar-2028"


def test_normalize_short_date_numeric_month_year_only_has_no_day():
    assert _normalize_short_date("12/2027") == "Dec-2027"
    assert _normalize_short_date("03-28") == "Mar-2028"  # 2-digit year assumed 20xx


def test_normalize_short_date_month_name_year_only_has_no_day():
    assert _normalize_short_date("MAR-2028") == "Mar-2028"
    assert _normalize_short_date("Apr/2026") == "Apr-2026"


def test_normalize_short_date_day_month_name_year():
    assert _normalize_short_date("01-MAR-2028") == "01-Mar-2028"
    assert _normalize_short_date("14 Jul 2026") == "14-Jul-2026"


def test_normalize_short_date_full_month_name_day_year():
    assert _normalize_short_date("August 14, 2026") == "14-Aug-2026"


def test_normalize_short_date_unrecognized_shape_left_unchanged():
    assert _normalize_short_date("Q3 2026") == "Q3 2026"
    assert _normalize_short_date(None) is None
    assert _normalize_short_date("") == ""


def test_expiry_and_mfg_date_normalized_end_to_end_on_batch_detail_row_format():
    from tests.pdf_builders import build_batch_detail_row_invoice_pdf_bytes

    pdf_bytes = build_batch_detail_row_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])
    assert line_items[0].expiry_date == "Mar-2028"
    assert line_items[0].mfg_date == "Apr-2026"
