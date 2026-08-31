"""
Native (born-digital) PDF invoice-field extraction: deterministic text-layer
reading, not OCR -- but this module now ALSO owns the label-matching/
field-mapping logic shared with the OCR path (app/services/ocr_extraction.py),
since both ultimately just hand it plain text. See "Text source vs field
mapping" below for how that split works.

Two passes:
    - extract_page_invoice_numbers(): cheap, per-page invoice-number-only
      pass. Output feeds directly into
      invoice_grouping.group_pages_into_invoices().
    - extract_invoice_group_fields(): heavier per-group pass (header fields
      + best-effort line items), run once pages are already grouped into a
      single invoice.

Everything here is rules-based label matching over plain extracted text --
there is no ML/layout model involved. Spots most likely to need rework once
we have more real samples are marked FRAGILE.

Text source vs field mapping (refactored to add OCR support):
    Previously, "get text off a PDF page" and "find fields in that text"
    were interleaved inside extract_invoice_group_fields -- fine when there
    was only ever one text source, but OCR needed to feed the exact same
    label-matching without duplicating every regex/label list in a second
    module. Split into:
        - "get text": page.get_text() (native, inline where it's called)
          vs ocr_extraction.extract_text_from_image_bytes() (OCR, a
          separate module -- see there) -- one function per source.
        - "find fields in text": extract_header_fields_from_text() below --
          ONE function, used by both native and OCR callers, parameterized
          by an ExtractionSource so it knows how to grade confidence.
    native_pdf_extraction.py does NOT import ocr_extraction.py (it would be
    circular -- ocr_extraction.py imports the shared field-mapper FROM
    here). Instead, extract_page_invoice_numbers()/extract_invoice_group_fields()
    take an optional ocr_page_text_fn callable -- callers (upload.py) pass
    in ocr_extraction.extract_text_from_image_bytes to opt into per-page
    OCR fallback; omitting it preserves the old native-only behavior
    exactly (existing callers/tests didn't need to change).

Confidence and OCR: OCR misreads characters in ways a native text layer
never does, so any field sourced (even partly) from OCR is capped at
REVIEW, never HIGH -- except the IRN, where a result that doesn't cleanly
match the 64-hex-char shape is downgraded to LOW instead, since a malformed
IRN from OCR is a strong signal of a misread character (0/O, 1/l, etc.),
not just generic uncertainty. See _cap_confidence_for_source().

Confirmed pharma/GST invoice format (client-confirmed, this round):
    - invoice_number is specifically the IRN (Invoice Reference Number) --
      a 64-hex-char string from the GST e-invoice system, not a
      vendor-assigned number. See _looks_like_irn.
    - The vendor/seller (party_name/party_gstin) is identified via a
      "FOR <company name>" signature line, NOT a labeled field -- distinct
      from buyer_name/buyer_address (Bill To / M/s / Customer), which is
      the actual invoice recipient.
    - ack_number, eway_bill_number, eway_bill_date, fssai_number are
      frequently blank on real invoices -- treated as legitimate
      NOT_FOUND, not an error.
    - Out of scope by client confirmation: Terms & Conditions text, QR
      code content, "Adj. Details" -- no extraction attempted for these
      (and, since the spatial-extraction work below, actively excluded
      from ever being matched against any field).

Coordinate-based extraction (spatial-extraction round):
    plain page.get_text() flattens a page into a single text stream in
    PDF CONTENT-STREAM order, which is not guaranteed to match VISUAL
    order -- confirmed against a real invoice where a values block was
    painted before its labels block, and where a run of adjacent labels
    was painted as one text object followed by a separate run of colons
    and a separate run of values. Regex label-matching over that flattened
    stream can silently fail (label and value never land adjacent in the
    string) or, worse, silently pair the wrong label with the wrong value.

    Fix: app/services/spatial_text.py (shared with the OCR path -- see
    that module's docstring) reconstructs visual rows from PyMuPDF's
    per-word coordinates (page.get_text("words")), sorted/grouped by true
    (page, y, x) position rather than stream order. Reconstructed-row text
    still goes through the SAME shared label-matching used everywhere
    else (extract_header_fields_from_text) -- coordinates only change how
    the text is assembled before matching, not how matching itself works.
    For the residual case where a label and its value are genuinely on
    DIFFERENT visual rows (not just reordered within one), a spatial
    fallback (spatial_text.find_value_near_label) searches nearby rows for
    the nearest token matching the field's value shape -- see
    _SPATIAL_FALLBACK_FIELDS and _apply_spatial_fallback below. This
    fallback is always graded at most REVIEW (LOW for a malformed IRN):
    it is a positional guess, not a clean same-line label:value pair.

    None of this is native-PDF-specific by design: spatial_text.py takes
    "these are the labels/columns we're looking for" as caller-supplied
    input and has no built-in knowledge of invoices or this format.

Section-scoped field disambiguation (same round):
    Buyer/customer info is now located by searching ONLY the text found
    within a section whose header row matches one of
    BUYER_SECTION_HEADER_PATTERNS (configurable list, e.g. "CUSTOMER
    DETAILS", "CONSIGNEE DETAILS", "BILL TO") -- fixes a real false
    positive where buyer_name matched boilerplate Terms & Conditions text
    containing the word "buyer". Text within a section matching
    EXCLUDE_SECTION_HEADER_PATTERNS (e.g. "TERMS AND CONDITIONS",
    "DISCLAIMER") is dropped BEFORE any field search runs, for every
    field, not just buyer fields. Vendor-vs-buyer field confusion (e.g.
    multiple GSTINs on one document) is resolved the same way: the
    vendor/general search pool explicitly excludes buyer-section rows, so
    a GSTIN appearing only in the buyer section can no longer be matched
    as party_gstin. See spatial_text.segment_sections, _partition_rows.

Header-row-driven line-item table parsing (same round):
    _extract_line_items_from_tables (PyMuPDF's find_tables(), ruled-line
    based) runs first, unchanged. When it finds nothing -- which happens
    whenever a real invoice's table uses whitespace/alignment rather than
    drawn vector ruling lines, a genuinely common case find_tables() can't
    handle at all -- _extract_line_items_positional runs as a fallback:
    it locates the header row generically (the row matching the most
    column-keywords from _LINE_ITEM_COLUMN_KEYWORDS), derives column
    X-ranges from that specific row's header word positions, then buckets
    every subsequent row's words into those ranges. No fixed column
    count/order/position is assumed anywhere -- a different vendor's
    different column layout doesn't need a separate code path, only a
    different actual header row in that vendor's own document. See
    spatial_text.detect_header_row/compute_column_boundaries/assign_row_to_columns.

find_tables()-first, multi-table selection (this round):
    Diagnostic evidence from a real invoice showed find_tables() detecting
    2 tables per page but the old code using 0 of them -- it only ever
    checked rows[0] (the first EXTRACTED row) as "the header", which is
    wrong whenever PyMuPDF detects the header as "external" (drawn above
    the table's own ruled grid, so extract()'s rows don't include it at
    all and rows[0] is actually the first DATA row -- see
    _table_header_names, which prefers table.header.names/.external over
    guessing). It also used EVERY qualifying table on a page rather than
    picking the best one -- harmless when only one table has an
    item_description-shaped header (the common case: a second table on
    the same page is often a tax-bracket-summary or totals block, which
    this already filtered out by requiring a description column), but not
    a real "pick the right one" decision. Now: every table on a page is
    scored by how many of its header cells match a keyword from
    _LINE_ITEM_COLUMN_KEYWORDS (the SAME configurable list used by the
    positional fallback below -- not a second list); among tables whose
    header includes an item_description-shaped column, the highest-scoring
    one is selected, with row count as a tie-breaker only. Keyword-match
    count is the primary signal because it's semantic (does this header
    actually look like a line-items table), whereas row/column count is
    not -- a tax-bracket-summary or totals-block table can easily have
    MORE rows than a short line-items table on the same page (the
    pharma-format fixture's own tax-rate-bracket table is a real example
    of this). Selection reasoning is recorded per page in the
    find_tables diagnostics (table-parsing debug endpoint) even when no
    table qualifies, so a future failure shows WHY nothing was selected,
    not just an empty result.

    A cell whose content wrapped across multiple visual lines within the
    PDF (e.g. a long manufacturer name) comes back from table.extract()
    as a single string with an embedded newline, not a separate row --
    _normalize_cell_text collapses that (and any other internal
    whitespace run) to a single space before the value is used, for both
    text fields and before amount-parsing (harmless there since
    _parse_amount already strips everything except digits/'.'/'-').

    A "Total for <brand>"/"Subtotal"/"Grand Total" row inside an
    otherwise-real ruled line-items table (common wherever a document
    groups items by brand/manufacturer with a running subtotal) has a
    real, non-empty description cell and often a real-looking numeric
    total in one of the amount columns -- indistinguishable from a real
    line item by column position alone. _is_subtotal_row (configurable
    pattern list, _SUBTOTAL_ROW_DESCRIPTION_PATTERNS) filters these out by
    description-text shape in BOTH _extract_line_items_from_tables and
    _extract_line_items_positional, so a brand-subtotal-heavy invoice
    doesn't get extra fake line items no matter which parser handled it.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Callable

import fitz  # PyMuPDF

from app.models.schemas import ConfidenceBand, InvoiceLineItem
from app.services.invoice_grouping import PageInvoiceNumberResult
from app.services.spatial_text import (
    ColumnBoundary,
    Section,
    Word,
    assign_row_to_columns,
    compute_column_boundaries,
    detect_header_row,
    find_value_near_label,
    group_words_into_rows,
    match_row_columns,
    merge_header_subrow,
    row_indices_in_sections,
    row_text,
    rows_to_segmented_text,
    rows_to_text,
    segment_sections,
    split_row_into_column_segments,
)


class ExtractionSource(str, Enum):
    """Where a piece of text came from -- drives confidence-capping in
    _cap_confidence_for_source(). NATIVE_PDF_TEXT_LAYER is only ever
    trustworthy up to HIGH; OCR is capped at REVIEW (LOW for a
    malformed IRN)."""

    NATIVE_PDF_TEXT_LAYER = "native_pdf_text_layer"
    OCR = "ocr"


# Below this many non-whitespace characters of extracted text, a page is
# treated as having no usable text layer (likely scanned -> needs the OCR
# path, see ocr_extraction.py). FRAGILE: arbitrary threshold, not derived
# from real samples.
MIN_TEXT_LAYER_CHARS = 20


def has_usable_text_layer(text: str) -> bool:
    return len(text.strip()) >= MIN_TEXT_LAYER_CHARS


def render_page_to_image_bytes(doc: fitz.Document, page_number: int, dpi: int = 200) -> bytes:
    """
    Renders a PDF page (1-indexed) to PNG bytes at the given DPI, for
    handing off to OCR when the page has no usable text layer. 150-200 DPI
    is a reasonable starting point for OCR accuracy on typical invoice text
    sizes without being unnecessarily slow -- FRAGILE, not tuned against a
    real scanned/photographed sample yet.
    """
    page = doc[page_number - 1]
    zoom = dpi / 72  # PyMuPDF page coordinates are in 72-DPI units
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    return pixmap.tobytes("png")


def extract_page_words(page: fitz.Page, page_number: int) -> list[Word]:
    """page.get_text("words") -- PyMuPDF's per-word coordinate mode (built
    on the same underlying data as get_text("dict")/("blocks"), just
    pre-split to single-word granularity, which is what column-boundary
    assignment and row reconstruction both need) -- returns
    (x0,y0,x1,y1,"word",block_no,line_no,word_no) tuples; only the bbox
    and text matter here since row/column position is derived from real
    coordinates, not PyMuPDF's own block/line grouping.

    Rotated pages (page.rotation != 0, e.g. a landscape-content table
    embedded in a page whose MediaBox is portrait, displayed rotated 90
    degrees) need their word boxes transformed through
    page.rotation_matrix before row/column reconstruction can work at
    all -- get_text("words")/("dict") returns coordinates relative to the
    page's UNROTATED content stream, not the "as displayed" orientation.
    Confirmed against a real invoice: every text line reported dir=(0,-1)
    (vertical) instead of the normal (1,0), and un-rotated word boxes for
    two header cells that are visually on the same row ("SR"/
    "DESCRIPTION") had wildly different y-ranges and identical x-ranges --
    exactly the signature of reading vertical-text boxes as if they were
    horizontal. Applying rotation_matrix and re-normalizing fixes this:
    the same two cells then land on the same y-range as expected. Without
    this, EVERY row/column reconstruction downstream (spatial fallback,
    header-row detection, positional column assignment) silently produces
    nonsense on a rotated page -- not a rare edge case, since a wide
    landscape-style line-item table embedded in an otherwise-portrait-ish
    document is a common reason a real invoice PDF sets page rotation at
    all.
    """
    rotation_matrix = page.rotation_matrix if page.rotation else None
    words: list[Word] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        if rotation_matrix is not None:
            rect = fitz.Rect(x0, y0, x1, y1) * rotation_matrix
            rect.normalize()
            x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
        words.append(Word(text=w[4], x0=x0, y0=y0, x1=x1, y1=y1, page_number=page_number))
    return words


def _partition_rows(rows: list[list[Word]]) -> tuple[list[list[Word]], list[list[Word]]]:
    """
    Splits visually-ordered rows into (general_rows, buyer_rows) using the
    configurable section patterns above (see spatial_text.segment_sections):
    general_rows excludes both buyer-kind and exclude-kind section rows
    (so vendor/IRN/totals search can never match buyer-section content,
    and never matches excluded boilerplate either); buyer_rows is
    buyer-kind rows only. Rows in an exclude-kind section are dropped from
    BOTH pools -- never searched for anything.
    """
    sections = segment_sections(
        rows, SECTION_PATTERNS, max_span_rows=SECTION_MAX_SPAN_ROWS, hard_stop_patterns=SECTION_HARD_STOP_PATTERNS
    )

    # A further, more reliable hard boundary than the regex hard-stops
    # above: wherever a line-item table's own header row is -- detected
    # the SAME generic way _extract_line_items_positional itself detects
    # it (reusing match_row_columns rather than guessing at header wording
    # via regex) -- no "buyer" section should ever extend past it. Found
    # empirically while validating against a second, differently-laid-out
    # fixture: a short buyer block (e.g. just a name + address + GSTIN)
    # immediately followed by a table, with no OTHER recognizable
    # stop-marker anywhere before the table ends, could otherwise have its
    # row-count budget swallow the entire table -- the regex hard-stops
    # above only fire on wording (a totals label, a "For "/description-ish
    # header) that isn't guaranteed to appear before the table does.
    #
    # EVERY qualifying row is collected (not just the single best one, as
    # an earlier version of this did via detect_header_row): a multi-page
    # invoice commonly reprints its buyer-section boilerplate ("Details of
    # Receiver(Billed to)", etc.) on every page, immediately followed by
    # that page's own copy of the table header -- a buyer section started
    # by page 3's reprinted boilerplate needs page 3's own table-header
    # row as its cutoff, not just page 1's. Confirmed necessary: with only
    # the single globally-best header row as a cutoff, a later page's
    # buyer-section restart (from its own boilerplate reprint) swallowed
    # that page's real table header entirely, since the row-count cap
    # never got there and no other stop pattern fired first.
    table_header_like_indices = [
        idx for idx, row in enumerate(rows) if len(match_row_columns(row, _LINE_ITEM_COLUMN_KEYWORDS)[0]) >= 3
    ]
    if table_header_like_indices:

        def _cut_before_table_header(section: Section) -> Section:
            if section.kind != "buyer":
                return section
            cutoff = next((idx for idx in table_header_like_indices if section.row_start < idx < section.row_end), None)
            if cutoff is None:
                return section
            return Section(section.kind, section.row_start, cutoff, section.matched_pattern)

        sections = [_cut_before_table_header(s) for s in sections]
        sections = [s for s in sections if s.row_end > s.row_start]

    exclude_idx = row_indices_in_sections(sections, {"exclude"})
    buyer_idx = row_indices_in_sections(sections, {"buyer"}) - exclude_idx

    # A "buyer" row is clipped to an X cutoff rather than dropped/kept
    # whole: real invoices routinely lay the buyer/receiver block out
    # side-by-side with unrelated columns (invoice metadata, transporter
    # details) on the SAME visual row. Whole-row exclusion previously
    # took the entire merged row out of general_rows, silently deleting
    # the other columns' content too -- confirmed against a real invoice
    # where "Invoice Date :" shared a row with the buyer block and
    # disappeared from extraction entirely as a result. The cutoff is
    # each section's OWN right edge, taken once from its header row (the
    # X where the section's 2nd side-by-side segment begins, if any) --
    # NOT simply "leftmost segment": a receiver block can itself legitimately
    # span more than one sub-column (e.g. name+address in one, that same
    # buyer's own DL/FSSAI/GSTIN numbers in a second, narrower one just to
    # its right) and both must stay buyer-scoped, or the buyer's own GSTIN
    # leaks into general_rows and gets mistaken for the vendor's -- also
    # confirmed against a real invoice. Words at/past the cutoff go to
    # general_rows so genuinely unrelated columns stay searchable.
    buyer_section_cutoffs: dict[int, float] = {}
    for section in sections:
        if section.kind != "buyer":
            continue
        header_segments = split_row_into_column_segments(rows[section.row_start])
        cutoff = header_segments[1][0].x0 if len(header_segments) > 1 else float("inf")
        for row_idx in range(section.row_start, section.row_end):
            buyer_section_cutoffs[row_idx] = cutoff

    buyer_rows: list[list[Word]] = []
    general_rows: list[list[Word]] = []
    for i, row in enumerate(rows):
        if i in exclude_idx:
            continue
        if i in buyer_idx:
            cutoff = buyer_section_cutoffs.get(i, float("inf"))
            buyer_part = [w for w in row if w.x0 < cutoff]
            remainder = [w for w in row if w.x0 >= cutoff]
            if buyer_part:
                buyer_rows.append(buyer_part)
            if remainder:
                general_rows.append(remainder)
            continue
        general_rows.append(row)
    return general_rows, buyer_rows


# (text, source, words, has_text_layer) for one already-processed page --
# see _get_page_text_cached. `words` is [] for an OCR'd page, or for a
# native page with no usable text layer and no ocr_page_text_fn given.
PageCacheEntry = tuple[str, ExtractionSource, list[Word], bool]
PageCache = dict[int, PageCacheEntry]


def _get_page_text_cached(
    doc: fitz.Document,
    page_number: int,
    ocr_page_text_fn: Callable[[bytes], str] | None,
    cache: PageCache,
    dpi: int = 200,
) -> PageCacheEntry:
    """
    THE single place a page's content actually gets read off the PDF or
    OCR'd. Both extract_page_invoice_numbers (the cheap per-page pass) and
    extract_invoice_group_fields (the heavier per-group pass) go through
    this, sharing one `cache` dict between them (upload.py creates it once
    per job and passes it into both) -- fixes a real bug where every page
    was being fully re-extracted, including a second OCR call for scanned
    pages, by the second pass even though the first pass immediately
    before it had already done that exact work. Confirmed via the
    raw-text debug endpoint: a 4-page PDF showed 8 page entries, pages 1-4
    each appearing twice with identical text -- root cause was these two
    passes never sharing results at all, each independently calling
    page.get_text()/get_text("words")/OCR for every page in scope.

    Returns (text, source, words, has_text_layer) -- and returns the SAME
    tuple again on a cache hit, doing no PDF/OCR work at all the second
    time. `text` is the full page's row-reconstructed text (see
    spatial_text.group_words_into_rows) for a native page, or the OCR'd
    text for a scanned one -- NOT pre-scoped to any section (buyer/
    exclude); callers do that themselves from `words`, at whatever
    granularity they need (single-page for the cheap pass, whole-group
    for the heavier one -- see _partition_rows), since that's cheap,
    pure-Python work that doesn't need deduplicating the way the actual
    PDF/OCR reads do.
    """
    if page_number in cache:
        return cache[page_number]

    page = doc[page_number - 1]
    native_text = page.get_text()
    has_text_layer = has_usable_text_layer(native_text)

    if has_text_layer:
        words = extract_page_words(page, page_number)
        text = rows_to_text(group_words_into_rows(words))
        source = ExtractionSource.NATIVE_PDF_TEXT_LAYER
    elif ocr_page_text_fn is not None:
        image_bytes = render_page_to_image_bytes(doc, page_number, dpi=dpi)
        text = ocr_page_text_fn(image_bytes)
        words = []
        source = ExtractionSource.OCR
    else:
        text = native_text
        words = []
        source = ExtractionSource.NATIVE_PDF_TEXT_LAYER

    entry: PageCacheEntry = (text, source, words, has_text_layer)
    cache[page_number] = entry
    return entry


def _cap_confidence_for_source(
    field_name: str, value, confidence: ConfidenceBand, source: ExtractionSource
) -> ConfidenceBand:
    """
    OCR output is inherently less reliable than a native PDF text layer --
    never let an OCR-sourced field grade HIGH, even on an otherwise-clean
    label match. IRN gets stricter treatment: OCR is especially prone to
    misreading individual hex characters (0/O, 1/l, etc.), so a
    non-64-hex-shaped IRN from OCR is downgraded to LOW rather than
    REVIEW -- a malformed IRN is a strong signal something was misread,
    not just uncertain.
    """
    if source != ExtractionSource.OCR:
        return confidence
    if confidence == ConfidenceBand.NOT_FOUND:
        return confidence  # nothing to cap -- it wasn't found at all
    if field_name == "invoice_number" and not _looks_like_irn(value or ""):
        return ConfidenceBand.LOW
    return ConfidenceBand.REVIEW if confidence == ConfidenceBand.HIGH else confidence

# --- Label patterns -----------------------------------------------------
# Kept as plain lists (not hardcoded inline in the regex-building code) so
# new label formats can be appended once we see more real client invoice
# layouts, without touching extraction logic itself.
# FRAGILE: label wording/coverage is a first guess, order matters (first
# match wins), and most of this has not been validated against a full real
# invoice sample.

# IRN-specific labels checked first (highest priority); generic
# invoice-number labels kept after as a fallback for non-IRN formats this
# codebase may still need to support.
INVOICE_NUMBER_LABELS = [
    r"irn\s*no\.?",
    r"irn",
    r"invoice\s*number",
    r"invoice\s*no\.?",
    r"inv\s*no\.?",
    r"bill\s*no\.?",
    r"invoice\s*#",
]

INVOICE_DATE_LABELS = [
    r"invoice\s*date",
    r"bill\s*date",
    r"date\s*of\s*invoice",
    r"dated",
]

ACK_NUMBER_LABELS = [
    r"ack\s*no\.?",
    r"acknowledge?ment\s*no\.?",
]

EWAY_BILL_NUMBER_LABELS = [
    r"e[\s\-]*way\s*bill\s*no\.?",
]

EWAY_BILL_DATE_LABELS = [
    r"e[\s\-]*way\s*bill\s*date",
]

FSSAI_NUMBER_LABELS = [
    r"fssai\s*no\.?",
    r"fssai",
]

# Buyer (the invoice recipient) -- NOT the vendor. See VENDOR_SIGNATURE_RE
# below for vendor/seller identification. Matched only within buyer_text
# (the section-scoped pool -- see BUYER_SECTION_HEADER_PATTERNS below and
# _partition_rows) when called from the native-PDF group-level path; a
# direct extract_header_fields_from_text(text, source) call with no
# buyer_text (e.g. the OCR path, or a test calling it directly) still
# searches the whole blob, same as before this round.
#
# Deliberately NOT included here: bare "buyer"/"customer"/"consignee"/
# "receiver" (no "name" suffix). Real invoices almost always use these as
# SECTION TITLES ("Customer Details", "Consignee Details", "Details of
# Receiver(Billed to)") rather than an inline "Buyer: <name>" label --
# already handled via BUYER_SECTION_HEADER_PATTERNS + the row-after-
# header fallback below. Confirmed harmful empirically: on a real
# invoice, a same-row bare "customer" match against the row "CUSTOMER
# DETAILS : CONSIGNEE DETAILS : ..." captured the literal word "DETAILS"
# as the buyer's name (a HIGH-confidence same-row match, which then
# pre-empted the correct row-after-header fallback).
BUYER_NAME_LABELS = [
    r"bill\s*to",
    r"m\/s\.?",
    r"buyer\s*name",
    r"customer\s*name",
    r"consignee\s*name",
    r"receiver\s*name",
]

BUYER_ADDRESS_LABELS = [
    r"address",
]

PARTY_GSTIN_LABELS = [
    # Longer/more specific first: "GSTIN No." / "GSTIN Number" is common
    # real wording (distinct from bare "GST No." -- note the extra "IN").
    # _search_labeled_value's value-capture starts immediately after
    # whatever this label matched, allowing only a single separator char
    # -- listing bare "gstin" first would match just "GSTIN", then fail
    # to bridge over the trailing "No."/"Number" word before the actual
    # colon+value, producing a false NOT_FOUND. Confirmed on two real
    # invoices, both using "GSTIN No. : <value>".
    r"gstin\s*no\.?",
    r"gstin\s*number",
    r"gstin",
    r"gst\s*no\.?",
    r"gst\s*number",
]

SUBTOTAL_TAXABLE_LABELS = [
    r"taxable\s*value",
    r"sub\s*total",
    r"subtotal",
]

# The confirmed pharma totals block has several distinct labeled amounts in
# sequence (Total Amount -> Discount -> TCS -> Invoice Amount -> Adjustment
# -> Net Payable). Each gets its own dedicated label list/field below so
# they don't collapse into each other; INVOICE_TOTAL_LABELS is reserved for
# the final "net payable" figure specifically.
TOTAL_AMOUNT_LABELS = [r"total\s*amount"]
DISCOUNT_AMOUNT_LABELS = [r"discount\s*amount", r"discount"]
TCS_AMOUNT_LABELS = [r"tcs\s*amount", r"tcs"]
INVOICE_AMOUNT_LABELS = [r"invoice\s*amount"]
ADJUSTMENT_AMOUNT_LABELS = [r"adjustment\s*amount", r"adj\.?\s*amount"]

INVOICE_TOTAL_LABELS = [
    r"net\s*payable\s*amount",
    r"net\s*payable",
    r"grand\s*total",
    r"invoice\s*total",
    r"amount\s*payable",
]

# --- Section boundary patterns (configurable, generic) --------------------
# Drives spatial_text.segment_sections(): each row is checked against
# every pattern below; a match starts a new section of that kind, running
# until the next section marker (of ANY kind) or end of document. This
# module has no other hardcoded notion of "where the buyer section is" or
# "what boilerplate looks like" -- it's entirely driven by these lists, so
# a new invoice layout's different section wording is a one-line addition
# here, not a logic change. See _partition_rows for how the resulting
# sections are used (buyer-field scoping, GSTIN disambiguation, and
# document-wide exclusion of boilerplate from every field).
BUYER_SECTION_HEADER_PATTERNS = [
    r"customer\s*details",
    r"consignee\s*details",
    r"bill\s*to",
    r"ship\s*to",
    r"buyer\s*details",
    r"buyer\s*information",
    r"details\s*of\s*receiver",
    r"receiver\s*details",
]

EXCLUDE_SECTION_HEADER_PATTERNS = [
    r"terms\s*(?:&|and)\s*condition",
    r"terms?\s*of\s*(?:sale|delivery|payment)",
    r"disclaimer",
    r"declaration",
]

SECTION_PATTERNS: dict[str, list[str]] = {
    "buyer": BUYER_SECTION_HEADER_PATTERNS,
    "exclude": EXCLUDE_SECTION_HEADER_PATTERNS,
}

# A customer/buyer-details block is realistically short (name + a couple
# of address lines + GSTIN/state code) -- capped so it can't accidentally
# swallow everything after it (the line-item table, totals) on a document
# where no OTHER section marker happens to follow before end of file.
# "exclude" sections are left uncapped in comparison (no entry here) --
# they legitimately run long (Terms & Conditions is often the longest
# block on the page) and, being intentionally excluded from extraction
# entirely, an over-wide exclusion is a much safer failure mode than an
# under-wide one. FRAGILE: a fixed row-count heuristic, not derived from a
# real sample.
SECTION_MAX_SPAN_ROWS: dict[str, int] = {"buyer": 10}

# Closes a "buyer" section as soon as a row inside it is clearly something
# else -- a totals label, the vendor's own "For <company>" signature line,
# or a line-item table's own header row -- even if the row-count cap above
# hasn't been reached yet and no other section marker has appeared. Without
# this, a document where the buyer block happens to be short (e.g. a
# single "Bill To: <name>" line immediately followed by the line-item
# table) can have its buyer section's row-count budget swallow real
# content that has nothing to do with the buyer -- confirmed while testing
# this round's changes (a short "Bill To:" line's buyer section swallowed
# the vendor's signature line a few rows later, blanking out party_name).
# Reuses the SAME configurable totals-label lists already defined above --
# not a separate hardcoded list. FRAGILE: a reasonable first list, not
# exhaustive; a document whose buyer block is followed by something this
# doesn't recognize still falls back to the row-count cap.
BUYER_SECTION_HARD_STOP_PATTERNS = [
    *TOTAL_AMOUNT_LABELS,
    *INVOICE_TOTAL_LABELS,
    *SUBTOTAL_TAXABLE_LABELS,
    r"^\s*for\s+[A-Za-z]",  # vendor "For <company>" signature line
    r"description.{0,40}(?:hsn|qty|quantity|rate)",  # a likely line-item table header row
]

SECTION_HARD_STOP_PATTERNS: dict[str, list[str]] = {"buyer": BUYER_SECTION_HARD_STOP_PATTERNS}

# Reuses the SAME totals/IRN/Ack label lists already defined above (not a
# separate hardcoded list) to immediately stop the positional line-item
# scan the moment a row matches one of them, regardless of whether that
# row also happens to have words landing in recognized table columns.
# Confirmed necessary against a real invoice whose page, below the real
# 4-row table, embeds a QR code as literal extractable TEXT (an unusual
# font renders each QR module as a character, producing a huge block of
# nonsense-looking rows) -- the existing "2 consecutive unmatched/unusable
# rows" stop never fired because the item_description column is wide
# enough that almost any stray word on the page lands inside it, so each
# QR-gibberish row registered as "usable data" and was appended as a fake
# line item. A row reliably marking "we've left the table" (a totals
# label, the IRN/Ack block, the signature/terms footer) is a much more
# robust signal than trying to detect gibberish by its shape.
_LINE_ITEM_TABLE_HARD_STOP_PATTERNS = [
    *TOTAL_AMOUNT_LABELS,
    *INVOICE_TOTAL_LABELS,
    *DISCOUNT_AMOUNT_LABELS,
    *TCS_AMOUNT_LABELS,
    *INVOICE_AMOUNT_LABELS,
    *ADJUSTMENT_AMOUNT_LABELS,
    *ACK_NUMBER_LABELS,
    *INVOICE_NUMBER_LABELS,
    r"(?:amount|value)\s*\(in\s*words\)",
    r"authorised\s*signatory",
    r"terms\s*(?:&|and)\s*condition",
    r"adj\.?\s*details",
    r"@\s*\d+\s*%",  # a GST rate-bracket summary row (e.g. "@5% 813.26 @12% ..."), not a line item
    # A separate Dr/Cr note/credit-adjustments table, distinct from the
    # line-items table -- confirmed on a real invoice's final page, which
    # reprints the SAME (unused, no product rows follow it there) product
    # table header as every other page, immediately followed by this
    # different table; its own cell values fell inside the stale header's
    # column boundaries by X-coincidence and were emitted as fake items.
    r"dr\s*/\s*cr",
    r"auto\s*adjustment",
    r"warranty\s*:",
]

# --- Value shape patterns, per field type -------------------------------

_TOKEN_VALUE = r"[A-Za-z0-9][A-Za-z0-9\/\-\.]{1,30}"
_DATE_VALUE = r"\d{1,4}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}"
_GSTIN_VALUE = r"[0-9A-Za-z]{10,15}"
# Leading "-" allowed: header-level amounts like Adjustment Amount are
# realistically signed (a credit/reduction reads as a negative figure).
_AMOUNT_VALUE = r"-?[₹$]?\s?[0-9][0-9,]*\.?[0-9]*"
_LINE_TEXT_VALUE = r"[^\n]{2,80}"
# IRN capture is deliberately broad (10-70 chars, alnum plus the same
# separator chars generic invoice numbers use) -- _looks_like_irn grades
# HIGH vs REVIEW separately, so this just needs to capture whatever token
# follows the label without truncating a real 64-char hex IRN, while still
# capturing non-hex/generic invoice numbers (e.g. "INV/2026/001") so they
# still get graded REVIEW rather than silently coming back as None.
_IRN_VALUE = r"[0-9A-Za-z][0-9A-Za-z\/\-\.]{9,69}"
_NUMERIC_CODE_VALUE = r"[0-9]{6,20}"
# Same shape as _IRN_VALUE, but requires at least one digit somewhere in
# the token (lookahead). Used ONLY by the spatial (nearby-row) fallback,
# never the same-row search: a same-row match already has strong context
# (immediately after a real label), but a nearby-row search has much
# looser positional certainty, and _IRN_VALUE's plain alnum shape happily
# matches ordinary English words -- confirmed empirically, where a
# same-row-less IRN search grabbed a word out of the vendor's own name a
# few rows below the label. A generic invoice/IRN number realistically
# always contains at least one digit; a bare word never does.
_IRN_FALLBACK_VALUE = r"(?=[0-9A-Za-z\/\-\.]*[0-9])[0-9A-Za-z][0-9A-Za-z\/\-\.]{9,69}"


def _search_labeled_value(text: str, label_patterns: list[str], value_pattern: str) -> str | None:
    """
    Returns the first label-adjacent value found on the SAME line, or None.
    Deliberately same-line-only (horizontal whitespace, not \\s) so a blank
    field (e.g. "E Way Bill No: " with nothing after it) doesn't bleed
    across the newline into the next line's label/text -- every field in
    this format can legitimately be blank, so that would produce wrong
    matches rather than a clean NOT_FOUND.
    """
    for label in label_patterns:
        regex = re.compile(rf"(?:{label})[ \t]*[:\-#]?[ \t]*({value_pattern})", re.IGNORECASE)
        match = regex.search(text)
        if match:
            # NOTE: strip ":" only, not "-" -- a leading "-" can be a
            # meaningful sign (e.g. a negative Adjustment Amount), not
            # stray separator punctuation.
            value = match.group(1).strip().strip(":").strip()
            if value:
                return value
    return None


def _looks_like_date(value: str) -> bool:
    return bool(
        re.match(r"^\d{1,4}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}$", value)
        or re.match(r"^[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}$", value)
    )


def _looks_like_gstin(value: str) -> bool:
    # Real GSTIN is exactly 15 alphanumeric chars in a fixed pattern; we
    # only check length here rather than the full checksum pattern.
    # FRAGILE: accepts any 15-char alnum token as "looks right".
    return len(value) == 15


def _looks_like_irn(value: str) -> bool:
    """IRN is a 64-character hex string generated by the GST e-invoice
    system -- an exact-format match is a strong (HIGH) confidence signal,
    since it's not something a vendor could plausibly type in by hand."""
    return bool(re.fullmatch(r"[0-9A-Fa-f]{64}", value))


