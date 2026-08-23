"""
Synthetic born-digital PDF builders for tests. Not part of the app --
test-only tooling (reportlab, a dev dependency, see requirements-dev.txt).
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

# PIL's built-in fallback bitmap font (used when ImageDraw.text() gets no
# `font`) is tiny and renders badly enough under OCR to be unrepresentative
# of real scanned/photographed text -- confirmed during manual OCR
# verification, where switching to a real TTF at a normal reading size
# fixed almost every misread except genuinely hard cases (e.g. a 64-char
# IRN). Try a handful of TTF locations that cover Windows (local dev) and
# Debian/Ubuntu (deployment/CI target); fall back to the bitmap default
# only if none exist, rather than failing the fixture outright.
_OCR_TEST_FONT_CANDIDATES = [
    "arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _load_ocr_test_font(size: int = 20) -> ImageFont.ImageFont:
    for candidate in _OCR_TEST_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_valid_png_bytes(text: str | None = None, size: tuple[int, int] = (400, 100)) -> bytes:
    """
    A genuinely valid, fully-decodable PNG -- unlike a hand-crafted magic-byte
    fixture, this actually survives Image.open()+convert() (e.g. OCR
    preprocessing), not just the file_validation magic-byte check. Pass
    `text` to render simple readable text onto it for OCR-path tests --
    rendered with a real TTF font when one can be located (see
    _load_ocr_test_font) rather than PIL's tiny bitmap default.
    """
    image = Image.new("RGB", size, color="white")
    if text:
        ImageDraw.Draw(image).text((10, 10), text, fill="black", font=_load_ocr_test_font())
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

# Column x-positions + headers for a bordered (ruled-line) line-items table.
# PyMuPDF's find_tables() detects vector-ruled tables, not just aligned
# text, so tests that want line-item extraction to succeed must draw real
# grid lines -- see native_pdf_extraction.py's FRAGILE notes on this.
_COL_X = [50, 190, 240, 280, 340, 410, 460, 510, 560]
_HEADERS = ["Description", "HSN", "Qty", "Rate", "Taxable Amt", "CGST", "SGST", "Total"]


def _draw_bordered_table(c: canvas.Canvas, top_y: float, rows: list[tuple]) -> float:
    row_height = 16
    n_rows = len(rows) + 1  # + header
    bottom_y = top_y - row_height * n_rows

    for i in range(n_rows + 1):
        y = top_y - i * row_height
        c.line(_COL_X[0], y, _COL_X[-1], y)
    for x in _COL_X:
        c.line(x, top_y, x, bottom_y)

    c.setFont("Helvetica-Bold", 9)
    text_y = top_y - 11
    for x, h in zip(_COL_X, _HEADERS):
        c.drawString(x + 3, text_y, h)

    c.setFont("Helvetica", 9)
    for row_idx, row in enumerate(rows, start=1):
        text_y = top_y - row_idx * row_height - 11
        for x, val in zip(_COL_X, row):
            c.drawString(x + 3, text_y, str(val))

    return bottom_y - 20


def build_invoice_pdf_bytes(invoices: list[dict]) -> bytes:
    """
    Builds a single PDF containing one or more invoices back to back.

    Each invoice dict:
        invoice_number, invoice_date, party_name, gstin, items (list of
        8-tuples matching _HEADERS), subtotal, total, and optionally
        pages (int, default 1) + continuation_items (drawn on every page
        after the first, to simulate a multi-page single invoice) +
        vendor_name (default "Test Vendor Co" -- drawn as a "For <vendor>"
        signature line, since party_name/party_gstin now represent the
        vendor/seller, extracted via that signature line rather than the
        "Bill To" label used for party_name here previously; "Bill To"
        now populates buyer_name instead -- see native_pdf_extraction.py)
        + vendor_gstin (drawn next to the "For <vendor>" signature line,
        i.e. the VENDOR'S own GSTIN -- distinct from `gstin`, which is
        drawn under "Bill To" and represents the BUYER's GSTIN. Two
        different values by default so a test asserting party_gstin ==
        vendor_gstin also implicitly proves the buyer's nearby GSTIN
        wasn't picked up instead -- see native_pdf_extraction.py's
        section-scoped GSTIN disambiguation).
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    for invoice in invoices:
        pages = invoice.get("pages", 1)
        for page_idx in range(pages):
            y = height - 60
            c.setFont("Helvetica-Bold", 14)
            title = "TAX INVOICE" + (f" (contd. page {page_idx + 1})" if page_idx > 0 else "")
            c.drawString(50, y, title)
            y -= 30

            c.setFont("Helvetica", 10)
            c.drawString(50, y, f"Invoice No: {invoice['invoice_number']}")
            y -= 16
            if page_idx == 0:
                c.drawString(50, y, f"Invoice Date: {invoice['invoice_date']}")
                y -= 16
                c.drawString(50, y, f"Bill To: {invoice['party_name']}")
                y -= 16
                c.drawString(50, y, f"GSTIN: {invoice['gstin']}")
                y -= 16
            y -= 10

            items = invoice["items"] if page_idx == 0 else invoice.get("continuation_items", [])
            if items:
                y = _draw_bordered_table(c, y, items)

            if page_idx == 0:
                c.setFont("Helvetica-Bold", 10)
                c.drawString(340, y, f"Taxable Value: {invoice['subtotal']}")
                y -= 16
                c.drawString(340, y, f"Grand Total: {invoice['total']}")
                y -= 30
                c.setFont("Helvetica", 10)
                c.drawString(50, y, f"For {invoice.get('vendor_name', 'Test Vendor Co')}")
                y -= 16
                c.drawString(50, y, f"GSTIN: {invoice.get('vendor_gstin', '29VENDR5678C1Z9')}")

            c.showPage()

    c.save()
    return buffer.getvalue()


