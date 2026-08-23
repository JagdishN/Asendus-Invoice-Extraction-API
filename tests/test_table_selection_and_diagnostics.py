"""
Tests for the find_tables()-first line-item extraction rework: multi-table
selection (by header keyword-match count, not "first found"/"largest"),
wrapped/multi-line cell normalization, and brand-subtotal-row filtering.

Diagnostic evidence from a real failing invoice showed find_tables()
detecting 2 tables per page but the pipeline using 0 of them -- see
native_pdf_extraction.py's module docstring ("find_tables()-first,
multi-table selection"). The real invoice's own raw text/reference values
were never actually provided (asked for twice), so
build_multi_table_line_items_pdf_bytes reproduces the SHAPE of that
evidence (multiple ruled tables per page, one of which is a decoy; a
wrapped manufacturer-name cell; interleaved "Total for <brand>" subtotal
rows) rather than the real invoice's own content.
"""
from app.services.native_pdf_extraction import extract_invoice_group_fields
from tests.pdf_builders import (
    MULTI_TABLE_AURUS_SUBTOTAL,
    MULTI_TABLE_ZEN_SUBTOTAL,
    build_ambiguous_column_headers_pdf_bytes,
    build_multi_table_line_items_pdf_bytes,
    build_pharma_invoice_pdf_bytes,
)


def _extract_with_diagnostics(pdf_bytes: bytes):
    captured: list[dict] = []
    header_fields, header_field_confidences, line_items = extract_invoice_group_fields(
        pdf_bytes, [1], on_table_diagnostics=captured.append
    )
    return header_fields, header_field_confidences, line_items, captured[0]


def test_best_table_selected_over_a_decoy_table_with_fewer_matched_columns():
    # The decoy table (drawn FIRST on the page, just "Item"/"Remarks") has
    # exactly one header cell that satisfies the item_description check --
    # enough to be "usable" at all, but with far fewer matched columns
    # than the real 11-column line-items table drawn second. A naive
    # "first qualifying table" or "first table found" selection would
    # wrongly pick the decoy.
    pdf_bytes = build_multi_table_line_items_pdf_bytes()
    _, _, line_items, diag = _extract_with_diagnostics(pdf_bytes)

    page_diag = diag["find_tables"]["pages"][0]
    assert page_diag["tables_found"] == 2
    assert page_diag["tables_used"] == 1
    assert page_diag["selected_table_index"] == 1  # the real table, not the decoy (index 0)
    assert "11 matched header columns" in page_diag["selection_reason"]
    assert "highest among 2 candidate(s)" in page_diag["selection_reason"]

    candidates = {c["table_index"]: c for c in page_diag["candidates"]}
    assert candidates[0]["matched_column_count"] == 1  # the decoy's lone "Item" column
    assert candidates[0]["has_item_description"] is True  # usable in isolation, just outscored
    assert candidates[1]["matched_column_count"] == 11

    # Only the real table's rows became line items -- the decoy's "Note"
    # row must not appear anywhere.
    assert not any("Note" in (item.item_description or "") for item in line_items)
    assert diag["positional_parser_ran"] is False  # find_tables() alone was sufficient


def test_wrapped_multiline_cell_content_is_normalized_to_a_single_space():
    pdf_bytes = build_multi_table_line_items_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    wrapped_item = next(item for item in line_items if item.hsn_sac == "30049099")
    assert wrapped_item.item_description == "AURUS HEALTHCARE PVT LTD IMMUNITY BOOSTER SYRUP"
    assert "\n" not in wrapped_item.item_description


