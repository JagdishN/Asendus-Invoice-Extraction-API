from app.models.schemas import ConfidenceBand, InvoiceLineItem
from app.services.invoice_grouping import group_pages_into_invoices
from app.services.native_pdf_extraction import (
    INVOICE_TOTAL_LABELS,
    _compute_pts_derived_quantities,
    _extract_amount_field,
    _extract_invoice_number,
    _map_table_header_columns,
    _merge_page_split_description_only_rows,
    _normalize_short_date,
    _split_description_and_pack,
    _split_description_and_pack_size_label,
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


def test_short_plain_numeric_invoice_number_is_not_rejected():
    # Regression check for a real bug: a real invoice's own plain numeric
    # invoice number ("369377796", 9 digits) was shorter than the
    # value-shape regex's old 10-character minimum, so the same-line
    # label:value match failed outright -- not just graded lower
    # confidence, but silently None -- and a coincidentally-10-character
    # nearby value (a DATE) got picked up by the spatial fallback instead.
    invoice = dict(SAMPLE_INVOICE, invoice_number="369377796")
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    results = extract_page_invoice_numbers(pdf_bytes)

    assert results[0].invoice_number == "369377796"
    assert results[0].confidence == ConfidenceBand.REVIEW


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
# "DD-MM-YYYY", or "01-MM-YYYY" when the source has no day at all).
# ---------------------------------------------------------------------------


def test_normalize_short_date_numeric_day_month_year():
    assert _normalize_short_date("01/09/2026") == "01-09-2026"
    assert _normalize_short_date("14-07-2026") == "14-07-2026"
    assert _normalize_short_date("5.3.2028") == "05-03-2028"


def test_normalize_short_date_numeric_month_year_defaults_day_to_01():
    assert _normalize_short_date("12/2027") == "01-12-2027"
    assert _normalize_short_date("03-28") == "01-03-2028"  # 2-digit year assumed 20xx


def test_normalize_short_date_month_name_year_defaults_day_to_01():
    assert _normalize_short_date("MAR-2028") == "01-03-2028"
    assert _normalize_short_date("Apr/2026") == "01-04-2026"


def test_normalize_short_date_day_month_name_year():
    assert _normalize_short_date("01-MAR-2028") == "01-03-2028"
    assert _normalize_short_date("14 Jul 2026") == "14-07-2026"


def test_normalize_short_date_full_month_name_day_year():
    assert _normalize_short_date("August 14, 2026") == "14-08-2026"


def test_normalize_short_date_unrecognized_shape_left_unchanged():
    assert _normalize_short_date("Q3 2026") == "Q3 2026"
    assert _normalize_short_date(None) is None
    assert _normalize_short_date("") == ""


def test_expiry_and_mfg_date_normalized_end_to_end_on_batch_detail_row_format():
    from tests.pdf_builders import build_batch_detail_row_invoice_pdf_bytes

    pdf_bytes = build_batch_detail_row_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])
    assert line_items[0].expiry_date == "01-03-2028"
    assert line_items[0].mfg_date == "01-04-2026"


# ---------------------------------------------------------------------------
# PTS-derived Original Quantity / Free Quantity (client-confirmed formula:
# original = round(taxable_value / rate_pts), free = total_quantity -
# original -- "dynamic" columns, populated only when a bill actually
# carries a PTS rate). NOTE: an intermediate version of this used `ptr`
# (Price To Retailer, a DIFFERENT column) as the divisor -- verified
# against a real invoice with an already-known correct answer (explicit
# Sold/Free columns) that PTS reconciles and PTR does not, so this was
# corrected back to `rate_pts`.
# ---------------------------------------------------------------------------


def _line_item(**overrides) -> InvoiceLineItem:
    defaults = dict(line_number=1, item_description="Test Product")
    defaults.update(overrides)
    return InvoiceLineItem(**defaults)


def test_pts_derived_quantities_confirmed_example():
    # quantity=320, taxable_value=20044.8, rate_pts=69.6 -> original=288,
    # free=32 (69.6 * 288 == 20044.8 exactly).
    item = _line_item(quantity=320.0, taxable_value=20044.8, rate_pts=69.6)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 288.0
    assert free == 32.0


def test_pts_derived_quantities_rounds_to_nearest_whole_unit():
    # 2400 / 69.6 = 34.48... -> rounds to 34, free = 320 - 34 = 286.
    item = _line_item(quantity=320.0, taxable_value=2400.0, rate_pts=69.6)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 34.0
    assert free == 286.0


def test_pts_derived_quantities_none_when_rate_pts_missing():
    item = _line_item(quantity=320.0, taxable_value=20044.8, rate_pts=None)
    assert _compute_pts_derived_quantities(item) == (None, None)


def test_pts_derived_quantities_none_when_rate_pts_is_zero():
    item = _line_item(quantity=320.0, taxable_value=20044.8, rate_pts=0.0)
    assert _compute_pts_derived_quantities(item) == (None, None)