def build_blank_pdf_bytes(page_count: int = 1) -> bytes:
    """A PDF with no text at all -- simulates a scanned/image-only page."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    for _ in range(page_count):
        c.showPage()
    c.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Pharma/GST invoice format (confirmed layout, this round of work) --
# recreates the confirmed field layout as closely as reasonably possible
# from a described (not directly available) real sample. Landscape A4 with
# a small font, since the confirmed line-item table has ~20 columns.
# ---------------------------------------------------------------------------

_PHARMA_LINE_ITEM_COLUMNS = [
    (20, "SR"),
    (80, "Product Description"),
    (40, "HSN/SAC"),
    (38, "Batch No"),
    (38, "Expiry"),
    (32, "Sold"),
    (32, "Free"),
    (36, "Total Qty"),
    (36, "MRP"),
    (36, "PTR"),
    (38, "Rate Pts"),
    (40, "Total Amt"),
    (36, "Discount"),
    (40, "Taxable"),
    (36, "CGST Rate"),
    (40, "CGST Amt"),
    (36, "SGST Rate"),
    (40, "SGST Amt"),
    (36, "IGST Rate"),
    (40, "IGST Amt"),
]

# Two products, each as separate Sold + Free rows (confirmed: not merged),
# sharing the same batch/HSN/expiry per product across its two rows.
_DEFAULT_PHARMA_LINE_ITEMS = [
    {
        "sr": 1, "description": "Paracetamol 500mg Tab", "hsn": "3004", "batch": "B2201", "expiry": "12/2027",
        "sold": 100, "free": "", "total_qty": 100, "mrp": "15.00", "ptr": "10.50", "rate_pts": "",
        "total_amt": "1050.00", "discount": "21.00", "taxable": "1029.00",
        "cgst_rate": "6%", "cgst_amt": "61.74", "sgst_rate": "6%", "sgst_amt": "61.74",
        "igst_rate": "", "igst_amt": "",
    },
    {
        "sr": 2, "description": "Paracetamol 500mg Tab", "hsn": "3004", "batch": "B2201", "expiry": "12/2027",
        "sold": "", "free": 10, "total_qty": 10, "mrp": "15.00", "ptr": "0.00", "rate_pts": "",
        "total_amt": "0.00", "discount": "0.00", "taxable": "0.00",
        "cgst_rate": "6%", "cgst_amt": "0.00", "sgst_rate": "6%", "sgst_amt": "0.00",
        "igst_rate": "", "igst_amt": "",
    },
    {
        "sr": 3, "description": "Amoxicillin 250mg Cap", "hsn": "3004", "batch": "B3312", "expiry": "06/2027",
        "sold": 50, "free": "", "total_qty": 50, "mrp": "40.00", "ptr": "28.00", "rate_pts": "",
        "total_amt": "1400.00", "discount": "28.00", "taxable": "1372.00",
        "cgst_rate": "6%", "cgst_amt": "82.32", "sgst_rate": "6%", "sgst_amt": "82.32",
        "igst_rate": "", "igst_amt": "",
    },
    {
        "sr": 4, "description": "Amoxicillin 250mg Cap", "hsn": "3004", "batch": "B3312", "expiry": "06/2027",
        "sold": "", "free": 5, "total_qty": 5, "mrp": "40.00", "ptr": "0.00", "rate_pts": "",
        "total_amt": "0.00", "discount": "0.00", "taxable": "0.00",
        "cgst_rate": "6%", "cgst_amt": "0.00", "sgst_rate": "6%", "sgst_amt": "0.00",
        "igst_rate": "", "igst_amt": "",
    },
]

# A syntactically valid-looking (but fake) 64-hex-char IRN for tests/demos.
SAMPLE_IRN = "1a2b3c4d5e6f70891a2b3c4d5e6f70891a2b3c4d5e6f70891a2b3c4d5e6f7089"


def _draw_table_with_widths(
    c: canvas.Canvas, top_y: float, left_x: float, columns: list[tuple[float, str]], rows: list[tuple]
) -> float:
    """Bordered grid table with explicit per-column widths (for the wide
    pharma line-item table, unlike the fixed-width _draw_bordered_table above)."""
    row_height = 14
    n_rows = len(rows) + 1  # + header
    bottom_y = top_y - row_height * n_rows

    col_x = [left_x]
    for width, _ in columns:
        col_x.append(col_x[-1] + width)
    right_x = col_x[-1]

    for i in range(n_rows + 1):
        y = top_y - i * row_height
        c.line(left_x, y, right_x, y)
    for x in col_x:
        c.line(x, top_y, x, bottom_y)

    c.setFont("Helvetica-Bold", 6)
    text_y = top_y - 10
    for x, (_, label) in zip(col_x, columns):
        c.drawString(x + 2, text_y, label)

    c.setFont("Helvetica", 6)
    for row_idx, row in enumerate(rows, start=1):
        text_y = top_y - row_idx * row_height - 10
        for x, val in zip(col_x, row):
            c.drawString(x + 2, text_y, "" if val is None else str(val))

    return bottom_y - 20


def build_pharma_invoice_pdf_bytes(
    *,
    irn: str = SAMPLE_IRN,
    ack_number: str = "112010098765432",
    invoice_date: str = "10/08/2026",
    vendor_name: str = "VIJAY SAI MEDICAL DISTRIBUTORS",
    eway_bill_number: str = "",
    eway_bill_date: str = "",
    fssai_number: str = "",
    line_items: list[dict] | None = None,
    tax_brackets: list[tuple[str, str, str]] | None = None,
    totals: dict[str, str] | None = None,
    include_buyer_section: bool = False,
    buyer_name: str = "",
    buyer_address: str = "",
) -> bytes:
    """
    Recreates the confirmed pharma/GST invoice layout as closely as
    reasonably possible (built from a described field layout, not a
    directly available sample file): IRN/Ack/E-Way Bill(blank by
    default)/FSSAI(blank by default) header block, the wide line-items
    table (SR/Description/HSN/Batch/Expiry/Sold/Free/Total Qty/MRP/PTR/
    Rate Pts/Total/Discount/Taxable/CGST/SGST/IGST), a tax-rate-bracket
    summary table, a totals block, and a "For <vendor>" signature line.
    No buyer section by default, matching the real (cropped) sample this
    was built from.
    """
    if line_items is None:
        line_items = _DEFAULT_PHARMA_LINE_ITEMS
    if tax_brackets is None:
        tax_brackets = [("12%", "2401.00", "288.12"), ("5%", "500.00", "25.00")]
    if totals is None:
        totals = {
            "Total Amount": "2450.00",
            "Discount Amount": "49.00",
            "TCS Amount": "10.00",
            "Invoice Amount": "2724.12",
            "Adjustment Amount": "-0.12",
            "Net Payable Amount": "2724.00",
        }

    buffer = BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=page_size)
    width, height = page_size
    left_x = 20
    y = height - 30

    c.setFont("Helvetica-Bold", 12)
    c.drawString(left_x, y, "TAX INVOICE")
    y -= 18

    c.setFont("Helvetica", 8)
    c.drawString(left_x, y, f"IRN No: {irn}")
    y -= 12
    c.drawString(left_x, y, f"Ack No: {ack_number}")
    y -= 12
    c.drawString(left_x, y, f"Invoice Date: {invoice_date}")
    y -= 12
    c.drawString(left_x, y, f"E Way Bill No: {eway_bill_number}")
    y -= 12
    c.drawString(left_x, y, f"E Way Bill Date: {eway_bill_date}")
    y -= 12
    c.drawString(left_x, y, f"FSSAI No: {fssai_number}")
    y -= 12

    if include_buyer_section:
        c.drawString(left_x, y, f"Bill To: {buyer_name}")
        y -= 12
        c.drawString(left_x, y, f"Address: {buyer_address}")
        y -= 12

    y -= 14
    rows = [
        (
            item["sr"], item["description"], item["hsn"], item["batch"], item["expiry"],
            item["sold"], item["free"], item["total_qty"], item["mrp"], item["ptr"],
            item["rate_pts"], item["total_amt"], item["discount"], item["taxable"],
            item["cgst_rate"], item["cgst_amt"], item["sgst_rate"], item["sgst_amt"],
            item["igst_rate"], item["igst_amt"],
        )
        for item in line_items
    ]
    y = _draw_table_with_widths(c, y, left_x, _PHARMA_LINE_ITEM_COLUMNS, rows)

    y -= 14
    c.setFont("Helvetica-Bold", 8)
    c.drawString(left_x, y, "Tax Rate-wise Summary")
    y -= 4
    tax_bracket_columns = [(60, "Rate"), (100, "Taxable Amount"), (100, "Tax Amount")]
    y = _draw_table_with_widths(c, y, left_x, tax_bracket_columns, tax_brackets)

    y -= 14
    c.setFont("Helvetica", 9)
    for label, value in totals.items():
        c.drawString(left_x, y, f"{label}: {value}")
        y -= 13

    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(left_x, y, f"For {vendor_name}")
    y -= 13
    c.drawString(left_x, y, "Authorised Signatory")

    c.showPage()
    c.save()
    return buffer.getvalue()


# A guaranteed-valid 64-hex-char string, distinct from SAMPLE_IRN above --
# used by build_alt_layout_invoice_pdf_bytes so its own fixture data
# doesn't accidentally collide with the pharma-format fixture's IRN.
ALT_SAMPLE_IRN = "a1" * 32


def build_alt_layout_invoice_pdf_bytes(
    *,
    irn: str = ALT_SAMPLE_IRN,
    ack_number: str = "556677889900112",
    invoice_date: str = "15/08/2026",
    vendor_name: str = "NORTHSTAR PHARMA DISTRIBUTORS",
    vendor_gstin: str = "07NORTH1234A1Z5",
    buyer_name: str = "Metro Health Pharmacy",
    buyer_address: str = "22 Lake View Road, Pune",
    buyer_gstin: str = "27METRO5678B1Z2",
    line_items: list[dict] | None = None,
) -> bytes:
    """
    A SECOND, deliberately DIFFERENT invoice layout from both
    build_invoice_pdf_bytes and build_pharma_invoice_pdf_bytes above --
    exists specifically to prove the spatial-extraction architecture in
    native_pdf_extraction.py / spatial_text.py generalizes across layouts
    rather than having memorized one fixture's specific shape:
        - IRN and Ack Number VALUES are painted several rows ABOVE their
          own labels (not same-row, not below) -- exercises
          find_value_near_label's "search rows above" branch specifically,
          the opposite direction from a below-the-label search.
        - "BILL TO" as the buyer-section header, not "CUSTOMER DETAILS"/
          "CONSIGNEE DETAILS" (the other fixtures' wording).
        - A reduced, reordered, PLAIN-TEXT line-item table -- Item / HSN /
          Qty / Rate / Taxable / Tax / Total, with NO ruled grid lines at
          all (so PyMuPDF's find_tables() finds nothing here, forcing the
          header-row-driven positional fallback parser to do the actual
          work) and a deliberately unrecognized "Tax" column mixed in
          among recognized ones (proves an unmapped column is silently
          skipped rather than breaking column assignment for its
          neighbors). Row 2 is a free-goods-style row (quantity only, no
          pricing/tax/total) to prove blank cells don't require any
          format-specific "Sold/Free" handling to work.
        - A "TERMS AND CONDITIONS" block with different wording than any
          other fixture, still containing the word "buyer" twice, to
          confirm the exclusion isn't keyed to one specific sentence.
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    y = height - 60

    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "INVOICE")
    y -= 40

    c.setFont("Helvetica", 10)
    # Values first, several rows before their own labels.
    c.drawString(50, y, irn)
    y -= 16
    c.drawString(50, y, ack_number)
    y -= 16
    c.drawString(50, y, invoice_date)
    y -= 30
    c.drawString(50, y, "IRN No:")
    y -= 16
    c.drawString(50, y, "Ack No:")
    y -= 16
    c.drawString(50, y, "Invoice Date:")
    y -= 30

    c.drawString(50, y, f"For {vendor_name}")
    y -= 16
    c.drawString(50, y, f"GSTIN: {vendor_gstin}")
    y -= 30

    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "BILL TO")
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(50, y, buyer_name)
    y -= 16
    c.drawString(50, y, f"Address: {buyer_address}")
    y -= 16
    c.drawString(50, y, f"GSTIN: {buyer_gstin}")
    y -= 30

    # Plain-text table -- no ruled grid lines, so find_tables() finds
    # nothing here; column order/count is deliberately different from the
    # other two fixtures' tables.
    col_positions = [50, 230, 300, 350, 410, 480, 530]
    headers = ["Item", "HSN", "Qty", "Rate", "Taxable", "Tax", "Total"]
    c.setFont("Helvetica-Bold", 9)
    for x, header in zip(col_positions, headers):
        c.drawString(x, y, header)
    y -= 18

    items = line_items or [
        {
            "item_description": "Vitamin C Effervescent",
            "hsn_sac": "21069099",
            "quantity": "20",
            "unit_rate": "45.00",
            "taxable_value": "900.00",
            "tax": "45.00",
            "line_total": "945.00",
        },
        {
            "item_description": "Vitamin C Effervescent (Free Sample)",
            "hsn_sac": "21069099",
            "quantity": "2",
        },
    ]
    field_order = ["item_description", "hsn_sac", "quantity", "unit_rate", "taxable_value", "tax", "line_total"]
    c.setFont("Helvetica", 9)
    for item in items:
        for field_name, x in zip(field_order, col_positions):
            value = item.get(field_name)
            if value:
                c.drawString(x, y, str(value))
        y -= 16

    y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawString(400, y, "Taxable Total: 900.00")
    y -= 16
    c.drawString(400, y, "Tax Total: 45.00")
    y -= 16
    c.drawString(400, y, "Grand Total: 945.00")

    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "TERMS AND CONDITIONS")
    y -= 16
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "All disputes are subject to local jurisdiction only. The buyer must inspect goods on")
    y -= 12
    c.drawString(50, y, "delivery -- the buyer's silence beyond 48 hours is deemed acceptance of the shipment.")

    c.showPage()
    c.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Two ruled tables on one page -- reproduces the real diagnostic evidence