def _parse_amount(value: str) -> float | None:
    is_negative = value.strip().startswith("-")
    cleaned = re.sub(r"[^0-9.]", "", value)
    if not cleaned:
        return None
    try:
        parsed = float(cleaned)
        return -parsed if is_negative else parsed
    except ValueError:
        return None


# Matches a vendor/seller signature line of the form "For <company name>",
# anchored to the start of a line (not a substring search) since "for" is
# far too common a word to safely match anywhere in body text -- this is
# deliberately a different mechanism from the generic labeled-value search.
_VENDOR_SIGNATURE_RE = re.compile(r"^[ \t]*for[ \t]+([A-Za-z0-9][^\n]{2,80})[ \t]*$", re.IGNORECASE | re.MULTILINE)

# IRN is 64 chars and can wrap to the line below its label on some layouts
# (unlike the shorter fields, which we deliberately keep same-line-only).
_IRN_NEXT_LINE_RE = re.compile(
    r"(?:irn\s*no\.?|irn)[ \t]*[:\-#]?[ \t]*\n[ \t]*([0-9A-Za-z]{20,70})",
    re.IGNORECASE,
)


def _extract_invoice_number(text: str) -> tuple[str | None, ConfidenceBand]:
    raw = _search_labeled_value(text, INVOICE_NUMBER_LABELS, _IRN_VALUE)
    if raw is None:
        match = _IRN_NEXT_LINE_RE.search(text)
        if match:
            raw = match.group(1).strip()
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, (ConfidenceBand.HIGH if _looks_like_irn(raw) else ConfidenceBand.REVIEW)