def test_pts_derived_quantities_none_when_quantity_or_taxable_value_missing():
    assert _compute_pts_derived_quantities(_line_item(taxable_value=20044.8, rate_pts=69.6)) == (None, None)
    assert _compute_pts_derived_quantities(_line_item(quantity=320.0, rate_pts=69.6)) == (None, None)


def test_pts_derived_quantities_falls_back_to_quantity_sold_plus_free_when_no_bundled_quantity():
    # Real invoice format: quantity/quantity_total both None, only
    # quantity_sold/quantity_free are populated (the Sold/Free-as-separate-
    # rows format) -- verified real numbers: Sold=50, taxable_value=
    # 14811.50, rate_pts=296.23 -> original=round(14811.50/296.23)=50,
    # matching Sold exactly; free = (50+0) - 50 = 0.
    item = _line_item(quantity_sold=50.0, quantity_free=0.0, taxable_value=14811.50, rate_pts=296.23)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 50.0
    assert free == 0.0


def test_pts_derived_quantities_falls_back_to_quantity_total_when_bundled_quantity_missing():
    item = _line_item(quantity_total=320.0, taxable_value=20044.8, rate_pts=69.6)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 288.0
    assert free == 32.0


def test_pts_derived_quantities_computed_end_to_end_via_extract_invoice_group_fields():
    from tests.pdf_builders import build_ambiguous_column_headers_pdf_bytes

    # This fixture's single line item: Qty=10, Taxable=760.00, Pts=70.00 ->
    # original = round(760/70) = 11, free = 10 - 11 = -1 (not clamped --
    # this fixture's own numbers just don't happen to reconcile, which is
    # exactly the "surface it, don't hide it" behavior being tested).
    pdf_bytes = build_ambiguous_column_headers_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert line_items[0].rate_pts == 70.0
    assert line_items[0].pts_original_quantity == 11.0
    assert line_items[0].pts_free_quantity == -1.0


def test_pts_derived_quantities_computed_end_to_end_for_sold_free_split_format():
    # Mirrors the real invoice's Sold/Free-as-separate-rows layout via the
    # pharma builder: Sold row has sold=50, taxable=14811.50, rate_pts=
    # 296.23 -> original=50, free=0 (matching the real invoice exactly).
    line_item = dict(
        sr=1, description="Nefrosave Forte Tablets -15s", hsn="30049099", batch="P0527", expiry="05/2028",
        sold=50, free="", total_qty=50, mrp="432.00", ptr="329.14", rate_pts="296.23",
        total_amt="14811.50", discount="", taxable="14811.50",
        cgst_rate="2.50%", cgst_amt="370.29", sgst_rate="2.50%", sgst_amt="370.29",
        igst_rate="", igst_amt="",
    )
    pdf_bytes = build_pharma_invoice_pdf_bytes(line_items=[line_item])
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert line_items[0].quantity_sold == 50.0
    assert line_items[0].rate_pts == 296.23
    assert line_items[0].pts_original_quantity == 50.0
    assert line_items[0].pts_free_quantity == 0.0


def test_pts_derived_quantities_falls_back_to_unit_rate_when_no_pts_column():
    # Client-confirmed fallback: a bill with no PTS column at all can use
    # any Disc Price/Rate/Unit Price/Unit Rate column instead (all
    # captured under the generic unit_rate field already).
    item = _line_item(quantity=100.0, taxable_value=5000.0, unit_rate=50.0)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 100.0
    assert free == 0.0


def test_pts_derived_quantities_prefers_rate_pts_over_unit_rate_when_both_present():
    item = _line_item(quantity=320.0, taxable_value=20044.8, rate_pts=69.6, unit_rate=999.0)
    original, free = _compute_pts_derived_quantities(item)
    assert original == 288.0  # uses rate_pts (69.6), not unit_rate (999.0)
    assert free == 32.0


# ---------------------------------------------------------------------------
# "Pack Size:" label embedded in the description cell itself (client-
# confirmed real invoice: Vishal Agencies, Hyderabad -- e.g. "CUDO FORTE\n
# Pack Size: 1*10 CAPSULE" as ONE cell, collapsed to "CUDO FORTE Pack
# Size: 1*10 CAPSULE" before this runs).
# ---------------------------------------------------------------------------


def test_split_description_and_pack_size_label_examples():
    assert _split_description_and_pack_size_label("CUDO FORTE Pack Size: 1*10 CAPSULE") == (
        "CUDO FORTE", "1*10 CAPSULE",
    )
    assert _split_description_and_pack_size_label("EIDO INJ. Pack Size: 1 AMP") == ("EIDO INJ.", "1 AMP")


def test_split_description_and_pack_size_label_no_label_leaves_description_unchanged():
    assert _split_description_and_pack_size_label("Plain Product Name") == ("Plain Product Name", None)


