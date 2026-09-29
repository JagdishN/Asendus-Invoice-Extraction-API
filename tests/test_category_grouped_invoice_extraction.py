"""
Tests for the "category-grouped, whitespace-aligned" line-item table
format (confirmed real invoice: Indoco Remedies Ltd). Products are grouped
under bare brand-category label rows ("PHARMA", "SPADE", "SPERA", "NXGEN")
with no data of their own -- a "tree" of categories each containing line
items, per the client's own description -- and the table itself has no
ruled lines at all (find_tables() finds nothing), so column boundaries are
inferred purely from word X-positions.

Real bugs this fixes:
  - Every item's description had its first word cut off (e.g. "CITAL
    SUGAR FREE 100ML" -> "SUGAR FREE 100ML") -- the real invoice's product-
    name text starts visually LEFT of its own "PRODUCT" header word, under
    a "CASE NO" column that's always blank in the real data, and the
    normal column-boundary math treated that as a real boundary to cut at.
  - Zero line items were extracted AT ALL initially -- the header row uses
    bare "PRODUCT" (not "description"/"item"/"particular") and "M.R.P."
    (with periods), neither of which any existing keyword matched.
  - Category label rows ("PHARMA" etc.) and a trailing manufacturer-plant-
    code legend block were emitted as fake line items.
"""
from app.services.native_pdf_extraction import extract_invoice_group_fields
from tests.pdf_builders import build_category_grouped_pharma_invoice_pdf_bytes


def test_category_label_rows_and_footer_legend_are_excluded():
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    descriptions = [item.item_description for item in line_items]
    assert "PHARMA" not in descriptions
    assert "SPADE" not in descriptions
    assert not any("Manufacturing Address" in d for d in descriptions)
    assert not any("REGD OFFICE" in d for d in descriptions)
    assert len(line_items) == 4  # exactly the 4 real products, nothing else


def test_item_description_is_not_truncated_despite_a_blank_left_column():
    # Regression check for the real bug: product name text starting left
    # of its own "PRODUCT" header word (under an always-blank "CASE NO"
    # column) must not have its first word silently dropped.
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    descriptions = {item.item_description for item in line_items}
    assert "CITAL SUGAR FREE 100ML" in descriptions
    assert "CLOBEN G CREAM 15G" in descriptions
    assert "CYCLOPAM TAB 15X3X10S" in descriptions
    # None of these should appear WITHOUT their first word.
    assert "SUGAR FREE 100ML" not in descriptions
    assert "G CREAM 15G" not in descriptions
    assert "TAB 15X3X10S" not in descriptions


def test_bare_product_and_perioded_mrp_headers_are_recognized():
    # Regression check: this header uses bare "PRODUCT" (not "description"/
    # "item"/"particular") and "M.R.P." (with periods) -- without these
    # keywords, NO column mapped to item_description at all and every row
    # was silently skipped, losing the entire table.
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) > 0
    first = next(item for item in line_items if item.item_description == "CITAL SUGAR FREE 100ML")
    assert first.mrp == 131.00
    assert first.hsn_sac == "30044090"
    assert first.expiry_date == "01-06-2029"
    assert first.batch_number == "26050582"
    assert first.quantity == 160.0
    assert first.ptr == 99.81
    assert first.rate_pts == 89.83
    assert first.taxable_value == 12936.00


def test_all_real_items_fully_extracted():
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 4
    total_taxable = sum(item.taxable_value or 0 for item in line_items)
    assert round(total_taxable, 2) == 12936.00 + 2228.40 + 15430.50 + 10461.00


def test_pack_column_and_manufacturer_column_are_extracted():
    # Real bug: a genuine "Pack" column (e.g. "100 ML") and a "MFRS"
    # (manufacturer) column were both being dropped entirely -- pack had
    # no keyword mapping at all (only the hyphen-suffix split worked), and
    # manufacturer was recognized only for table-boundary detection, never
    # actually stored on the line item.
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    by_description = {item.item_description: item for item in line_items}
    assert by_description["CITAL SUGAR FREE 100ML"].pack == "100 ML"
    assert by_description["CITAL SUGAR FREE 100ML"].manufacturer is None
    assert by_description["CLOBEN G CREAM 15G"].pack == "15 GMS"
    assert by_description["CYCLOPAM TAB 15X3X10S"].pack == "10S"
    assert by_description["CYCLOPAM TAB 15X3X10S"].manufacturer == "WALU"


def test_manufacturer_only_continuation_row_is_not_mistaken_for_a_line_item():
    # Regression check: promoting `manufacturer` to a real, storable field
    # must not break the existing "wrapped continuation line" protection.
    # The fixture includes a bare "LIMITED" row (a wrapped continuation of
    # the manufacturer cell above it, with no description/qty/mrp of its
    # own) right after CYCLOPAM TAB and right before OTOREX EAR DROPS --
    # it must not become a fake extra line item, AND must not be mistaken
    # for the end of the table (the real item after it must still be
    # found).
    pdf_bytes = build_category_grouped_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    descriptions = [item.item_description for item in line_items]
    assert "LIMITED" not in descriptions
    assert all(item.item_description for item in line_items)
    assert "OTOREX EAR DROPS 10ML" in descriptions
