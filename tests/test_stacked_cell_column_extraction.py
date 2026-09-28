"""
Tests for the "stacked-cell column" line-item table format (confirmed real
invoice: Mankind Pharma Ltd's "Discovery"/"Nobelis" templates). Several
ruled-table columns hold TWO distinct values stacked in one grid cell via
an embedded newline (e.g. every "Mfg Date/Exp Date" cell is literally
"<mfg date>\n<exp date>") -- see native_pdf_extraction.py's
_map_combined_header_columns and _find_best_internal_header_row.

Real bug this fixes: expiry_date came out as the garbled, unparseable
"FEB-25 JAN-27" (both dates squashed into one string by the generic
column-collapsing logic), instead of either date alone -- which is what
the client actually reported ("date is not in DD-MMM-YYYY, especially
year").
"""
from app.services.native_pdf_extraction import (
    _find_best_internal_header_row,
    _map_combined_header_columns,
    extract_invoice_group_fields,
)
from tests.pdf_builders import build_stacked_cell_columns_invoice_pdf_bytes


def test_map_combined_header_columns_splits_known_real_pairs():
    header_row = [
        "Sr.No.",
        "Material\nHSN Code",
        "Material Description",
        "Pack",
        "Mfg Name/\nBatch",
        "Mfg Date/\nExp Date",
        "MRP",
        "P.T.R",
        "P.T.S",
        "QTY",
        "Amount",
        "Disc.Amt/\nDisc.%",
        "Amount/\nCGST%",
        "Amount/\nSGST%",
        "Amount/\nIGST%",
        "Net Amount",
    ]
    combined = _map_combined_header_columns(header_row)

    assert combined[1] == (None, "hsn_sac")
    assert combined[4] == (None, "batch_number")
    assert combined[5] == ("mfg_date", "expiry_date")
    assert combined[11] == ("discount_amount", "discount_rate")
    assert combined[12] == ("cgst_amount", "cgst_rate")
    assert combined[13] == ("sgst_amount", "sgst_rate")
    assert combined[14] == ("igst_amount", "igst_rate")
    # Single-line headers never get treated as combined columns, even ones
    # that happen to share a keyword with a combined pair.
    assert 0 not in combined
    assert 6 not in combined  # "MRP" -- no embedded newline at all


def test_map_combined_header_columns_ignores_multiline_headers_with_no_known_pair():
    # "Mfg date/use Before" wraps across 3 lines purely for column width --
    # not two distinct stacked VALUES, so it must not be mistaken for one.
    header_row = ["Sr.No.", "Mfg date/\nuse\nBefore"]
    assert _map_combined_header_columns(header_row) == {}


def test_find_best_internal_header_row_skips_a_leading_letterhead_block():
    rows = [
        ["MANKIND PHARMA LTD\nPLOT NO A6/6...", None, None, "Invoice No : 369377796\nInv. Date : 12.03.2025"],
        [None, None, None, None],
        ["Sr.No.", "Material\nHSN Code", "Material Description", "Mfg Date/\nExp Date"],
        ["1", "50005589\n30049099", "DYNADUO-10 TABLETS", "FEB-25\nJAN-27"],
    ]
    assert _find_best_internal_header_row(rows) == 2


def test_find_best_internal_header_row_returns_none_when_nothing_clears_the_threshold():
    rows = [["just some text", None], ["more text", "1234"]]
    assert _find_best_internal_header_row(rows) is None


def test_stacked_cell_columns_extracted_correctly_end_to_end():
    pdf_bytes = build_stacked_cell_columns_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 2

    first = line_items[0]
    assert first.item_description == "DYNADUO-10 TABLETS"
    assert first.pack == "10 TABS"
    assert first.hsn_sac == "30049099"
    assert first.batch_number == "GB2Y003"
    # Raw "FEB-25"/"JAN-27" normalize to short-date with day defaulted to 01.
    assert first.mfg_date == "01-Feb-2025"
    assert first.expiry_date == "01-Jan-2027"
    assert first.mrp == 79.90
    assert first.ptr == 57.07  # "P.T.R" (with periods) must still match
    assert first.rate_pts == 51.36  # "P.T.S" (with periods) must still match
    assert first.quantity == 24.0
    assert first.discount_amount == 0.0
    assert first.discount_rate == 0.0
    assert first.cgst_amount == 73.96
    assert first.cgst_rate == 6.0
    assert first.sgst_amount == 73.96
    assert first.sgst_rate == 6.0
    # Bare "Amount"/"Net Amount" headers -- exact-match, not substring, so
    # "Net Amount" (which contains "amount") doesn't get stolen by a
    # generic "amount" keyword meant for taxable_value.
    assert first.taxable_value == 1232.64
    assert first.line_total == 1380.56

    second = line_items[1]
    assert second.item_description == "DYNADUO-25 TABELTS"
    assert second.hsn_sac == "30049099"
    assert second.batch_number == "GC2Y002"
    assert second.mfg_date == "01-Feb-2025"
    assert second.expiry_date == "01-Jan-2027"


def test_stacked_cell_columns_do_not_leak_into_each_other():
    # Regression check for the real bug: neither combined field should
    # ever contain the OTHER field's value (the pre-fix behavior squashed
    # both stacked values from a cell into one garbled string assigned to
    # a single field).
    pdf_bytes = build_stacked_cell_columns_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    for item in line_items:
        assert "Jan" not in item.mfg_date
        assert "Feb" not in item.expiry_date
        assert "MANKIND" not in item.batch_number
        assert "50005589" not in item.hsn_sac and "50005590" not in item.hsn_sac