def test_pack_size_label_split_end_to_end_via_extract_invoice_group_fields():
    # Exercises the split function's own wiring into extract_invoice_
    # group_fields's post-processing loop (the pharma test builder's own
    # description column is a single-line cell, so this feeds it the
    # already-collapsed shape a real multi-line PDF cell would produce).
    # Kept short to fit this builder's fixed description column width
    # (a longer string overflows into the neighboring column and gets
    # truncated -- a test-fixture rendering artifact, not a real bug; see
    # the pure _split_description_and_pack_size_label tests above and the
    # real-invoice cross-check for full-length verification).
    line_item = dict(
        sr=1, description="CUDO Pack Size: 1x10", hsn="21069099", batch="DN126287",
        expiry="02/2028", sold=21, free="", total_qty=21, mrp="1938.04", ptr="1476.60", rate_pts="1328.94",
        total_amt="27907.74", discount="", taxable="27907.74",
        cgst_rate="2.50%", cgst_amt="697.69", sgst_rate="2.50%", sgst_amt="697.69",
        igst_rate="", igst_amt="",
    )
    pdf_bytes = build_pharma_invoice_pdf_bytes(line_items=[line_item])
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert line_items[0].item_description == "CUDO"
    assert line_items[0].pack == "1x10"


def test_mfg_name_header_recognized_as_manufacturer():
    # Confirmed real invoice (Vishal Agencies, Hyderabad) spells this
    # column out fully as "Mfg Name" rather than abbreviating.
    mapping = _map_table_header_columns(["Description of Goods", "Mfg Name", "Expiry Date"])
    assert mapping[1] == "manufacturer"


# ---------------------------------------------------------------------------
# Cross-page product-name split (confirmed real invoice: Vishal Agencies,
# Hyderabad, a 7-page invoice where one product's name landed as the very
# last row of a page with nothing else on it, and the rest of that same
# row's data printed as the first row of the next page with no name at
# all -- each page parsed independently, so this produced two broken line
# items instead of one correct one).
# ---------------------------------------------------------------------------


def test_merge_page_split_description_only_row_into_following_real_row():
    orphan = _line_item(line_number=1, item_description="EMPASHIELD-10")
    real = _line_item(
        line_number=2, item_description="Pack Size: 1*10 TABLET", batch_number="EMV260394A",
        taxable_value=636.40, quantity=10.0,
    )
    merged = _merge_page_split_description_only_rows([orphan, real])

    assert len(merged) == 1
    assert merged[0].item_description == "EMPASHIELD-10 Pack Size: 1*10 TABLET"
    assert merged[0].batch_number == "EMV260394A"
    assert merged[0].taxable_value == 636.40
    assert merged[0].line_number == 1


def test_merge_leaves_two_genuine_consecutive_items_untouched():
    first = _line_item(line_number=1, item_description="Item A", taxable_value=100.0, quantity=1.0)
    second = _line_item(line_number=2, item_description="Item B", taxable_value=200.0, quantity=2.0)
    merged = _merge_page_split_description_only_rows([first, second])

    assert len(merged) == 2
    assert [item.item_description for item in merged] == ["Item A", "Item B"]


def test_merge_does_not_touch_a_description_only_row_with_no_following_real_row():
    # A genuinely blank/sparse trailing row (not a page-split artifact)
    # must be left alone rather than merged into nothing.
    orphan = _line_item(line_number=1, item_description="Some Label")
    merged = _merge_page_split_description_only_rows([orphan])

    assert len(merged) == 1
    assert merged[0].item_description == "Some Label"


def test_page_split_product_name_merged_end_to_end_on_real_invoice_shape():
    # Mirrors the real 7-page invoice's exact scenario via extract_
    # invoice_group_fields's own merge step, using hand-built line items
    # (find_tables()/positional per-page parsing is what actually produces
    # this split in production -- see _merge_page_split_description_only_rows'
    # own docstring for the real page-break mechanism).
    from app.services.native_pdf_extraction import _merge_page_split_description_only_rows as merge_fn

    orphan = _line_item(line_number=74, item_description="EMPASHIELD-10", source_page=5)
    real = _line_item(
        line_number=75, item_description="Pack Size: 1*10 TABLET", source_page=6,
        batch_number="EMV260394A", taxable_value=636.40, quantity=10.0,
    )
    following = _line_item(
        line_number=76, item_description="EMPASHIELD-S 25/100", source_page=6,
        taxable_value=3837.90, quantity=30.0,
    )
    merged = merge_fn([orphan, real, following])

    assert len(merged) == 2
    assert merged[0].item_description == "EMPASHIELD-10 Pack Size: 1*10 TABLET"
    assert merged[0].line_number == 1
    assert merged[1].item_description == "EMPASHIELD-S 25/100"
    assert merged[1].line_number == 2