def test_brand_subtotal_rows_excluded_but_checksum_against_real_rows_matches():
    pdf_bytes = build_multi_table_line_items_pdf_bytes()
    _, _, line_items, diag = _extract_with_diagnostics(pdf_bytes)

    # Exactly the 3 real product rows -- neither "Total for AURUS" nor
    # "Total for ZEN" made it into the line items.
    assert len(line_items) == 3
    descriptions = [item.item_description for item in line_items]
    assert not any(d.lower().startswith("total for") for d in descriptions)

    # Skipped rows are captured, not just counted -- a caller can check a
    # brand group's real line items against the row's OWN stated total
    # without needing a separately-hardcoded expectation.
    subtotal_rows = diag["find_tables"]["pages"][0]["subtotal_rows"]
    assert len(subtotal_rows) == 2
    subtotals_by_brand = {row["item_description"]: row["taxable_value"] for row in subtotal_rows}
    assert subtotals_by_brand["Total for AURUS"] == MULTI_TABLE_AURUS_SUBTOTAL
    assert subtotals_by_brand["Total for ZEN"] == MULTI_TABLE_ZEN_SUBTOTAL

    aurus_sum = sum(item.taxable_value for item in line_items if "AURUS" in item.item_description)
    zen_sum = sum(item.taxable_value for item in line_items if "ZEN" in item.item_description)
    assert aurus_sum == subtotals_by_brand["Total for AURUS"]
    assert zen_sum == subtotals_by_brand["Total for ZEN"]


def test_sr_no_column_recognized_for_scoring_but_not_written_onto_line_items():
    # "Sr No" is a real, recognizable column (counts toward the winning
    # table's matched_column_count) but has no corresponding
    # InvoiceLineItem field -- it must not leak into field_confidences or
    # raise trying to construct the model.
    pdf_bytes = build_multi_table_line_items_pdf_bytes()
    _, _, line_items, diag = _extract_with_diagnostics(pdf_bytes)

    selected = diag["find_tables"]["pages"][0]["candidates"][1]
    assert selected["matched_columns"]["sr_no"] == "Sr No"

    for item in line_items:
        assert "sr_no" not in item.field_confidences
        assert not hasattr(item, "sr_no")


def test_ambiguous_real_world_column_headers_map_correctly():
    # Reproduces the specific header vocabulary gaps found while reviewing
    # real diagnostic evidence: a bare "PTS" column (no "Rate"/"Points"
    # wording), a bare "VALUE" line-amount column (not "Total"), a
    # price-revision document with BOTH "Old Mrp"/"New Mrp" columns, and
    # BOTH "Disc %"/"Disc Amt" columns on the same header row.
    pdf_bytes = build_ambiguous_column_headers_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 1
    item = line_items[0]

    # The CURRENT mrp wins -- the superseded "Old Mrp" value must not leak
    # into the mrp field or anywhere else on the item.
    assert item.mrp == 120.0
    assert not hasattr(item, "mrp_old")

    assert item.ptr == 80.0
    assert item.rate_pts == 70.0  # "Pts" -- Price To Stockist, no "rate"/"points" wording
    assert item.line_total == 800.0  # "Value" column, not "Total"

    # Discount rate and amount land in their own separate fields, not
    # collapsed onto one.
    assert item.discount_rate == 5.0
    assert item.discount_amount == 40.0

    assert item.taxable_value == 760.0
    assert item.cgst_amount == 68.4
    assert item.sgst_amount == 68.4


def test_pharma_fixture_still_selects_the_correct_table_over_its_tax_bracket_table():
    # Regression check ("Vijay Sai" fixture -- build_pharma_invoice_pdf_bytes's
    # default vendor_name): this fixture already has 2 ruled tables per
    # page (the line-items table + a separate tax-rate-bracket summary
    # table -- see build_pharma_invoice_pdf_bytes). The old code happened
    # to get this right by coincidence (only one candidate had an
    # item_description column at all); the new explicit
    # keyword-match-count selection must still get it right, not
    # regress a case that already worked.
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    _, _, line_items, diag = _extract_with_diagnostics(pdf_bytes)

    assert len(line_items) == 4  # 2 products x (Sold row + Free row)
    page_diag = diag["find_tables"]["pages"][0]
    assert page_diag["tables_found"] == 2
    assert page_diag["selected_table_index"] == 0
    candidates = {c["table_index"]: c for c in page_diag["candidates"]}
    assert candidates[0]["has_item_description"] is True
    assert candidates[1]["has_item_description"] is False  # the tax-bracket table
    assert diag["positional_parser_ran"] is False
