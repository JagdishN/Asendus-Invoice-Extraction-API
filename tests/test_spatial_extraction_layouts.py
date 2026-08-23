"""
Validates the spatial-extraction architecture (app/services/spatial_text.py
+ the coordinate-aware paths in app/services/native_pdf_extraction.py)
against TWO deliberately different invoice layouts -- not just one -- per
the explicit requirement that a fix validated against a single fixture
doesn't prove the architecture is actually generic.

Fixture 1 (build_invoice_pdf_bytes / build_pharma_invoice_pdf_bytes,
already covered by tests/test_native_pdf_extraction.py and
tests/test_pharma_invoice_extraction.py): same-row labels, "Bill To" /
"FOR <vendor>" wording, ruled-line tables (find_tables()-based).

Fixture 2 (build_alt_layout_invoice_pdf_bytes, this file): the OPPOSITE
shape on every axis that matters --
  - IRN/Ack values painted SEVERAL ROWS ABOVE their own labels (not
    same-row, not below) -- exercises the "search rows above" branch of
    the spatial label/value fallback specifically.
  - "BILL TO" as a standalone section-title row (name on the row below,
    not a same-row "Bill To: <name>" pair) -- exercises the
    section-marker-then-content-below buyer-name fallback.
  - A PLAIN-TEXT table (no ruled grid lines at all -- find_tables() finds
    nothing), with a reduced, reordered column set including a
    deliberately UNRECOGNIZED "Tax" column sitting between two recognized
    ones, and a free-goods-style row with several blank cells.
  - A "TERMS AND CONDITIONS" block with different wording than any other
    fixture, still containing the word "buyer" twice.

If a fix only worked on fixture 1, these tests are where that would show.
"""
from app.models.schemas import ConfidenceBand
from app.services.native_pdf_extraction import extract_invoice_group_fields
from tests.pdf_builders import ALT_SAMPLE_IRN, build_alt_layout_invoice_pdf_bytes


def test_irn_and_ack_found_via_spatial_fallback_when_values_are_above_labels():
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["invoice_number"] == ALT_SAMPLE_IRN
    assert header_fields["ack_number"] == "556677889900112"
    assert header_fields["invoice_date"] == "15/08/2026"
    # Spatial-fallback matches are never HIGH, regardless of how clean the
    # eventual match is -- a positional guess is graded differently from a
    # same-line label:value pair on principle, not just when the value
    # shape is ambiguous.
    assert header_field_confidences["invoice_number"] != ConfidenceBand.HIGH
    assert header_field_confidences["ack_number"] != ConfidenceBand.HIGH


def test_vendor_and_buyer_gstin_correctly_disambiguated_different_layout():
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    header_fields, _, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["party_name"] == "NORTHSTAR PHARMA DISTRIBUTORS"
    assert header_fields["party_gstin"] == "07NORTH1234A1Z5"  # vendor's, not buyer's
    assert header_fields["party_gstin"] != "27METRO5678B1Z2"  # the buyer/consignee's GSTIN


def test_buyer_name_found_below_a_standalone_section_title_row():
    """'BILL TO' here is its own section-title row with no same-row value
    at all (unlike fixture 1's same-row 'Bill To: <name>') -- buyer_name
    must still be found, via the section-header-then-content-below
    fallback, not just the same-row label search."""
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["buyer_name"] == "Metro Health Pharmacy"
    assert header_fields["buyer_address"] == "22 Lake View Road, Pune"
    assert header_field_confidences["buyer_name"] != ConfidenceBand.HIGH  # positional, not a label match


def test_buyer_name_not_corrupted_by_terms_and_conditions_boilerplate():
    """The false-positive this was originally fixed for: 'buyer' appearing
    in Terms & Conditions text must never override/corrupt the real
    buyer_name found in the BILL TO section -- this fixture's Terms
    wording ('the buyer must inspect goods...') is deliberately DIFFERENT
    from any other fixture's, to confirm the exclusion isn't keyed to one
    specific sentence."""
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    header_fields, _, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["buyer_name"] == "Metro Health Pharmacy"
    assert "inspect" not in (header_fields["buyer_name"] or "")
    assert "48 hours" not in (header_fields["buyer_name"] or "")


def test_positional_table_parser_handles_reordered_reduced_columns_no_ruled_lines():
    """No vector ruling lines at all in this fixture's table -- find_tables()
    finds nothing, so this only passes if the header-row-driven positional
    fallback parser actually ran and worked, on a column set (Item/HSN/
    Qty/Rate/Taxable/Tax/Total) that's a reduced, reordered set relative
    to the pharma fixture's columns."""
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 2

    priced = line_items[0]
    assert priced.item_description == "Vitamin C Effervescent"
    assert priced.quantity == 20.0
    assert priced.unit_rate == 45.0
    assert priced.taxable_value == 900.0
    assert priced.line_total == 945.0
    # Table cells are heuristic -- never HIGH, matching the existing
    # find_tables()-based path's own confidence convention.
    assert priced.field_confidences["taxable_value"] != ConfidenceBand.HIGH

    free_row = line_items[1]
    assert free_row.quantity == 2.0
    # The free-goods row's blank pricing/tax/total cells stayed genuinely
    # blank (None) -- no value guessed/carried over from the priced row
    # above it, and no format-specific "Sold/Free column" handling was
    # needed for this non-pharma layout to get this right.
    assert free_row.unit_rate is None
    assert free_row.taxable_value is None
    assert free_row.line_total is None


def test_unrecognized_middle_column_does_not_corrupt_its_neighbors():
    """The 'Tax' column has no mapped field at all (deliberately) and
    sits BETWEEN two recognized columns (Taxable, Total) -- its data must
    not leak into either neighbor. Regression test for a real bug found
    while building this fixture: the naive column-boundary midpoint
    between two recognized headers let an unrecognized column's data
    silently corrupt whichever neighbor its rendered text width happened
    to reach."""
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    priced = line_items[0]
    # Taxable=900.00, Tax=45.00, Total=945.00 in the source -- if the "Tax"
    # value leaked into either neighbor, one of these would come back
    # wrong (e.g. taxable_value corrupted to something unparseable, or
    # line_total picking up "45.00 945.00" and failing to parse).
    assert priced.taxable_value == 900.0
    assert priced.line_total == 945.0


def test_table_parser_stops_at_totals_block_not_swallowed_as_line_items():
    """Regression test for a real bug found while building this fixture:
    the totals rows below the table ('Taxable Total: 900.00', 'Tax Total:
    45.00', 'Grand Total: 945.00') used to leak through as three extra,
    entirely-empty line items, because a totals label word landing in a
    known column (e.g. 'Total:' in the line_total column) counted as
    'matched' even though nothing about the row actually parsed into
    anything usable."""
    pdf_bytes = build_alt_layout_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 2  # not 5