# that motivated the find_tables()-first rework in native_pdf_extraction.py
# (find_tables() detecting MULTIPLE tables per page, only one of which is
# the actual line-items table). See build_multi_table_line_items_pdf_bytes's
# own docstring for what each piece of this fixture is validating.
# ---------------------------------------------------------------------------

# Brand-group taxable-value subtotals used by this fixture -- kept as a
# module-level constant so a test can cross-check the extracted line items'
# summed taxable_value against these EXACT numbers (the same "Total for
# <brand>" checksum technique requested for the real failing invoice; that
# invoice's own raw text/values were never actually provided this round --
# see native_pdf_extraction.py's module docstring -- so this fixture
# reproduces the pattern rather than the real numbers).
MULTI_TABLE_AURUS_SUBTOTAL = 2000.00
MULTI_TABLE_ZEN_SUBTOTAL = 1200.00


def build_multi_table_line_items_pdf_bytes() -> bytes:
    """
    Two ruled (vector-grid) tables on ONE page:
      - A small DECOY table, drawn FIRST on the page, with just two
        columns ("Item", "Remarks") -- its single "Item" header cell alone
        is enough to satisfy the item_description check, so a naive
        "first table found" or "first qualifying table" selection would
        wrongly pick this one. The REAL line-items table (drawn second,
        11 recognizable columns) must win on keyword-match count instead
        -- see _extract_line_items_from_tables' selection logic.
      - The real line-items table's second data row's Description cell is
        drawn as TWO lines of text within the SAME grid row (not a second
        row) -- simulates a wrapped manufacturer/product name.
        table.extract() returns that as one cell string with an embedded
        newline, exercising _normalize_cell_text.
      - "Total for AURUS" / "Total for ZEN" brand-group subtotal rows
        interleaved between real product rows -- must be excluded from
        the returned line items (see _is_subtotal_row) while the real
        rows immediately around them still parse correctly. Each brand's
        subtotal row value exactly equals the sum of that brand's own
        real rows' taxable_value (see MULTI_TABLE_AURUS_SUBTOTAL/
        MULTI_TABLE_ZEN_SUBTOTAL), for a test to cross-check against.
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    left_x = 20
    y = height - 40

    c.setFont("Helvetica-Bold", 12)
    c.drawString(left_x, y, "TAX INVOICE")
    y -= 30

    decoy_columns = [(80, "Item"), (200, "Remarks")]
    decoy_rows = [("Note", "See attached price list for full catalog")]
    y = _draw_table_with_widths(c, y, left_x, decoy_columns, decoy_rows)
    y -= 20

    # Column widths deliberately sum to comfortably less than A4's ~595pt
    # width (left_x=20 + sum <= ~560) -- a table whose rightmost ruling
    # line falls off the page edge has that column silently dropped by
    # find_tables() entirely, confirmed empirically while building this
    # fixture.
    col_widths = [
        (30, "Sr No"), (140, "Description"), (45, "HSN"), (35, "Batch"),
        (30, "Qty"), (40, "MRP"), (40, "Rate"), (50, "Taxable"),
        (40, "CGST"), (40, "SGST"), (50, "Total"),
    ]
    # (row_height, cells) -- the second AURUS row gets a taller row height
    # (22 vs the normal 14) so its two wrapped Description lines both fit
    # within that ONE row's own vertical span; a normal 14pt row only has
    # room for one 7pt text line plus margin.
    rows = [
        (14, ["1", "AURUS MULTIVITAMIN TABS", "21069011", "BAT100", "10", "120.00", "100.00", "1000.00", "90.00", "90.00", "1180.00"]),
        (22, ["2", "AURUS HEALTHCARE PVT LTD\nIMMUNITY BOOSTER SYRUP", "30049099", "BAT101", "5", "250.00", "200.00", "1000.00", "90.00", "90.00", "1180.00"]),
        (14, ["", "Total for AURUS", "", "", "", "", "", str(MULTI_TABLE_AURUS_SUBTOTAL), "", "", ""]),
        (14, ["3", "ZEN WELLNESS CAPSULES", "21069012", "BAT200", "20", "80.00", "60.00", "1200.00", "108.00", "108.00", "1416.00"]),
        (14, ["", "Total for ZEN", "", "", "", "", "", str(MULTI_TABLE_ZEN_SUBTOTAL), "", "", ""]),
    ]

    header_height = 14
    top_y = y
    # boundaries[i] = the horizontal ruling line ABOVE row i (boundaries[0]
    # is the header's top line, boundaries[1] is the header/row-1 line,
    # etc.) -- row i's own text sits between boundaries[i] and
    # boundaries[i+1], using each row's OWN height rather than a fixed one.
    boundaries = [top_y]
    for row_height, _cells in [(header_height, None)] + rows:
        boundaries.append(boundaries[-1] - row_height)
    bottom_y = boundaries[-1]

    col_x = [left_x]
    for w, _ in col_widths:
        col_x.append(col_x[-1] + w)
    right_x = col_x[-1]

    for yy in boundaries:
        c.line(left_x, yy, right_x, yy)
    for x in col_x:
        c.line(x, top_y, x, bottom_y)

    c.setFont("Helvetica-Bold", 7)
    for x, (_, label) in zip(col_x, col_widths):
        c.drawString(x + 2, boundaries[0] - 10, label)

    c.setFont("Helvetica", 7)
    for row_idx, (_row_height, cells) in enumerate(rows, start=1):
        row_top = boundaries[row_idx]
        for x, val in zip(col_x, cells):
            if not val:
                continue
            for line_idx, line in enumerate(val.split("\n")):
                c.drawString(x + 2, row_top - 10 - line_idx * 9, line)

    c.showPage()
    c.save()
    return buffer.getvalue()


def build_ambiguous_column_headers_pdf_bytes() -> bytes:
    """
    A single ruled line-items table whose header wording reproduces the
    real-invoice quirks that motivated extending _LINE_ITEM_COLUMN_KEYWORDS
    beyond the original pharma-format fixture's wording:
      - "PTS" with no "Rate"/"Points" wording at all (Price To Stockist).
      - A bare "Value" line-amount column instead of "Total".
      - BOTH "Old Mrp" and "New Mrp" columns (a price-revision document) --
        the current MRP must win, the superseded one must not collide with
        it or leak into the same field.
      - BOTH "Disc %" and "Disc Amt" columns -- must map to the separate
        discount_rate/discount_amount fields, not collide on one.
    """
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4
    left_x = 20
    y = height - 40

    c.setFont("Helvetica-Bold", 12)
    c.drawString(left_x, y, "TAX INVOICE")
    y -= 30

    # Widths deliberately sum to comfortably less than A4's ~595pt width
    # (left_x=20 + sum <= ~580) -- see the same note in
    # build_multi_table_line_items_pdf_bytes: a ruling line that falls off
    # the page edge silently drops that whole column from find_tables().
    columns = [
        (30, "Sr No"), (90, "Description"), (35, "HSN"),
        (35, "Old Mrp"), (35, "New Mrp"), (35, "Ptr"), (35, "Pts"),
        (30, "Qty"), (45, "Value"), (35, "Disc %"), (40, "Disc Amt"),
        (45, "Taxable"), (35, "CGST"), (35, "SGST"),
    ]
    rows = [
        ("1", "Test Product", "30049099", "100.00", "120.00", "80.00", "70.00", "10", "800.00", "5", "40.00", "760.00", "68.40", "68.40"),
    ]
    y = _draw_table_with_widths(c, y, left_x, columns, rows)

    c.showPage()
    c.save()
    return buffer.getvalue()