def _extract_invoice_date(text: str) -> tuple[str | None, ConfidenceBand]:
    raw = _search_labeled_value(text, INVOICE_DATE_LABELS, _DATE_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, (ConfidenceBand.HIGH if _looks_like_date(raw) else ConfidenceBand.REVIEW)


def _extract_vendor_name(text: str) -> tuple[str | None, ConfidenceBand]:
    """Vendor/seller name, via the "FOR <company>" signature line (this
    confirmed format's own convention -- not a labeled field)."""
    match = _VENDOR_SIGNATURE_RE.search(text)
    if match is None:
        return None, ConfidenceBand.NOT_FOUND
    return match.group(1).strip(), ConfidenceBand.HIGH


def _extract_vendor_gstin(text: str) -> tuple[str | None, ConfidenceBand]:
    """
    Grabs the first "GSTIN"-labeled value in `text`. When called from the
    native-PDF group-level path, `text` is already the section-scoped
    general/vendor pool (buyer-section rows excluded -- see
    _partition_rows), so a buyer/consignee GSTIN appearing only inside a
    buyer section can no longer be matched here even though it's the same
    label wording -- disambiguation is by WHICH ROWS get searched, not by
    "take the first match" positional luck. A direct call with an
    unscoped blob (e.g. from a test, or the OCR path, which doesn't yet do
    section scoping) still just takes the first match in that blob.
    """
    raw = _search_labeled_value(text, PARTY_GSTIN_LABELS, _GSTIN_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, (ConfidenceBand.HIGH if _looks_like_gstin(raw) else ConfidenceBand.REVIEW)


def _extract_buyer_name(text: str) -> tuple[str | None, ConfidenceBand]:
    """`text` is expected to already be scoped to the buyer section (see
    _partition_rows/BUYER_SECTION_HEADER_PATTERNS) when called from the
    native-PDF group-level path -- fixes a real false positive where this
    used to match "buyer" inside Terms & Conditions boilerplate when
    searching the whole document. Passed an unscoped blob directly (e.g. a
    test, or the OCR path), it still searches the whole thing, same as
    before."""
    raw = _search_labeled_value(text, BUYER_NAME_LABELS, _LINE_TEXT_VALUE)
    if raw is not None and raw.lstrip().startswith("/"):
        # "Bill To / Place of Supply:" -- a compound SECTION TITLE, not an
        # inline "Bill To: <name>" label (confirmed on a real invoice
        # whose actual buyer name is on the row below this label, not on
        # the label's own row). Rejected so this falls through to
        # NOT_FOUND and lets the row-after-section-header fallback (see
        # extract_invoice_group_fields) pick up the real name instead of
        # this label's own "/ Place of Supply" continuation text.
        raw = None
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, (ConfidenceBand.HIGH if len(raw) >= 2 else ConfidenceBand.REVIEW)


def _extract_buyer_address(text: str) -> tuple[str | None, ConfidenceBand]:
    """Same section-scoping note as _extract_buyer_name. Single-line
    capture only; real addresses commonly wrap 2-3 lines, which this does
    not attempt to join."""
    raw = _search_labeled_value(text, BUYER_ADDRESS_LABELS, _LINE_TEXT_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, ConfidenceBand.REVIEW  # "address" label alone is too generic to call HIGH


def _extract_numeric_code_field(text: str, labels: list[str]) -> tuple[str | None, ConfidenceBand]:
    """For ack_number/eway_bill_number/fssai_number: frequently blank on
    real invoices -- a missing value is a legitimate NOT_FOUND, not an
    error condition."""
    raw = _search_labeled_value(text, labels, _NUMERIC_CODE_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, ConfidenceBand.HIGH


def _extract_date_field(text: str, labels: list[str]) -> tuple[str | None, ConfidenceBand]:
    raw = _search_labeled_value(text, labels, _DATE_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    return raw, (ConfidenceBand.HIGH if _looks_like_date(raw) else ConfidenceBand.REVIEW)


def _extract_amount_field(text: str, labels: list[str]) -> tuple[float | None, ConfidenceBand]:
    raw = _search_labeled_value(text, labels, _AMOUNT_VALUE)
    if raw is None:
        return None, ConfidenceBand.NOT_FOUND
    parsed = _parse_amount(raw)
    if parsed is None:
        return None, ConfidenceBand.REVIEW
    return parsed, ConfidenceBand.HIGH


PageTextRecorder = Callable[[int, ExtractionSource, str], None]


def extract_page_invoice_numbers(
    pdf_bytes: bytes,
    page_numbers: list[int] | None = None,
    ocr_page_text_fn: Callable[[bytes], str] | None = None,
    on_page_text: PageTextRecorder | None = None,
    page_cache: PageCache | None = None,
) -> list[PageInvoiceNumberResult]:
    """
    Cheap per-page pass: for each page in scope, try to find an invoice
    number (IRN) via label matching, and flag whether the page has a
    usable text layer at all. page_numbers is 1-indexed; None scans every page.

    ocr_page_text_fn: optional callable (image bytes -> text), e.g.
    ocr_extraction.extract_text_from_image_bytes. When given, a page with
    no usable native text layer is rendered to an image and OCR'd instead
    of being left as an undetected page -- important for correct grouping
    of scanned invoices (an IRN never found means the page can only ever
    attach as an "undetected" continuation page, see invoice_grouping.py).
    Omit it to keep the old native-only behavior exactly as before.

    on_page_text: optional callable (page_number, source, text), invoked
    the FIRST time each page is actually read/OCR'd -- a debugging hook
    only (see app/core/debug_store.py), never consulted for extraction
    itself.

    page_cache: optional shared dict this function populates as it reads
    each page (see _get_page_text_cached) -- pass the SAME dict into a
    subsequent extract_invoice_group_fields call (upload.py does) so that
    call reuses these results instead of re-reading/re-OCR'ing every page
    a second time. Pass None (the default) to run standalone -- behaves
    exactly as before this parameter existed.
    """
    results: list[PageInvoiceNumberResult] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        cache: PageCache = page_cache if page_cache is not None else {}
        pages_to_scan = page_numbers if page_numbers is not None else list(range(1, doc.page_count + 1))
        for page_number in pages_to_scan:
            already_cached = page_number in cache
            text, source, words, has_text_layer = _get_page_text_cached(doc, page_number, ocr_page_text_fn, cache)

            if not already_cached and on_page_text is not None:
                on_page_text(page_number, source, text)

            if has_text_layer:
                # Page-scoped (not group-scoped) section partitioning --
                # this pass only needs THIS page's own invoice number, not
                # multi-page buyer/exclude scoping (see
                # extract_invoice_group_fields for the group-scoped
                # version of the same partitioning).
                rows = group_words_into_rows(words)
                general_rows, _buyer_rows = _partition_rows(rows)
                search_text = rows_to_text(general_rows)
            else:
                general_rows = None
                search_text = text

            invoice_number, confidence = _extract_invoice_number(search_text)
            if confidence == ConfidenceBand.NOT_FOUND and general_rows is not None:
                # Same-row search found nothing -- try the spatial fallback
                # for the case where the IRN/invoice-number value is on a
                # different visual row than its own label entirely (the
                # real-world case this round's fix was diagnosed against).
                invoice_number, confidence = _find_invoice_number_near_label(general_rows)
            confidence = _cap_confidence_for_source("invoice_number", invoice_number, confidence, source)
            results.append(
                PageInvoiceNumberResult(
                    page_number=page_number,
                    invoice_number=invoice_number,
                    confidence=confidence,
                    has_text_layer=has_text_layer,
                )
            )
    finally:
        doc.close()
    return results


# --- Line-item table detection ------------------------------------------
# FRAGILE: this whole section is a first pass at PyMuPDF's built-in
# find_tables() heuristics. It has not been validated against a real
# invoice table layout -- expect it to miss tables, split/merge columns
# wrong, or misread headers until it's tuned against real samples.
#
# Column keyword ordering matters: compound/specific pharma columns (e.g.
# "CGST Rate") are checked BEFORE the generic fallbacks (e.g. plain "Rate"
# -> unit_rate) below them, since a naive substring check would otherwise
# match "CGST Rate" against the generic "rate" keyword first.
#
# KNOWN AMBIGUITY: the confirmed column list has "Total" appear twice --
# once as the Quantity group's own Sold/Free/Total sub-column, and again as
# a separate line-amount column. A flat keyword-substring mapper cannot
# disambiguate two columns with the literal same header text by content
# alone. This module's own test PDF sidesteps the collision by labeling
# them "Total Qty" / "Total Amt" -- if the real invoice's columns are truly
# both just bare "Total", this WILL misassign one to the other, and needs
# positional (column-index-based) disambiguation instead once a real
# sample is available.
_LINE_ITEM_COLUMN_KEYWORDS: dict[str, list[str]] = {
    "cgst_rate": ["cgst rate", "cgst%", "cgst %"],
    "sgst_rate": ["sgst rate", "sgst%", "sgst %"],
    "igst_rate": ["igst rate", "igst%", "igst %"],
    "cgst_amount": ["cgst amt", "cgst amount", "cgst"],
    "sgst_amount": ["sgst amt", "sgst amount", "sgst"],
    "igst_amount": ["igst amt", "igst amount", "igst"],
    # Checked BEFORE discount_amount (same rate-before-amount priority
    # pattern as CGST/SGST/IGST above) -- a real invoice can print both a
    # per-line discount % and the resulting discount amount as separate
    # columns; without this, "DISC %" would fall through to the generic
    # "disc" substring in discount_amount's own keywords and collide with
    # a separate "DISC AMT" column on the same row.
    "discount_rate": ["disc %", "disc%", "discount %", "discount%", "discount rate", "disc rate"],
    "batch_number": ["batch"],
    "expiry_date": ["expiry", "exp date", "exp."],
    "quantity_sold": ["sold"],
    "quantity_free": ["free"],
    "quantity_total": ["total qty", "qty total"],
    # Checked BEFORE "mrp" -- a document mid price-revision can print BOTH
    # an "OLD MRP" and a "NEW MRP" column; without a specific "old mrp"
    # keyword checked first, "OLD MRP" would match "mrp"'s own bare
    # substring and collide with "NEW MRP" over the single `mrp` field
    # (whichever column happened to be processed first would win). Not an
    # InvoiceLineItem field (see _INVOICE_LINE_ITEM_FIELD_NAMES) -- the
    # superseded old value is deliberately not kept, only recognized so it
    # can't steal the slot from the current one.
    "mrp_old": ["old mrp"],
    "mrp": ["new mrp", "mrp"],
    "ptr": ["ptr"],
    # "pts" (no "rate"/"points" wording at all) confirmed against a real
    # invoice that pairs a bare "PTR"/"PTS" column -- Price To Stockist,
    # the standard pharma-distribution counterpart to PTR (Price To
    # Retailer). See rate_pts's comment in schemas.py.
    "rate_pts": ["rate pts", "rate points", "pts"],
    "discount_amount": ["disc amt", "discount amt", "discount", "disc"],
    "taxable_value": ["taxable"],
    # Longer/more specific phrasings listed BEFORE their shorter
    # substrings -- e.g. "product description" before bare "description"
    # -- matters for match_row_columns (the positional parser): it tries
    # each field's keywords in this list's order and stops at the first
    # match, so if "description" were listed first it would match just
    # the single word "DESCRIPTION" out of a "PRODUCT DESCRIPTION" header
    # phrase, leaving "PRODUCT" as a separate unmatched anchor that then
    # narrows the computed column boundary from the left -- confirmed
    # against a real invoice where this cut the first word ("Nefrosave")
    # off every item's description entirely.
    "item_description": [
        "description of goods",
        "product description",
        "goods description",
        "name of product",
        "product name",
        "description",
        "particular",
        "item",
    ],
    "hsn_sac": ["hsn", "sac"],
    "quantity": ["qty", "quantity"],
    "uom": ["uom", "unit"],
    "unit_rate": ["rate", "price"],
    # "value" confirmed against a real invoice whose line-amount column is
    # titled bare "VALUE", not "Total" -- checked after taxable_value (see
    # above) already claims any "Taxable Value" cell first, so this can't
    # collide with it.
    "line_total": ["total", "value"],
    # Not InvoiceLineItem fields -- see _INVOICE_LINE_ITEM_FIELD_NAMES.
    # Kept in this SAME configurable list (not a second one) purely so
    # these columns still count toward header-match scoring/table
    # selection and show up in diagnostics as recognized columns, even
    # though their per-row values are never written to a line item.
    "sr_no": ["sr no", "sr.no", "s.no", "sl no", "sl.no", "serial no", "serial number"],
    # "manufacturer" additionally matters for the POSITIONAL parser's
    # table-end detection, not just scoring: a real invoice's manufacturer
    # column commonly wraps 2-3 lines -- giving it a real column boundary
    # means a wrapped continuation line (e.g. just "LIMITED" on its own
    # row) lands in a recognized column instead of nowhere, so it isn't
    # mistaken for having left the table. See the "not any(field_name in
    # _INVOICE_LINE_ITEM_FIELD_NAMES ...)" branch in
    # _extract_line_items_positional.
    "manufacturer": ["mfgr", "manufacturer", "mfg."],
}

# _LINE_ITEM_COLUMN_KEYWORDS intentionally includes a few keys (currently
# "sr_no" and "mrp_old") with no corresponding InvoiceLineItem field --
# they're real, recognizable invoice columns worth detecting for
# header-match scoring/diagnostics, but have nothing meaningful to be
# written into. Any matched column whose field_name isn't in this set is
# skipped when actually building an InvoiceLineItem, in both
# _extract_line_items_from_tables and _extract_line_items_positional.
_INVOICE_LINE_ITEM_FIELD_NAMES = set(InvoiceLineItem.model_fields)

_LINE_ITEM_NUMERIC_FIELDS = {
    "quantity",
    "quantity_sold",
    "quantity_free",
    "quantity_total",
    "unit_rate",
    "mrp",
    "ptr",
    "rate_pts",
    "taxable_value",
    "discount_rate",
    "discount_amount",
    "cgst_rate",
    "cgst_amount",
    "sgst_rate",
    "sgst_amount",
    "igst_rate",
    "igst_amount",
    "line_total",
}


def _map_table_header_columns(header_row: list[str | None]) -> dict[int, str]:
    """Maps column index -> canonical field name (see
    _LINE_ITEM_COLUMN_KEYWORDS/_INVOICE_LINE_ITEM_FIELD_NAMES -- not every
    mapped name is a real InvoiceLineItem field; callers writing an actual
    line item must additionally check that)."""
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header_row):
        if not cell:
            continue
        cell_lower = cell.strip().lower()
        for field_name, keywords in _LINE_ITEM_COLUMN_KEYWORDS.items():
            if field_name in mapping.values():
                continue
            if any(keyword in cell_lower for keyword in keywords):
                mapping[idx] = field_name
                break
    return mapping


def _normalize_cell_text(value: str) -> str:
    """A cell whose content wrapped across multiple visual lines within the
    PDF comes back from table.extract() as one string with an embedded
    newline (confirmed real-world case: a long manufacturer name) -- collapse
    any internal whitespace run (not just leading/trailing) to a single
    space. Safe to apply before amount-parsing too: _parse_amount already
    strips everything but digits/'.'/'-' , so an embedded newline there was
    already harmless, this just makes text fields consistent as well."""
    return re.sub(r"\s+", " ", value).strip()


# Real invoices commonly append a packing reference to the product name,
# separated by " -" (e.g. "Nefrosave Forte Tablets -15s", "K Mac B6
# Active Liquid - 200ml") -- client-confirmed requirement to split this
# into its own `pack` field rather than leave it embedded in
# item_description. The prefix group is greedy so a description with an
# earlier, unrelated " - " (e.g. "Multi - Vitamin Syrup - 200ml") still
# splits at the LAST such point, not the first. A hyphen with no
# preceding space (e.g. "Anti-Inflammatory") never matches -- deliberately
# narrower than "any hyphen" so a mid-word compound name isn't mistaken
# for a pack reference.
_PACK_SUFFIX_RE = re.compile(r"^(.*\S)\s-\s*(\S+)$")


def _split_description_and_pack(description: str) -> tuple[str, str | None]:
    match = _PACK_SUFFIX_RE.match(description)
    if match is None:
        return description, None
    return match.group(1).strip(), match.group(2).strip()


# A row with a real, non-empty description cell and a real-looking number in
# one of the amount columns is otherwise indistinguishable from a genuine
# line item by column position alone -- but a running "Total for <brand>"/
# "Subtotal"/"Grand Total" row inside an otherwise-real ruled table is a
# summary row, not a product. Checked against the row's OWN description
# text (whichever column that landed in), not the whole row, so it can't
# accidentally match a real product name that happens to contain "total"
# mid-string (e.g. "Total Wellness Multivitamin") -- these patterns are all
# anchored to the START of the description.
_SUBTOTAL_ROW_DESCRIPTION_PATTERNS = [
    r"^\s*grand\s*total\b",
    r"^\s*sub[\s\-]?total\b",
    r"^\s*total\s+for\b",
    r"^\s*total\b",
]


def _is_subtotal_row(description: str | None) -> bool:
    if not description:
        return False
    stripped = description.strip()
    return any(re.match(pattern, stripped, re.IGNORECASE) for pattern in _SUBTOTAL_ROW_DESCRIPTION_PATTERNS)


def _subtotal_row_snapshot(item_kwargs: dict) -> dict:
    """
    A skipped subtotal row ("Total for AURUS", etc.) is excluded from the
    real line items (see _is_subtotal_row), but simply discarding it left
    no way to check a brand group's line items against that row's OWN
    stated total -- exactly the cross-check a caller would want to run.
    Captures the row's description plus whatever fields actually parsed
    off it (typically a taxable_value and/or line_total, everything else
    blank on a real subtotal row) into diagnostics' `subtotal_rows` list,
    instead of just an int count.
    """
    return {
        k: v
        for k, v in item_kwargs.items()
        if k not in ("line_number", "field_confidences") and v not in (None, "")
    }


def _table_header_names(table) -> tuple[list[str | None], bool]:
    """
    Prefers PyMuPDF's own table.header.names/.external over assuming
    table.extract()'s first row is the header. Some detected tables have
    an "external" header (drawn above the table's own ruled grid/bbox,
    e.g. a caption row that isn't part of the grid itself) -- when that's
    the case, extract()'s rows do NOT include the header row at all, and
    treating rows[0] as the header would actually be looking at the first
    DATA row instead (silently breaking column mapping for the entire
    table). Falls back to treating rows[0] as the header only if the
    installed PyMuPDF version's Table object predates the .header
    attribute entirely.
    """
    try:
        header = table.header
        return list(header.names), bool(header.external)
    except AttributeError:
        return None, False


def _extract_line_items_from_tables(
    doc: fitz.Document, page_numbers: list[int], diagnostics: dict | None = None
) -> list[InvoiceLineItem]:
    """
    PRIMARY line-item extraction path -- PyMuPDF's find_tables() (ruled-line
    based) already does real grid/cell segmentation, which is more reliable
    than the positional fallback's X-coordinate guessing whenever it's
    available at all. _extract_line_items_positional only runs (see
    extract_invoice_group_fields) when this returns nothing usable for the
    whole group -- e.g. a table drawn with whitespace/alignment instead of
    vector ruling lines, which find_tables() can't detect no matter how
    good its header wording is.

    When find_tables() detects MULTIPLE tables on one page (a real
    document commonly has a tax-bracket-summary or totals-block table
    alongside the actual line-items table), every candidate is scored by
    how many of its header cells match a keyword from
    _LINE_ITEM_COLUMN_KEYWORDS; the highest-scoring candidate that
    includes an item_description-shaped column is selected -- see the
    module docstring's "find_tables()-first, multi-table selection"
    section for why keyword-match count (a semantic signal) is used ahead
    of row/column count (which isn't).

    diagnostics: optional dict this function POPULATES IN PLACE (same
    mutate-in-place convention as _extract_line_items_positional) --
    per-page: every candidate table found (header names, matched columns,
    whether it had an item_description column), which one was selected
    and why (or why none was), gated behind settings.DEBUG upstream (see
    GET /api/jobs/{job_id}/debug/table-parsing).
    """
    if diagnostics is not None:
        diagnostics.update(pymupdf_find_tables_available=hasattr(fitz.Page, "find_tables"), pages=[])

    if not hasattr(fitz.Page, "find_tables"):
        # Installed PyMuPDF version predates table-finding support.
        return []

    line_items: list[InvoiceLineItem] = []
    line_number = 1

    for page_number in page_numbers:
        page = doc[page_number - 1]
        page_diag = {
            "page_number": page_number,
            "tables_found": 0,
            "tables_used": 0,
            "subtotal_rows": [],
            "error": None,
            "candidates": [],
            "selected_table_index": None,
            "selection_reason": None,
        }
        try:
            table_finder = page.find_tables()
        except Exception as exc:  # noqa: BLE001 - table-finding is best-effort
            if diagnostics is not None:
                page_diag["error"] = str(exc)
                diagnostics["pages"].append(page_diag)
            continue

        page_diag["tables_found"] = len(table_finder.tables)

        candidates: list[dict] = []
        for table_index, table in enumerate(table_finder.tables):
            try:
                rows = table.extract()
            except Exception:  # noqa: BLE001
                continue

            header_names, header_external = _table_header_names(table)
            if header_names is None:
                header_names = rows[0] if rows else []
                header_external = False
            data_rows = rows if header_external else rows[1:]
            if len(rows) < (1 if header_external else 2):
                continue

            column_map = _map_table_header_columns(header_names)
            candidates.append(
                {
                    "table_index": table_index,
                    "bbox": list(table.bbox) if getattr(table, "bbox", None) else None,
                    "row_count": len(data_rows),
                    "header_names": header_names,
                    "header_external": header_external,
                    "matched_columns": {
                        field_name: header_names[idx] for idx, field_name in column_map.items()
                    },
                    "matched_column_count": len(column_map),
                    "has_item_description": "item_description" in column_map.values(),
                    "data_rows": data_rows,
                    "column_map": column_map,
                }
            )

        if diagnostics is not None:
            page_diag["candidates"] = [
                {k: v for k, v in c.items() if k not in ("data_rows", "column_map")} for c in candidates
            ]

        usable = [c for c in candidates if c["has_item_description"]]
        if not usable:
            page_diag["selection_reason"] = (
                "no candidate table's header included an item_description-shaped column"
                if candidates
                else "find_tables() detected no tables on this page"
            )
            if diagnostics is not None:
                diagnostics["pages"].append(page_diag)
            continue

        usable.sort(key=lambda c: (c["matched_column_count"], c["row_count"]), reverse=True)
        selected = usable[0]
        tied = [
            c["table_index"]
            for c in usable[1:]
            if c["matched_column_count"] == selected["matched_column_count"]
        ]
        page_diag["selected_table_index"] = selected["table_index"]
        if tied:
            page_diag["selection_reason"] = (
                f"table {selected['table_index']} selected: tied on {selected['matched_column_count']} "
                f"matched header columns with table(s) {tied}, won on row count "
                f"({selected['row_count']} data rows)"
            )
        else:
            page_diag["selection_reason"] = (
                f"table {selected['table_index']} selected: {selected['matched_column_count']} matched "
                f"header columns, the highest among {len(usable)} candidate(s) with an item_description column"
            )
        page_diag["tables_used"] = 1

        for row in selected["data_rows"]:
            # Checked against the row's FULL text (every non-empty cell,
            # not just whatever landed in item_description) -- confirmed
            # necessary against a real invoice where a "Total for <brand>"
            # subtotal row's label was positioned under the PTR/PTS
            # columns, not DESCRIPTION at all (the label's X-position on a
            # summary row doesn't have to match a real item's), so a
            # description-only check silently missed it entirely and let
            # it through as a fake, mostly-blank line item.
            row_full_text = " ".join(cell.strip() for cell in row if cell and cell.strip())
            is_subtotal = _is_subtotal_row(row_full_text)

            item_kwargs: dict = {
                "line_number": line_number,
                "item_description": "",
                "source_page": page_number,
            }
            field_confidences: dict[str, ConfidenceBand] = {}

            for idx, field_name in selected["column_map"].items():
                if field_name not in _INVOICE_LINE_ITEM_FIELD_NAMES:
                    continue
                if idx >= len(row) or not row[idx]:
                    continue
                raw_value = _normalize_cell_text(row[idx])
                if not raw_value:
                    continue
                if field_name in _LINE_ITEM_NUMERIC_FIELDS:
                    parsed = _parse_amount(raw_value)
                    item_kwargs[field_name] = parsed
                    field_confidences[field_name] = (
                        ConfidenceBand.REVIEW if parsed is not None else ConfidenceBand.NOT_FOUND
                    )
                else:
                    item_kwargs[field_name] = raw_value
                    field_confidences[field_name] = ConfidenceBand.REVIEW

            if is_subtotal:
                # The subtotal label itself may not have landed in the
                # item_description column at all (see above) -- fall back
                # to the row's own full text so the diagnostic snapshot is
                # still readable rather than blank.
                item_kwargs["item_description"] = item_kwargs["item_description"] or row_full_text
                page_diag["subtotal_rows"].append(_subtotal_row_snapshot(item_kwargs))
                continue

            if not item_kwargs.get("item_description"):
                # No description on this row -- likely a wholly blank row
                # rather than a real line item. Skip rather than fabricate
                # a placeholder description.
                continue

            item_kwargs["field_confidences"] = field_confidences
            line_items.append(InvoiceLineItem(**item_kwargs))
            line_number += 1

        if diagnostics is not None:
            diagnostics["pages"].append(page_diag)

    return line_items


# --- Header-row-driven positional line-item parsing (fallback) -----------
# Used when _extract_line_items_from_tables (above, ruled-line based) finds
# nothing -- the common case for a real invoice whose table uses
# whitespace/alignment rather than drawn vector ruling lines, which
# find_tables() has no way to detect at all. Column count/order/position
# are never assumed -- both are derived fresh from whatever header row is
# actually found in a given document (see spatial_text.detect_header_row).
_MAX_POSITIONAL_TABLE_ROWS = 300
# Fewer confidently-matched header columns than this is treated as a weak/
# uncertain header match -- every cell parsed under it is graded LOW
# rather than REVIEW, per the "flag what you're unsure about" requirement.
# FRAGILE: an arbitrary threshold, not derived from real samples.
_STRONG_HEADER_MIN_COLUMNS = 5

# Diagnostic-only cap (see app/core/config.py's DEBUG flag / the
# GET /api/jobs/{job_id}/debug/table-parsing route) -- how many post-header
# data rows get their attempted column assignment recorded for inspection.
# Independent of _MAX_POSITIONAL_TABLE_ROWS, which bounds actual parsing.
_DIAGNOSTIC_SAMPLE_ROW_COUNT = 5


# ---------------------------------------------------------------------------
# Alternate line-item format: "batch detail on its own row" (confirmed
# real invoice -- AbbVie Therapeutics). Each line item prints across TWO
# physical rows instead of one:
#   item row:   <item code> <material code> <description...> <qty> <uom>
#               <mrp> <dist price> <retail price> <trade price> <igst%> <value>
#   detail row: <hsn code>  BATCH NO:<batch>  EXP DT : <expiry>  MFG DT : <mfg>  [<qty repeated>]
#
# This is structurally incompatible with the single-row-per-item column-
# boundary model _extract_line_items_positional uses (one header word ->
# one X-range -> one field): the detail row's "HSN"/"batch"/"expiry"/
# "mfg date" all sit in what would otherwise be the Description column's
# X-range on THIS row, not in their own columns. Bending the existing
# column-boundary machinery to understand "this row's Description column
# actually means something different" would make it fragile for every
# other format it already handles correctly -- so this is its own
# dedicated, narrowly-triggered parser instead, selected per-page (see
# extract_invoice_group_fields) ONLY when a row's text actually matches
# the literal "BATCH NO:" inline pattern below. No other confirmed format
# triggers this: elsewhere "batch"/"no" are separate column HEADER words
# with the batch value alone in its own cell, never glued together with a
# colon like "BATCH NO:125640".
# ---------------------------------------------------------------------------

_BATCH_DETAIL_ROW_SIGNATURE_RE = re.compile(r"batch\s*no\s*:", re.IGNORECASE)

_BATCH_DETAIL_ITEM_ROW_RE = re.compile(
    r"^\d{4,8}\s+"  # item/line code
    r"\S+\s+"  # material code
    r"(?P<description>.+?)\s+"
    r"(?P<quantity>[\d,]+\.\d+)\s+"
    r"(?P<uom>[A-Za-z]+)\s+"
    r"(?P<mrp>[\d,]+\.\d+)\s+"
    r"(?P<dist_price>[\d,]+\.\d+)\s+"
    r"(?P<retail_price>[\d,]+\.\d+)\s+"
    r"(?P<trade_price>[\d,]+\.\d+)\s+"
    r"(?P<igst_rate>[\d,]+\.\d+)%\s+"
    r"(?P<value>[\d,]+\.\d+)\s*$"
)

_BATCH_DETAIL_ROW_RE = re.compile(
    r"^(?P<hsn>\d{4,8})\s+"
    r"batch\s*no\s*:\s*(?P<batch>\S+)\s+"
    r"exp\s*dt\s*:?\s*(?P<expiry>\S+)\s+"
    r"mfg\s*dt\s*:?\s*(?P<mfg>\S+)"
    r"(?:\s+[\d,]+\.\d+)?\s*$",
    re.IGNORECASE,
)


def _split_description_and_pack_trailing_number(description: str) -> tuple[str, str | None]:
    """
    Pack-split rule for the batch-detail-row format ONLY: the LAST
    whitespace-separated token that starts with a digit, through to the
    end of the string, is the pack (e.g. "GLUCOMOL 0.5% 5 ML" -> pack
    "5 ML"; "COMBIGAN OPTHALMIC SOLN 5ML SALE 1100 L" -> pack "1100 L",
    since the earlier "5ML" is NOT the last digit-led token). Client-
    confirmed rule for this format specifically.

    Deliberately a SEPARATE function from _split_description_and_pack
    (the hyphen-based rule used by the positional/find_tables() paths
    elsewhere): reusing that rule here would miss these (no hyphen at
    all), and reusing THIS rule elsewhere would wrongly fire on something
    like "Paracetamol 500mg Tab" (splitting off "500mg Tab" as if it were
    a pack) -- each stays scoped to the format it was confirmed against.

    A literal reading of "last number to end", not a smarter "last
    number immediately followed by a unit" heuristic -- confirmed
    against a real description ("RESTASIS ... 30 X 0.4 ML FLOW WRAP")
    where the literal rule pulls in more trailing words than a human
    would call "the pack size" (pack ends up "0.4 ML FLOW WRAP"); still
    applied as literally specified rather than second-guessed.
    """
    tokens = description.split()
    split_idx = None
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i][:1].isdigit():
            split_idx = i
            break
    if split_idx is None:
        return description, None
    return " ".join(tokens[:split_idx]).strip(), " ".join(tokens[split_idx:]).strip()


def _extract_line_items_batch_detail_rows(
    rows: list[list[Word]], diagnostics: dict | None = None
) -> list[InvoiceLineItem]:
    """
    Dedicated parser for the "batch detail on its own row" format -- see
    the module comment above _BATCH_DETAIL_ROW_SIGNATURE_RE. Pairs each
    item row with its detail row (the next row matching
    _BATCH_DETAIL_ROW_RE), scanning FORWARD past any number of
    intervening non-matching rows to find it -- not just the immediately
    next row. Necessary because `rows` is meant to be the WHOLE group's
    pooled rows (see extract_invoice_group_fields), and a page break can
    land between an item row and its own detail row with an entire
    reprinted header/address/payment-info boilerplate block in between
    (confirmed on a real invoice: the last item on page 1 had its detail
    row as the first table row of page 2, separated by that page's full
    letterhead reprint). The forward scan stops (giving up on a detail
    row for this item) the moment it hits ANOTHER item row first, so a
    genuinely detail-less item can never accidentally swallow the next
    item's own rows while searching.

    An item row with no matching detail row still becomes a line item,
    just without hsn/batch/expiry/mfg_date populated (recorded in
    diagnostics rather than silently dropped). Rows that match neither
    pattern (headers, page boilerplate, the repeated "Description /
    Batch and Expiry Date" sub-header) are silently skipped -- unlike the
    column-boundary parser, a non-matching row here can never be mistaken
    for real data, so there's no "table end" detection to run.
    """
    line_items: list[InvoiceLineItem] = []
    unmatched_item_rows: list[str] = []
    row_idx = 0
    while row_idx < len(rows):
        row = rows[row_idx]
        match = _BATCH_DETAIL_ITEM_ROW_RE.match(row_text(row))
        if match is None:
            row_idx += 1
            continue

        raw_description = _normalize_cell_text(match.group("description"))
        description, pack = _split_description_and_pack_trailing_number(raw_description)
        item_kwargs = dict(
            line_number=len(line_items) + 1,
            item_description=description,
            pack=pack,
            quantity=_parse_amount(match.group("quantity")),
            uom=match.group("uom"),
            mrp=_parse_amount(match.group("mrp")),
            ptr=_parse_amount(match.group("trade_price")),
            igst_rate=_parse_amount(match.group("igst_rate")),
            line_total=_parse_amount(match.group("value")),
            source_page=row[0].page_number,
        )

        detail_match = None
        rows_consumed = 1
        scan_idx = row_idx + 1
        while scan_idx < len(rows):
            candidate_text = row_text(rows[scan_idx])
            detail_match = _BATCH_DETAIL_ROW_RE.match(candidate_text)
            if detail_match is not None:
                rows_consumed = scan_idx - row_idx + 1
                break
            if _BATCH_DETAIL_ITEM_ROW_RE.match(candidate_text) is not None:
                break  # the next real item -- stop, this one has no detail row
            scan_idx += 1

        if detail_match is not None:
            item_kwargs.update(
                hsn_sac=detail_match.group("hsn"),
                batch_number=detail_match.group("batch"),
                expiry_date=detail_match.group("expiry"),
                mfg_date=detail_match.group("mfg"),
            )
            row_idx += rows_consumed
        else:
            unmatched_item_rows.append(row_text(row))
            row_idx += 1

        line_items.append(InvoiceLineItem(**item_kwargs))

    if diagnostics is not None:
        diagnostics.update(
            format="batch_detail_rows",
            line_items_produced=len(line_items),
            item_rows_without_a_matching_detail_row=unmatched_item_rows,
        )
    return line_items


def _word_to_dict(word: Word) -> dict:
    return {"text": word.text, "x0": word.x0, "y0": word.y0, "x1": word.x1, "y1": word.y1}


def _boundary_to_dict(boundary: ColumnBoundary) -> dict:
    # left/right can be +-inf for the first/last column -- not valid JSON
    # (json.dumps rejects it outright), so represented as None ("no
    # boundary on that side") for the diagnostic response instead.
    return {
        "field_name": boundary.field_name,
        "left": boundary.left if boundary.left != float("-inf") else None,
        "right": boundary.right if boundary.right != float("inf") else None,
        "header_text": boundary.header_text,
    }


def _extract_line_items_positional(
    rows: list[list[Word]], diagnostics: dict | None = None
) -> list[InvoiceLineItem]:
    """
    diagnostics: optional dict this function POPULATES IN PLACE (same
    mutate-in-place convention as _apply_spatial_fallback) with exactly
    what header-detection/column-boundary/row-assignment decisions it
    made -- gated behind settings.DEBUG upstream (see
    GET /api/jobs/{job_id}/debug/table-parsing), never built otherwise.
    Populated at EVERY exit point, including the early "no header found"
    and "no description column" returns, since a diagnostic that only
    ever appears on success is useless for debugging a failure -- the
    whole point is showing the attempted mapping even when it's wrong or
    empty, not just a final empty line_items list.
    """
    if diagnostics is not None:
        diagnostics.update(
            header_detected=False,
            header_row_index=None,
            header_row_text=None,
            matched_columns=[],
            unmatched_header_words=[],
            column_boundaries=[],
            sample_rows=[],
            line_items_produced=0,
            subtotal_rows=[],
            stopped_reason=None,
            sub_header_merged=False,
        )

    header = detect_header_row(rows, _LINE_ITEM_COLUMN_KEYWORDS, min_matched_columns=3)
    if header is None:
        if diagnostics is not None:
            diagnostics["stopped_reason"] = "no row matched >= 3 column keywords -- no header row detected at all"
        return []
    header_row_idx, header_matches, header_unmatched = header

    # Real invoices commonly print column headers across TWO physical
    # lines -- a group label ("CGST", "OLD", "SALE") on one line, a
    # disambiguating sub-label ("%", "MRP", "QTY") on the next -- confirmed
    # against real documents. If the row right after the detected header
    # ALSO scores as clearly header-vocabulary rather than data, it's
    # merged into the header (see merge_header_subrow) and header_matches/
    # header_unmatched are recomputed from the merged result, instead of
    # just being skipped as a wasted row: the group word ALONE is often
    # genuinely ambiguous or unmatched entirely (bare "CGST" defaults to
    # the wrong field -- amount instead of rate -- and bare "OLD"/"SALE"
    # match no keyword at all), so simply skipping the sub-label row threw
    # its disambiguating information away. Confirmed necessary against a
    # real invoice where, without the merge, MRP and quantity came back
    # completely blank and CGST/SGST/IGST were mapped to the wrong field.
    _SUB_HEADER_MIN_MATCHES = 2
    data_start_idx = header_row_idx + 1
    sub_header_merged = False
    if data_start_idx < len(rows):
        sub_header_matches, _sub_header_unmatched = match_row_columns(rows[data_start_idx], _LINE_ITEM_COLUMN_KEYWORDS)
        if len(sub_header_matches) >= _SUB_HEADER_MIN_MATCHES:
            merged_row, orphan_subs = merge_header_subrow(rows[header_row_idx], rows[data_start_idx])
            header_matches, header_unmatched = match_row_columns(merged_row, _LINE_ITEM_COLUMN_KEYWORDS)
            # Lower-priority fallback over the ORPHANS ONLY (see
            # merge_header_subrow's docstring) -- run SECOND and only for
            # fields the merged pass didn't already claim, so an orphan
            # sub-label can resolve a field nothing else matched (e.g. a
            # bare "SOLD" with no primary counterpart) without being able
            # to outrank a genuine primary-row column for a field both
            # could plausibly match (the "Total"-beats-"TOTAL" collision
            # merge_header_subrow's docstring describes). Orphans this
            # pass STILL doesn't claim are appended to header_unmatched
            # too -- they remain useful boundary anchors even unclaimed
            # (confirmed necessary: without "Total" as an anchor here,
            # MRP's boundary crept left and swallowed a neighboring
            # quantity value into the same cell).
            remaining_keywords = {
                name: kws for name, kws in _LINE_ITEM_COLUMN_KEYWORDS.items() if name not in header_matches
            }
            if orphan_subs:
                fallback_matches, fallback_unmatched = match_row_columns(orphan_subs, remaining_keywords)
                header_matches.update(fallback_matches)
                header_unmatched = header_unmatched + fallback_unmatched
            data_start_idx += 1
            sub_header_merged = True

    if diagnostics is not None:
        diagnostics.update(
            header_detected=True,
            header_row_index=header_row_idx,
            header_row_text=row_text(rows[header_row_idx]),
            matched_columns=[
                {"field_name": name, **_word_to_dict(word)} for name, word in header_matches.items()
            ],
            unmatched_header_words=[_word_to_dict(w) for w in header_unmatched],
            sub_header_merged=sub_header_merged,
        )

    if "item_description" not in header_matches:
        # Can't even find a description column -- don't guess at a table
        # structure that might not be a line-items table at all (could be
        # the tax-bracket-summary table instead -- see
        # _extract_tax_bracket_summary).
        if diagnostics is not None:
            diagnostics["stopped_reason"] = (
                "header row detected, but none of its matched columns is 'item_description' -- "
                "not treated as a line-items table (could be the tax-bracket-summary table instead)"
            )
        return []

    boundaries: list[ColumnBoundary] = compute_column_boundaries(header_matches, header_unmatched)
    base_confidence = ConfidenceBand.LOW if len(header_matches) < _STRONG_HEADER_MIN_COLUMNS else ConfidenceBand.REVIEW

    if diagnostics is not None:
        diagnostics["column_boundaries"] = [_boundary_to_dict(b) for b in boundaries]
        diagnostics["base_confidence"] = base_confidence.value

    line_items: list[InvoiceLineItem] = []
    line_number = 1
    consecutive_unmatched_rows = 0
    data_rows = rows[data_start_idx:]

    for row_offset, row in enumerate(data_rows):
        if line_number > _MAX_POSITIONAL_TABLE_ROWS:
            if diagnostics is not None and diagnostics["stopped_reason"] is None:
                diagnostics["stopped_reason"] = f"hit the {_MAX_POSITIONAL_TABLE_ROWS}-row safety cap"
            break

        if any(re.search(p, row_text(row), re.IGNORECASE) for p in _LINE_ITEM_TABLE_HARD_STOP_PATTERNS):
            # An unconditional stop, checked BEFORE column assignment --
            # a totals/IRN/Ack/signature/terms row is reliable evidence
            # the table has ended even if some of its words happen to
            # land inside a recognized column by X-coincidence (see
            # _LINE_ITEM_TABLE_HARD_STOP_PATTERNS).
            if diagnostics is not None and diagnostics["stopped_reason"] is None:
                diagnostics["stopped_reason"] = (
                    f"row matched a table-end marker (totals/IRN/Ack/signature/terms label) at row index "
                    f"{data_start_idx + row_offset}"
                )
            break

        cells = assign_row_to_columns(row, boundaries)
        non_empty_cells = {k: v.strip() for k, v in cells.items() if v.strip()}

        if diagnostics is not None and row_offset < _DIAGNOSTIC_SAMPLE_ROW_COUNT:
            # Recorded regardless of whether this row ends up a real line
            # item, an "unmatched" row, or a totals row that got filtered
            # out below -- the actual attempted mapping, not just what
            # survived.
            diagnostics["sample_rows"].append(
                {
                    "row_index": data_start_idx + row_offset,
                    "words": [_word_to_dict(w) for w in sorted(row, key=lambda w: w.x0)],
                    "assigned_cells": dict(cells),
                }
            )

        if not non_empty_cells:
            consecutive_unmatched_rows += 1
            if consecutive_unmatched_rows >= 2:
                # Two rows in a row with nothing landing in any known
                # column -- past the end of the table (blank space, or
                # the start of an unrelated block, e.g. totals/tax
                # summary that this table's column X-ranges don't apply
                # to at all).
                if diagnostics is not None and diagnostics["stopped_reason"] is None:
                    diagnostics["stopped_reason"] = (
                        f"2 consecutive rows with no words landing in any known column (row index "
                        f"{data_start_idx + row_offset})"
                    )
                break
            continue

        if not any(field_name in _INVOICE_LINE_ITEM_FIELD_NAMES for field_name in non_empty_cells):
            # Every word in this row landed in a recognized-but-not-a-real-
            # field column (e.g. "manufacturer" -- see
            # _INVOICE_LINE_ITEM_FIELD_NAMES) -- typically a wrapped
            # continuation line of a multi-line cell (a manufacturer name
            # that wraps 2-3 lines is common on real invoices; confirmed
            # against a real document where this caused the table scan to
            # stop after just 3 rows, mistaking each wrap line for
            # evidence of having left the table). This IS still clearly
            # part of the table's own column structure, unlike a fully
            # blank row -- don't count it toward the "left the table"
            # streak, but it isn't a line item either.
            consecutive_unmatched_rows = 0
            continue

        item_kwargs: dict = {
            "line_number": line_number,
            "item_description": "",
            "source_page": row[0].page_number,
        }
        field_confidences: dict[str, ConfidenceBand] = {}

        for field_name, raw_value in non_empty_cells.items():
            if field_name not in _INVOICE_LINE_ITEM_FIELD_NAMES:
                # e.g. "sr_no" -- a recognized column with no matching
                # InvoiceLineItem field (see _INVOICE_LINE_ITEM_FIELD_NAMES).
                continue
            if field_name in _LINE_ITEM_NUMERIC_FIELDS:
                parsed = _parse_amount(raw_value)
                item_kwargs[field_name] = parsed
                field_confidences[field_name] = base_confidence if parsed is not None else ConfidenceBand.NOT_FOUND
            else:
                item_kwargs[field_name] = raw_value
                field_confidences[field_name] = base_confidence

        has_usable_data = item_kwargs["item_description"] or any(
            v is not None for k, v in item_kwargs.items() if k not in ("line_number", "item_description", "source_page")
        )
        if not has_usable_data:
            # Words landed in known column ranges, but nothing about this
            # row actually parsed into anything usable (e.g. a totals row
            # like "Grand Total: 945.00" whose "Total:" token lands in the
            # line_total column but parses to nothing, while the real
            # amount lands elsewhere) -- treat it the same as a row that
            # matched no columns at all, not as a real (if very sparse)
            # line item. Confirmed necessary empirically: without this, a
            # totals block right below the table was silently emitted as
            # several extra, entirely-empty "line items".
            consecutive_unmatched_rows += 1
            if consecutive_unmatched_rows >= 2:
                if diagnostics is not None and diagnostics["stopped_reason"] is None:
                    diagnostics["stopped_reason"] = (
                        f"2 consecutive rows with matched cells that parsed to nothing usable (row index "
                        f"{data_start_idx + row_offset})"
                    )
                break
            continue
        consecutive_unmatched_rows = 0

        # Checked against the row's FULL text, not just whatever landed in
        # item_description -- confirmed necessary against a real invoice
        # where a "Total for <brand>" subtotal row's label was positioned
        # under the PTR/PTS columns rather than under DESCRIPTION at all
        # (a summary row's label doesn't have to line up where a real
        # item's description would), so checking item_description alone
        # silently missed it and let it through as a fake line item.
        if _is_subtotal_row(row_text(row)):
            # A real, non-blank row (has_usable_data passed) that reads as
            # a "Total for <brand>"/"Subtotal"/"Grand Total" summary line
            # rather than a product -- not the end of the table (more real
            # items commonly follow a brand-group subtotal), just not a
            # line item itself. See _is_subtotal_row.
            if diagnostics is not None:
                item_kwargs["item_description"] = item_kwargs["item_description"] or row_text(row)
                diagnostics["subtotal_rows"].append(_subtotal_row_snapshot(item_kwargs))
            continue

        item_kwargs["field_confidences"] = field_confidences
        line_items.append(InvoiceLineItem(**item_kwargs))
        line_number += 1

    if diagnostics is not None:
        diagnostics["line_items_produced"] = len(line_items)
        if diagnostics["stopped_reason"] is None:
            diagnostics["stopped_reason"] = "reached the end of the document's rows"

    return line_items


# --- Invoice-level tax bracket summary -----------------------------------
# Separate GST rate-wise breakup table (5% / 12% / 18% / 28% brackets, each
# with its own taxable_amount + tax_amount), kept deliberately independent
# of the per-line-item tax fields above -- no consolidation between the two.

def _looks_like_tax_bracket_table(header_row: list[str | None]) -> bool:
    header_text = " ".join((cell or "").lower() for cell in header_row)
    return (
        "rate" in header_text
        and ("taxable" in header_text or "tax" in header_text)
        and "description" not in header_text
        and "hsn" not in header_text
    )


def _extract_tax_bracket_summary(doc: fitz.Document, page_numbers: list[int]) -> list[dict]:
    if not hasattr(fitz.Page, "find_tables"):
        return []

    summary: list[dict] = []

    for page_number in page_numbers:
        page = doc[page_number - 1]
        try:
            table_finder = page.find_tables()
        except Exception:  # noqa: BLE001
            continue

        for table in table_finder.tables:
            try:
                rows = table.extract()
            except Exception:  # noqa: BLE001
                continue
            if len(rows) < 2 or not _looks_like_tax_bracket_table(rows[0]):
                continue

            header = [(cell or "").strip().lower() for cell in rows[0]]
            rate_idx = next((i for i, h in enumerate(header) if "rate" in h), None)
            taxable_idx = next((i for i, h in enumerate(header) if "taxable" in h), None)
            tax_idx = next(
                (i for i, h in enumerate(header) if "tax amt" in h or "tax amount" in h or h == "tax"), None
            )
            if rate_idx is None:
                continue

            for row in rows[1:]:
                if rate_idx >= len(row) or not row[rate_idx]:
                    continue
                rate = _parse_amount(row[rate_idx])
                if rate is None:
                    continue
                taxable_amount = (
                    _parse_amount(row[taxable_idx]) if taxable_idx is not None and taxable_idx < len(row) else None
                )
                tax_amount = _parse_amount(row[tax_idx]) if tax_idx is not None and tax_idx < len(row) else None
                summary.append({"rate": rate, "taxable_amount": taxable_amount, "tax_amount": tax_amount})

    return summary


def extract_header_fields_from_text(
    text: str, source: ExtractionSource, buyer_text: str | None = None
) -> tuple[dict, dict[str, ConfidenceBand]]:
    """
    THE shared field-mapper -- every label-matched header field (IRN, Ack
    No, E-Way Bill No/Date, FSSAI No, vendor, buyer, totals block), used by
    BOTH the native-PDF path (extract_invoice_group_fields below) and the
    OCR path (ocr_extraction.py). `source` drives confidence-capping (see
    _cap_confidence_for_source) -- pass ExtractionSource.OCR whenever any
    part of `text` came from OCR rather than a native PDF text layer.

    buyer_text: optional, section-scoped text to search for buyer_name/
    buyer_address instead of `text` (see _partition_rows /
    BUYER_SECTION_HEADER_PATTERNS) -- everything else always searches
    `text`. Omit it (default None) to search buyer fields within `text`
    too, same as before section-scoping existed; the native-PDF
    group-level path always passes it (even as an empty string when no
    buyer section was detected -- that's a deliberate NOT_FOUND rather
    than falling back to an unscoped, false-positive-prone search).

    Does NOT include tax_bracket_summary or line_items -- those are
    table-structure extractions via PyMuPDF's find_tables()/the
    coordinate-based fallback (_extract_line_items_positional), which read
    the PDF's own structure/coordinates and have no meaning for a single
    flat text blob (see ocr_extraction.py's module docstring for why OCR
    doesn't get these at all).

    Out of scope by client confirmation, not attempted here: QR code
    content, "Adj. Details". Terms & Conditions text is additionally now
    actively excluded upstream (see _partition_rows) rather than merely
    "not attempted" -- it used to be a real source of false-positive
    matches (e.g. buyer_name matching the word "buyer" inside boilerplate).
    """
    header_fields: dict = {}
    header_field_confidences: dict[str, ConfidenceBand] = {}
    buyer_search_text = text if buyer_text is None else buyer_text

    def set_field(name: str, value, confidence: ConfidenceBand) -> None:
        header_fields[name] = value
        header_field_confidences[name] = _cap_confidence_for_source(name, value, confidence, source)

    set_field("invoice_number", *_extract_invoice_number(text))
    set_field("invoice_date", *_extract_invoice_date(text))
    set_field("ack_number", *_extract_numeric_code_field(text, ACK_NUMBER_LABELS))
    set_field("eway_bill_number", *_extract_numeric_code_field(text, EWAY_BILL_NUMBER_LABELS))
    set_field("eway_bill_date", *_extract_date_field(text, EWAY_BILL_DATE_LABELS))
    set_field("fssai_number", *_extract_numeric_code_field(text, FSSAI_NUMBER_LABELS))
    set_field("party_name", *_extract_vendor_name(text))
    set_field("party_gstin", *_extract_vendor_gstin(text))
    set_field("buyer_name", *_extract_buyer_name(buyer_search_text))
    set_field("buyer_address", *_extract_buyer_address(buyer_search_text))
    set_field("subtotal_taxable", *_extract_amount_field(text, SUBTOTAL_TAXABLE_LABELS))
    set_field("total_amount", *_extract_amount_field(text, TOTAL_AMOUNT_LABELS))
    set_field("discount_amount", *_extract_amount_field(text, DISCOUNT_AMOUNT_LABELS))
    set_field("tcs_amount", *_extract_amount_field(text, TCS_AMOUNT_LABELS))
    set_field("invoice_amount", *_extract_amount_field(text, INVOICE_AMOUNT_LABELS))
    set_field("adjustment_amount", *_extract_amount_field(text, ADJUSTMENT_AMOUNT_LABELS))
    set_field("invoice_total", *_extract_amount_field(text, INVOICE_TOTAL_LABELS))

    return header_fields, header_field_confidences


# --- Spatial fallback (label and value on different visual rows) ---------
# Reuses the SAME configurable label lists already defined above -- this is
# not a second, separately-maintained pattern list, just a pointer at which
# (labels, value-shape) pairs are eligible for the positional fallback.
# invoice_number is handled separately (_find_invoice_number_near_label,
# below) rather than through this dict -- _IRN_VALUE's shape is loose
# enough (any 10-70 char alnum token) that a plain nearest-token search
# using it directly was confirmed, empirically, to grab an unrelated
# nearby word (e.g. one word out of the vendor's own name) when no real
# IRN was actually nearby; invoice_number needs the stricter two-tier
# search that function does instead.
_SPATIAL_FALLBACK_FIELDS: dict[str, tuple[list[str], str]] = {
    "ack_number": (ACK_NUMBER_LABELS, _NUMERIC_CODE_VALUE),
    "eway_bill_number": (EWAY_BILL_NUMBER_LABELS, _NUMERIC_CODE_VALUE),
    "eway_bill_date": (EWAY_BILL_DATE_LABELS, _DATE_VALUE),
    "fssai_number": (FSSAI_NUMBER_LABELS, _NUMERIC_CODE_VALUE),
    "invoice_date": (INVOICE_DATE_LABELS, _DATE_VALUE),
}


def _find_invoice_number_near_label(
    rows: list[list[Word]], exclude_values: set[str] | None = None
) -> tuple[str | None, ConfidenceBand]:
    """
    Two-tier spatial search, specifically for invoice_number: an EXACT
    64-hex IRN match anywhere within range is preferred over a merely
    number-shaped token nearer by, since exact IRN shape is a much
    stronger correctness signal than mere proximity for this field.
    Falls back to the loose (but digit-required -- see
    _IRN_FALLBACK_VALUE) shape only if no exact-shape candidate exists
    nearby, and that fallback is graded LOW rather than REVIEW: shape
    isn't confirmed, so correctness is materially less certain than the
    exact-match tier. Confirmed necessary empirically: a single-tier
    search using the loose shape directly picked up a plain word from
    the vendor's name when the real IRN was slightly further away than
    that word.
    """
    raw, _matched = find_value_near_label(
        rows, INVOICE_NUMBER_LABELS, r"[0-9A-Fa-f]{64}", exclude_values=exclude_values
    )
    if raw is not None:
        return raw, ConfidenceBand.REVIEW
    raw, _matched = find_value_near_label(
        rows, INVOICE_NUMBER_LABELS, _IRN_FALLBACK_VALUE, exclude_values=exclude_values
    )
    if raw is not None:
        return raw, ConfidenceBand.LOW
    return None, ConfidenceBand.NOT_FOUND


def _apply_spatial_fallback(
    rows: list[list[Word]],
    header_fields: dict,
    header_field_confidences: dict[str, ConfidenceBand],
    source: ExtractionSource,
    fields: dict[str, tuple[list[str], str]] = _SPATIAL_FALLBACK_FIELDS,
) -> None:
    """
    Mutates header_fields/header_field_confidences in place: for any field
    in `fields` that's still NOT_FOUND after the same-row regex pass,
    tries spatial_text.find_value_near_label as a second attempt --
    handles a label and its value genuinely being on different visual
    rows (not just reordered within one, which row reconstruction alone
    already fixes -- see module docstring). Always graded REVIEW: a
    spatial nearest-token guess is never as trustworthy as a clean
    same-line label:value match. invoice_number is handled separately
    beforehand (see _find_invoice_number_near_label) since it needs a
    stricter two-tier search, not the single generic pass used here.
    """
    # Values already resolved (by the same-row pass, an earlier fallback
    # field, or invoice_number's own separate fallback above) are
    # off-limits as candidates for a DIFFERENT field -- otherwise two
    # blank fields near an unrelated third field's real value can both
    # "steal" it just because it's the nearest token of the right shape.
    already_claimed = {str(v) for v in header_fields.values() if isinstance(v, str) and v}

    for field_name, (label_patterns, value_pattern) in fields.items():
        if header_field_confidences.get(field_name) != ConfidenceBand.NOT_FOUND:
            continue
        raw, _matched_label = find_value_near_label(
            rows, label_patterns, value_pattern, exclude_values=already_claimed
        )
        if raw is None:
            continue
        header_fields[field_name] = raw
        header_field_confidences[field_name] = _cap_confidence_for_source(
            field_name, raw, ConfidenceBand.REVIEW, source
        )
        already_claimed.add(raw)


def extract_invoice_group_fields(
    pdf_bytes: bytes,
    page_numbers: list[int],
    ocr_page_text_fn: Callable[[bytes], str] | None = None,
    on_page_text: PageTextRecorder | None = None,
    page_cache: PageCache | None = None,
    on_table_diagnostics: Callable[[dict], None] | None = None,
) -> tuple[dict, dict[str, ConfidenceBand], list[InvoiceLineItem]]:
    """
    Heavier per-group pass, run after invoice_grouping.py has already
    grouped pages into a single invoice. Returns (header_fields,
    header_field_confidences, line_items) -- shapes matching
    InvoiceGroup.header_fields / .header_field_confidences / .line_items.

    ocr_page_text_fn: optional callable (image bytes -> text). When given,
    any page in this group with no usable native text layer is rendered
    and OCR'd instead of contributing empty text. Omit it to keep the old
    native-only behavior exactly as before.

    on_page_text: optional callable (page_number, source, text), invoked
    the FIRST time each page is actually read/OCR'd -- a debugging hook
    only (see app/core/debug_store.py), never consulted for extraction
    itself. If `page_cache` already has an entry for a page (i.e.
    extract_page_invoice_numbers already processed it), this is NOT
    called again for that page -- it already fired once, when the page
    was first read.

    page_cache: optional shared dict (see _get_page_text_cached) -- pass
    the SAME dict used in an earlier extract_page_invoice_numbers call
    over (a superset of) these pages to reuse its results here instead of
    re-reading/re-OCR'ing every page a second time. Pass None (the
    default) to run standalone, e.g. in a test that doesn't call
    extract_page_invoice_numbers first -- behaves exactly as if every
    page were a cache miss.

    on_table_diagnostics: optional callable(dict), invoked once with a
    diagnostic snapshot of the line-item table extraction attempt for
    this group -- both the find_tables() stage and, if it ran, the
    header-row-driven positional fallback (header detection, matched
    column positions, computed boundaries, and the first few data rows'
    attempted column assignment). A debugging hook only, gated behind
    settings.DEBUG upstream (see GET /api/jobs/{job_id}/debug/table-parsing
    and app/core/debug_store.py) -- never consulted for extraction itself,
    and never built at all when this is None (see _extract_line_items_*'s
    own diagnostics params).

    Line items and tax_bracket_summary are always attempted via native
    PyMuPDF table-finding regardless of ocr_page_text_fn -- there is no OCR
    equivalent (see extract_header_fields_from_text's docstring); an
    image-only page simply won't have a find_tables()-detectable table, so
    this already degrades to "no tables found" for such pages with no
    special-casing needed.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        sorted_pages = sorted(page_numbers)
        cache: PageCache = page_cache if page_cache is not None else {}
        rows: list[list[Word]] | None = None
        general_rows: list[list[Word]] | None = None
        buyer_rows_for_fallback: list[list[Word]] | None = None

        all_words: list[Word] = []
        any_page_used_ocr = False
        page_texts_in_order: list[str] = []
        for page_number in sorted_pages:
            already_cached = page_number in cache
            text, source, words, _has_text_layer = _get_page_text_cached(doc, page_number, ocr_page_text_fn, cache)
            if not already_cached and on_page_text is not None:
                on_page_text(page_number, source, text)
            all_words.extend(words)
            page_texts_in_order.append(text)
            if source == ExtractionSource.OCR:
                any_page_used_ocr = True

        if any_page_used_ocr:
            # Conservative simplification: if ANY page in the group needed
            # OCR, the entire group's field-extraction is treated as
            # OCR-sourced for confidence-capping, even though some fields
            # may have actually come from a different, native-text page in
            # the same group. This avoids tracking per-field page
            # provenance through a single combined-text regex search --
            # and erring toward under-trusting is the safer direction for
            # a confidence signal. Worth reconsidering if mixed native+OCR
            # groups turn out to be common in practice. OCR-involved
            # groups don't get section-scoping this round either (words
            # is empty for an OCR'd page -- see _get_page_text_cached).
            combined_text = "\n".join(page_texts_in_order)
            buyer_text = None
            source = ExtractionSource.OCR
        else:
            rows = group_words_into_rows(all_words)
            general_rows, buyer_rows_for_fallback = _partition_rows(rows)
            # Segmented, not plain rows_to_text: real invoices lay out
            # several unrelated info blocks side-by-side on the same
            # visual row (buyer block next to invoice-metadata next to
            # transporter details) -- see rows_to_segmented_text's
            # docstring. Plain rows_to_text would let e.g. a "Consignee"
            # label's same-row value search sweep in a neighboring
            # "Type Of Sale" block's text just because Y-position merged
            # them into one row.
            combined_text = rows_to_segmented_text(general_rows)
            buyer_text = rows_to_segmented_text(buyer_rows_for_fallback)
            source = ExtractionSource.NATIVE_PDF_TEXT_LAYER

        header_fields, header_field_confidences = extract_header_fields_from_text(
            combined_text, source, buyer_text=buyer_text
        )
        if general_rows is not None:
            if header_field_confidences.get("invoice_number") == ConfidenceBand.NOT_FOUND:
                raw, band = _find_invoice_number_near_label(general_rows)
                if raw is not None:
                    header_fields["invoice_number"] = raw
                    header_field_confidences["invoice_number"] = _cap_confidence_for_source(
                        "invoice_number", raw, band, source
                    )
            _apply_spatial_fallback(general_rows, header_fields, header_field_confidences, source)

        if header_field_confidences.get("party_name") == ConfidenceBand.NOT_FOUND and rows is not None:
            # The vendor's "For <company>" signature line is searched
            # again here over ALL rows (not just general_rows), segmented
            # rather than plain-joined: confirmed against a real invoice
            # where the signature line shared a row with an unrelated
            # "Terms & Condition" label 400pt away, so the WHOLE row was
            # dropped by the "exclude" section (see EXCLUDE_SECTION_
            # HEADER_PATTERNS/_partition_rows) before combined_text was
            # ever built -- the signature was never excluded on its own
            # merits, just collateral damage from sharing a row with
            # something that was. _VENDOR_SIGNATURE_RE's own anchoring
            # (line-start "for", 2-80 char capture) keeps this safe to
            # run unscoped -- it isn't a generic word search.
            vendor_text = rows_to_segmented_text(rows)
            raw, band = _extract_vendor_name(vendor_text)
            if raw is not None:
                header_fields["party_name"] = raw
                header_field_confidences["party_name"] = _cap_confidence_for_source(
                    "party_name", raw, band, source
                )

        if (
            header_field_confidences.get("buyer_name") == ConfidenceBand.NOT_FOUND
            and buyer_rows_for_fallback is not None
            and len(buyer_rows_for_fallback) >= 2
        ):
            # A buyer section whose header is a standalone section title
            # (e.g. "BILL TO" on its own row, the actual name on the row
            # below -- rather than a same-row "Bill To: <name>" label) has
            # no same-row value for _extract_buyer_name to find at all.
            # buyer_rows_for_fallback[0] is the section-header row itself
            # (see _partition_rows); whatever immediately follows it is
            # presumed to be the buyer identification -- REVIEW, not HIGH,
            # since this is positional, not a label match. Each entry in
            # buyer_rows_for_fallback is already clipped to its leftmost
            # column segment by _partition_rows, so this can't sweep in an
            # unrelated side-by-side block (invoice-metadata/transporter
            # columns) sharing the same visual row.
            fallback_name = row_text(buyer_rows_for_fallback[1]).strip()
            if fallback_name:
                header_fields["buyer_name"] = fallback_name
                header_field_confidences["buyer_name"] = _cap_confidence_for_source(
                    "buyer_name", fallback_name, ConfidenceBand.REVIEW, source
                )

        tax_bracket_summary = _extract_tax_bracket_summary(doc, sorted_pages)
        header_fields["tax_bracket_summary"] = tax_bracket_summary
        header_field_confidences["tax_bracket_summary"] = (
            ConfidenceBand.HIGH if tax_bracket_summary else ConfidenceBand.NOT_FOUND
        )

        table_diagnostics: dict | None = {"group_pages": sorted_pages} if on_table_diagnostics is not None else None
        find_tables_diag = {} if table_diagnostics is not None else None
        line_items = _extract_line_items_from_tables(doc, sorted_pages, diagnostics=find_tables_diag)
        if table_diagnostics is not None:
            table_diagnostics["find_tables"] = find_tables_diag

        should_run_positional = not line_items and general_rows is not None
        positional_diag = None
        if should_run_positional:
            # Whole-group fork, checked ONCE before deciding how to scan
            # at all: a document using the batch-detail-row format (see
            # _extract_line_items_batch_detail_rows' module comment) is
            # run as ONE pooled pass over every page's rows together, NOT
            # per-page like the generic positional parser below. Reason:
            # this format's line items are pairs of adjacent PHYSICAL
            # rows (item row + detail row), and that pairing can span a
            # page break (confirmed on a real invoice -- the last item on
            # page 1 had its detail row printed as the first row of page
            # 2); per-page scoping would cut the pair apart and lose the
            # detail row's hsn/batch/expiry/mfg_date entirely. This is
            # safe to pool (unlike the generic parser -- see below):
            # _BATCH_DETAIL_ITEM_ROW_RE/_BATCH_DETAIL_ROW_RE are strict,
            # fully-anchored shape matches, not a loose column-boundary
            # guess, so a reprinted boilerplate row on page 2 can't
            # accidentally satisfy either pattern the way it could
            # accidentally land inside a column's X-range.
            uses_batch_detail_format = any(
                _BATCH_DETAIL_ROW_SIGNATURE_RE.search(row_text(row)) for row in general_rows if row
            )
            if uses_batch_detail_format:
                batch_diag = {} if table_diagnostics is not None else None
                line_items = _extract_line_items_batch_detail_rows(general_rows, diagnostics=batch_diag)
                if table_diagnostics is not None:
                    positional_diag = {"pages": [batch_diag], "line_items_produced": len(line_items)}
            else:
                # Run PER PAGE, not once over the whole group's pooled rows
                # -- a multi-page invoice commonly reprints its full
                # header/customer/address block at the top of every
                # continuation page (confirmed against a real 4-page
                # invoice). Pooling every page's rows into one continuous
                # scan meant that block was scanned using page 1's column
                # boundaries once the real table ended, and enough of its
                # words happened to fall inside those boundaries by
                # X-coincidence to be emitted as garbage line items (e.g.
                # "GSTIN", "DESCRIPTION" itself) instead of being
                # recognized as off-table content and stopped on. Scoping
                # the header-detect/parse/stop cycle to one page at a time
                # means each page's own boilerplate can only ever run into
                # ITS OWN table-end detection, not bleed into the next.
                pages_with_rows = sorted({row[0].page_number for row in general_rows if row})
                per_page_diags = [] if table_diagnostics is not None else None
                positional_items: list[InvoiceLineItem] = []
                line_number_offset = 0
                for pg in pages_with_rows:
                    page_rows = [row for row in general_rows if row and row[0].page_number == pg]
                    page_diag = {} if per_page_diags is not None else None
                    page_items = _extract_line_items_positional(page_rows, diagnostics=page_diag)
                    for item in page_items:
                        item.line_number += line_number_offset
                    line_number_offset += len(page_items)
                    positional_items.extend(page_items)
                    if per_page_diags is not None:
                        page_diag["page_number"] = pg
                        per_page_diags.append(page_diag)
                line_items = positional_items
                if table_diagnostics is not None:
                    positional_diag = {"pages": per_page_diags, "line_items_produced": len(line_items)}
        if table_diagnostics is not None:
            table_diagnostics["positional_parser"] = positional_diag
            table_diagnostics["positional_parser_ran"] = should_run_positional
            table_diagnostics["final_line_item_count"] = len(line_items)
            on_table_diagnostics(table_diagnostics)
    finally:
        doc.close()

    # Applied once, centrally, to every produced item regardless of which
    # extraction path (find_tables() or the positional fallback, on any
    # page) built it -- see _split_description_and_pack. Skipped for an
    # item that already has a pack (the batch-detail-row format's own
    # parser sets it via a DIFFERENT rule, _split_description_and_pack_
    # trailing_number -- this would otherwise unconditionally overwrite
    # that with this rule's result, which is None whenever there's no
    # hyphen, silently discarding a correctly-split pack).
    for item in line_items:
        if item.pack is None:
            item.item_description, item.pack = _split_description_and_pack(item.item_description)

    return header_fields, header_field_confidences, line_items
