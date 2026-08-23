"""
Source-agnostic, layout-aware text reconstruction: turns a flat stream of
"words with (x, y) coordinates" into visually-ordered rows, and provides
generic section segmentation over those rows. Used by BOTH the native-PDF
path (app/services/native_pdf_extraction.py, words from PyMuPDF's
page.get_text("words")) and the OCR path (app/services/ocr_extraction.py,
words from pytesseract.image_to_data) -- same underlying problem (a flat
word stream whose stream/reading order doesn't reliably match visual
position) regardless of source, so it lives here once instead of being
reimplemented per source.

Nothing in this module knows anything about invoices, labels, or field
names -- it operates purely on (text, x0, y0, x1, y1) tuples and
caller-supplied pattern lists. That genericity is deliberate: the caller
(native_pdf_extraction.py) supplies what a "label" or a "section boundary"
looks like for the format it's parsing; this module just does the spatial
arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    """One recognized word/token with its bounding box. `page_number` is
    1-indexed and lets a single row-clustering pass span multiple pages
    (a group's rows are always kept separate per page -- see
    group_words_into_rows)."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page_number: int = 1

    @property
    def top(self) -> float:
        return self.y0

    @property
    def left(self) -> float:
        return self.x0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2


def group_words_into_rows(words: list[Word], row_tolerance_ratio: float = 0.6) -> list[list[Word]]:
    """
    Clusters words into visual rows purely by (page, vertical position),
    in true top-to-bottom / left-to-right geometric order -- NOT the
    stream/paint order the words were originally produced in. This is the
    key fix for content whose stream order doesn't match its visual order
    (a generated PDF that paints a value before its label, or paints a
    row's labels and values as separate text runs): sorting and grouping
    by real (page, top, left) coordinates reconstructs the order a human
    reader would actually see, regardless of what order the source
    (PyMuPDF word list, Tesseract word boxes) originally handed them to us
    in.

    row_tolerance_ratio: how close two words' vertical centers need to be
    (relative to word height) to count as "the same row". FRAGILE: a fixed
    ratio, not adaptive to a document's specific line spacing -- too tight
    and a single row splits in two; too loose and two visually-close but
    genuinely separate rows merge into one.
    """
    if not words:
        return []

    ordered = sorted(words, key=lambda w: (w.page_number, w.top, w.left))
    rows: list[list[Word]] = [[ordered[0]]]
    row_running_top = ordered[0].top
    row_page = ordered[0].page_number

    for word in ordered[1:]:
        tolerance = max(rows[-1][-1].height, word.height) * row_tolerance_ratio
        same_page = word.page_number == row_page
        if same_page and abs(word.top - row_running_top) <= tolerance:
            rows[-1].append(word)
            row_running_top = sum(w.top for w in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            row_running_top = word.top
            row_page = word.page_number

    return rows


def row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in sorted(row, key=lambda w: w.left))


def rows_to_text(rows: list[list[Word]]) -> str:
    return "\n".join(row_text(row) for row in rows)


def split_row_into_column_segments(row: list[Word], gap_threshold: float = 50.0) -> list[list[Word]]:
    """
    Splits one visual row into left-to-right word clusters, breaking
    wherever the horizontal gap between two consecutive words exceeds
    `gap_threshold`. Real invoices commonly lay out several unrelated
    info blocks side-by-side on the same visual row (e.g. "Bill To" /
    "Consignee Details" / "Transporter Details" columns, or a "Terms &
    Conditions" block next to a vendor's "For <company>" signature) --
    since group_words_into_rows only clusters by Y-position, these all
    land in one row, and a same-line label:value search over that row's
    full joined text can accidentally sweep a neighboring block's words
    into a match's value (or hide a real match inside an unrelated
    block's excluded/matched text). Confirmed necessary against two real
    invoices where side-by-side blocks were 130-400pt apart -- well past
    normal inter-word spacing (a few pt) within one block.

    gap_threshold=50.0 is a fixed default, not adaptive to a document's
    own font size/column spacing -- large enough that ordinary label:value
    spacing (e.g. "Bill To:    Acme Corp") never splits, small enough to
    separate genuinely distinct side-by-side blocks in the real documents
    this was built against. A row with no large gaps returns one segment
    (itself), so this is a no-op on any single-column layout.
    """
    words_sorted = sorted(row, key=lambda w: w.x0)
    if not words_sorted:
        return []
    segments: list[list[Word]] = [[words_sorted[0]]]
    for word in words_sorted[1:]:
        prev = segments[-1][-1]
        if word.x0 - prev.x1 > gap_threshold:
            segments.append([word])
        else:
            segments[-1].append(word)
    return segments


def leftmost_column_segment(row: list[Word], gap_threshold: float = 50.0) -> list[Word]:
    """The leftmost side-by-side block of a row (see
    split_row_into_column_segments) -- for fields known to always be the
    first/leftmost of several blocks sharing a row (e.g. a buyer/receiver
    block that's visually first, with invoice-metadata/transporter
    columns to its right)."""
    segments = split_row_into_column_segments(row, gap_threshold)
    return segments[0] if segments else []


def rows_to_segmented_text(rows: list[list[Word]], gap_threshold: float = 50.0) -> str:
    """Like rows_to_text, but each row is first split into column
    segments (see split_row_into_column_segments), with each segment
    becoming its OWN line in the output. Same-line label:value search
    (_search_labeled_value and friends) then naturally can't cross
    between two side-by-side blocks that only appear on the same line
    because of Y-proximity, not because they're actually related."""
    lines: list[str] = []
    for row in rows:
        for segment in split_row_into_column_segments(row, gap_threshold):
            lines.append(row_text(segment))
    return "\n".join(lines)


@dataclass(frozen=True)
class Section:
    """A contiguous span of rows [row_start, row_end) whose start was
    triggered by a row matching one of the caller's section-marker
    patterns. `kind` is whatever label the caller used for that pattern
    group (e.g. "buyer", "exclude") -- this module attaches no meaning to
    it beyond grouping."""

    kind: str
    row_start: int
    row_end: int
    matched_pattern: str


def segment_sections(
    rows: list[list[Word]],
    section_patterns: dict[str, list[str]],
    max_span_rows: dict[str, int] | None = None,
    hard_stop_patterns: dict[str, list[str]] | None = None,
) -> list[Section]:
    """
    Generic document segmentation: for each row, checks whether its text
    matches ANY pattern from ANY kind in `section_patterns` (a dict of
    {kind: [regex, ...]} supplied entirely by the caller -- this function
    has no built-in notion of what a "buyer section" or an "exclude
    section" is). Each match starts a new Section of that kind, running
    until the next matched row (of ANY kind) or end of the document.

    Two independent, optional ways to close a section EARLIER than that
    natural end, since "the next section marker" is often too far away
    (or never comes) on a real document:
      - max_span_rows[kind]: a flat row-count cap -- a safety net for
        section kinds that are only ever a few rows (e.g. a customer-
        details block), so an unbounded span can't swallow unrelated
        content (the line-item table, totals) if no other marker happens
        to follow before end of document.
      - hard_stop_patterns[kind]: regexes that, if matched by a row INSIDE
        an active section of that kind, close the section immediately at
        that row (the matching row itself is excluded) -- for content
        that's a reliable signal "this is clearly something else now"
        even before the row-count cap is reached (e.g. a totals label or
        the vendor's signature line appearing while still technically
        within a buyer section's row-count budget).
    Rows before the first match belong to no section at all -- callers
    typically treat that as the "default" pool.
    """
    max_span_rows = max_span_rows or {}
    hard_stop_patterns = hard_stop_patterns or {}
    markers: list[tuple[int, str, str]] = []  # (row_index, kind, matched_pattern)

    for row_idx, row in enumerate(rows):
        text = row_text(row)
        for kind, patterns in section_patterns.items():
            match = None
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    break
            if match:
                markers.append((row_idx, kind, match.group(0)))
                break  # first matching kind on this row wins; a row is one marker at most

    sections: list[Section] = []
    for i, (row_idx, kind, matched_pattern) in enumerate(markers):
        natural_end = markers[i + 1][0] if i + 1 < len(markers) else len(rows)
        cap = max_span_rows.get(kind)
        row_end = min(natural_end, row_idx + cap) if cap is not None else natural_end

        stops = hard_stop_patterns.get(kind)
        if stops:
            for candidate_idx in range(row_idx + 1, row_end):
                candidate_text = row_text(rows[candidate_idx])
                if any(re.search(pattern, candidate_text, re.IGNORECASE) for pattern in stops):
                    row_end = candidate_idx
                    break

        sections.append(Section(kind=kind, row_start=row_idx, row_end=max(row_end, row_idx + 1), matched_pattern=matched_pattern))

    return sections


def row_indices_in_sections(sections: list[Section], kinds: set[str]) -> set[int]:
    indices: set[int] = set()
    for section in sections:
        if section.kind in kinds:
            indices.update(range(section.row_start, section.row_end))
    return indices


def find_value_near_label(
    rows: list[list[Word]],
    label_patterns: list[str],
    value_pattern: str,
    max_row_distance: int = 3,
    exclude_values: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """
    Fallback for when a label and its value are NOT on the same visual
    row -- e.g. a document that paints a "values block" separately from
    its "labels block" (both internally row-consistent, but at different
    y-positions rather than interleaved). Finds the first row containing a
    label match, then searches nearby rows (below first, since that's the
    far more common layout direction, then above) for the nearest single
    word matching value_pattern in full.

    Deliberately single-word-only: this is a fallback for short
    token-shaped values (an ID number, a code) that survived as one word
    from the source's word-tokenization -- not a general-purpose
    multi-word value extractor (same-row _search_labeled_value-style
    matching already covers multi-word values; this only needs to handle
    the "value is a lone token on a nearby row" case).

    exclude_values: candidate tokens equal (after stripping) to any value
    in this set are skipped -- used so a value already claimed by a
    DIFFERENT field's clean same-row match can't also get grabbed by this
    field's nearest-token search just because it happens to be nearby
    (e.g. two blank numeric-code fields on adjacent rows, one of which sits
    close to a THIRD, unrelated field's already-resolved value).

    Returns (value, matched_label_text) or (None, None). The caller
    decides confidence -- this function only reports whether a positional
    match was found, since "found via spatial proximity, not a same-line
    label:value pair" should never be graded as confidently as a clean
    same-row match regardless of which field it is.
    """
    value_re = re.compile(rf"^(?:{value_pattern})$")
    exclude_values = exclude_values or set()

    for row_idx, row in enumerate(rows):
        text = row_text(row)
        label_match = None
        for label in label_patterns:
            label_match = re.search(rf"(?:{label})", text, re.IGNORECASE)
            if label_match:
                break
        if label_match is None:
            continue

        candidate_row_indices = list(range(row_idx + 1, min(row_idx + 1 + max_row_distance, len(rows))))
        candidate_row_indices += list(range(row_idx - 1, max(row_idx - 1 - max_row_distance, -1), -1))
        for candidate_idx in candidate_row_indices:
            for word in sorted(rows[candidate_idx], key=lambda w: w.left):
                stripped = word.text.strip().strip(":-").strip()
                if stripped and stripped not in exclude_values and value_re.match(stripped):
                    return stripped, label_match.group(0)
        return None, label_match.group(0)  # label found, but no nearby value -- legitimate NOT_FOUND

    return None, None


# ---------------------------------------------------------------------------
# Header-row-driven columnar table parsing -- generic: the caller supplies
# {field_name: [keyword, ...]}, this module only does the geometry (finding
# the header row, deriving column X-ranges from it, bucketing subsequent
# rows' words into those ranges). No fixed column count or order is assumed
# anywhere here -- both come entirely from whatever header row is actually
# found in a given document.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnBoundary:
    field_name: str
    left: float
    right: float
    header_text: str


def match_row_columns(row: list[Word], column_keywords: dict[str, list[str]]) -> tuple[dict[str, Word], list[Word]]:
    """
    Tries to match each field's keywords against this row's words. Each
    keyword is checked against a window sized to THAT keyword's own word
    count (e.g. "taxable" checks single words only; "cgst rate" checks
    2-word windows only) -- NOT a fixed 1-3-word sliding scan independent
    of the keyword's own length. That distinction matters: an early
    version of this function tried wider windows first regardless of
    keyword length, which let a short keyword like "taxable" match INSIDE
    a wider window spanning several unrelated adjacent single-word headers
    (e.g. "Qty Rate Taxable" as one 3-word blob, because the joined phrase
    happens to contain the substring "taxable") -- silently merging three
    separate columns into one. Sizing the window to each keyword's own
    length prevents that: "taxable" (one word) can now only ever match a
    single word.

    A window of size 1 (does any SINGLE word's own text already contain
    the full keyword phrase) is always tried FIRST, before the keyword's
    natural multi-word window, regardless of the keyword's own length.
    This matters for a row containing compound words produced by
    merge_header_subrow (e.g. "CGST %", from folding a 2-physical-line
    header together): a 2-word keyword like "cgst %" must match that ONE
    already-compound word directly, not go on to consume it plus an
    unrelated NEIGHBORING compound word (e.g. "DISC %" + "CGST %") as one
    4-token window just because the keyword's own length is 2 -- confirmed
    to silently corrupt column boundaries on a real invoice (the resulting
    "window" spanned two genuinely different columns' positions). For a
    row of ordinary single-token words (the common case), the window-1
    attempt simply never matches a multi-word keyword and falls through to
    the original multi-word window search unchanged.

    Iterates column_keywords in the CALLER's dict order, so compound/
    specific keys (e.g. "cgst_rate") get first claim on a word before
    generic fallbacks (e.g. "unit_rate") -- same priority convention
    already used by _map_table_header_columns for the find_tables()-based
    path. Keywords within one field's own list are also tried in the
    CALLER's given order (existing convention already lists longer/more
    specific phrasings first, e.g. "cgst amt" before bare "cgst"). A word
    can only be claimed by one field.
    """
    words_sorted = sorted(row, key=lambda w: w.x0)
    used = [False] * len(words_sorted)
    matches: dict[str, Word] = {}

    for field_name, keywords in column_keywords.items():
        if field_name in matches:
            continue
        found_window = None
        for keyword in keywords:
            natural_window_size = len(keyword.split())
            window_sizes = (1,) if natural_window_size == 1 else (1, natural_window_size)
            for window_size in window_sizes:
                for i in range(len(words_sorted) - window_size + 1):
                    if any(used[i : i + window_size]):
                        continue
                    window = words_sorted[i : i + window_size]
                    phrase = " ".join(w.text for w in window).strip().lower()
                    if keyword in phrase:
                        found_window = (i, window_size, window)
                        break
                if found_window:
                    break
            if found_window:
                break
        if found_window is None:
            continue
        i, size, window = found_window
        for k in range(i, i + size):
            used[k] = True
        matches[field_name] = Word(
            text=" ".join(w.text for w in window),
            x0=min(w.x0 for w in window),
            y0=min(w.y0 for w in window),
            x1=max(w.x1 for w in window),
            y1=max(w.y1 for w in window),
            page_number=window[0].page_number,
        )

    unmatched = [word for i, word in enumerate(words_sorted) if not used[i]]
    return matches, unmatched


def merge_header_subrow(
    primary: list[Word], sub: list[Word], max_x_distance: float = 20.0
) -> tuple[list[Word], list[Word]]:
    """
    Merges a two-physical-line table header into one pseudo-row for
    match_row_columns: real invoices commonly split a column's label
    across two lines -- a group word on the header row itself ("CGST",
    "OLD", "SALE") and a disambiguating sub-label directly below it
    ("%", "MRP", "QTY") -- and the group word ALONE is often genuinely
    ambiguous (e.g. bare "CGST" could be a rate or an amount column;
    "OLD" alone matches no keyword at all). Confirmed against a real
    invoice where, without this, "CGST"/"SGST"/"IGST" fell back to the
    wrong field (amount instead of rate) and "OLD"/"NEW"/"SALE" matched
    nothing at all, leaving MRP and quantity blank despite the data being
    right there in the table.

    Pairing is GLOBAL greedy nearest-first (every primary/sub pair within
    max_x_distance, sorted by distance, claimed in that order) -- NOT each
    primary word independently grabbing its own nearest available sub
    word in left-to-right order. The latter is order-dependent and can
    steal a sub word out from under its true (closer) match: e.g. "PTS"
    sitting 36pt from "QTY" would claim it first just by being processed
    earlier, even though "SALE" sits almost exactly above "QTY" (~0pt) --
    confirmed against a real invoice where this produced a bogus "PTS
    QTY" pairing and left the real "SALE"/"QTY" column (quantity) with no
    sub-label match at all. Global nearest-first ensures the truly
    closest pair is always claimed first regardless of scan order.

    Each claimed pair is combined into one compound Word ("OLD" + "MRP"
    -> "OLD MRP") positioned at the primary word's coordinates -- so
    match_row_columns's per-keyword window search can match compound
    phrases like "old mrp"/"cgst %"/"sale qty" that only exist once both
    physical lines are read together. A primary word with no close-enough
    sub word is passed through unchanged.

    Returns (merged, orphans): `merged` is the primary-derived pseudo-row
    (one entry per primary word, standalone or compounded); `orphans` is
    every leftover unpaired sub word (no primary word close enough, or
    already claimed by a closer primary word), returned SEPARATELY rather
    than appended into `merged` -- deliberately, even though an orphan
    might be a legitimate lone keyword match on its own (e.g. a bare
    "SOLD" sub-label with no primary counterpart). Confirmed harmful to
    fold orphans directly into `merged` against a real invoice: a 3rd
    sub-column with no primary word of its own (a quantity table's
    "Total" sold+free column, sharing its row with an unrelated genuine
    "TOTAL" primary header elsewhere on the SAME row) sat to the LEFT of
    that genuine "TOTAL" column and won a same-keyword match ahead of it
    purely by X-position, silently stealing the line-amount column's
    data. The caller is expected to run match_row_columns on `merged`
    FIRST, then a lower-priority fallback pass over `orphans` for any
    field the first pass didn't already claim -- see
    _extract_line_items_positional -- so an orphan can still resolve a
    field nothing else claims, without being able to outrank a genuine
    primary-row column for a field both could plausibly match. Orphans
    the fallback pass still doesn't claim remain useful as boundary
    anchors (see compute_column_boundaries) even though they matched no
    field -- omitting them entirely (instead of returning them at all)
    was ALSO confirmed harmful: without "Total" as an anchor, MRP's
    computed left edge crept far enough left to swallow a neighboring
    quantity value into the same cell.
    """
    candidates = sorted(
        (
            (abs(sub_word.x_center - word.x_center), p_idx, s_idx)
            for p_idx, word in enumerate(primary)
            for s_idx, sub_word in enumerate(sub)
            if abs(sub_word.x_center - word.x_center) < max_x_distance
        )
    )
    paired_sub_for_primary: dict[int, int] = {}
    used_primary: set[int] = set()
    used_sub: set[int] = set()
    for _dist, p_idx, s_idx in candidates:
        if p_idx in used_primary or s_idx in used_sub:
            continue
        paired_sub_for_primary[p_idx] = s_idx
        used_primary.add(p_idx)
        used_sub.add(s_idx)

    merged: list[Word] = []
    for p_idx, word in enumerate(primary):
        if p_idx not in paired_sub_for_primary:
            merged.append(word)
            continue
        sub_word = sub[paired_sub_for_primary[p_idx]]
        merged.append(
            Word(
                text=f"{word.text} {sub_word.text}",
                x0=min(word.x0, sub_word.x0),
                y0=word.y0,
                x1=max(word.x1, sub_word.x1),
                y1=word.y1,
                page_number=word.page_number,
            )
        )
    orphans = [sub_word for s_idx, sub_word in enumerate(sub) if s_idx not in used_sub]
    return merged, orphans


def detect_header_row(
    rows: list[list[Word]],
    column_keywords: dict[str, list[str]],
    min_matched_columns: int = 3,
    row_range: range | None = None,
) -> tuple[int, dict[str, Word], list[Word]] | None:
    """
    Scans rows (optionally restricted to `row_range`) for the one most
    likely to be a header row: the row matching the most distinct columns
    (must be >= min_matched_columns to count as a header at all, so a
    stray row that happens to contain one or two keyword-ish words doesn't
    get mistaken for a real table header). Ties go to the earliest/best
    row seen. Returns (row_index, {field_name: header_word}, unmatched_words)
    or None if no row cleared the threshold -- unmatched_words (the header
    row's OWN words that didn't match any known field) is passed straight
    through to compute_column_boundaries, so an unrecognized column
    between two recognized ones still gets its own boundary carved out
    rather than that gap being split between its two neighbors (see that
    function's docstring).
    """
    search_rows = enumerate(rows) if row_range is None else ((i, rows[i]) for i in row_range if i < len(rows))
    best: tuple[int, dict[str, Word], list[Word]] | None = None
    for idx, row in search_rows:
        matched, unmatched = match_row_columns(row, column_keywords)
        if len(matched) >= min_matched_columns and (best is None or len(matched) > len(best[1])):
            best = (idx, matched, unmatched)
    return best


def compute_column_boundaries(matches: dict[str, Word], unmatched_words: list[Word] | None = None) -> list[ColumnBoundary]:
    """
    Derives each column's X-range from its header word's bbox: the
    boundary between two adjacent columns is the midpoint between them, so
    a data word gets assigned to whichever header it's geometrically
    closer to. The first column's left edge and the last column's right
    edge are left open (-inf/+inf) since a data cell can legitimately
    start left of / extend right of its own header text.

    unmatched_words: other words on the SAME header row that didn't match
    any known field (see detect_header_row) -- included as extra boundary
    anchors (but never given their own ColumnBoundary/field name) so an
    unrecognized column sitting BETWEEN two recognized ones gets a
    correctly-sized gap carved out for it, rather than that whole gap
    being split down the middle between its two neighbors. Confirmed
    necessary empirically: without this, a data value under an
    unrecognized middle column could have its rendered width put its
    center past the naive midpoint, silently landing in -- and
    corrupting -- an adjacent recognized column's cell instead of being
    correctly excluded from both.
    """
    matched_ordered = sorted(matches.items(), key=lambda kv: kv[1].x0)
    all_anchors = sorted(
        [word for _field, word in matched_ordered] + list(unmatched_words or []), key=lambda w: w.x0
    )

    boundaries: list[ColumnBoundary] = []
    for field_name, word in matched_ordered:
        anchor_idx = all_anchors.index(word)
        prev_anchor = all_anchors[anchor_idx - 1] if anchor_idx > 0 else None
        next_anchor = all_anchors[anchor_idx + 1] if anchor_idx + 1 < len(all_anchors) else None
        left = float("-inf") if prev_anchor is None else (prev_anchor.x1 + word.x0) / 2
        right = float("inf") if next_anchor is None else (word.x1 + next_anchor.x0) / 2
        boundaries.append(ColumnBoundary(field_name=field_name, left=left, right=right, header_text=word.text))
    return boundaries


def assign_row_to_columns(row: list[Word], boundaries: list[ColumnBoundary]) -> dict[str, str]:
    """Buckets each word in `row` into whichever column's X-range contains
    its horizontal center, then joins each bucket's words (in left-to-right
    order) into that column's cell text. A column with no words in this
    row is simply absent from the result -- the caller treats that as a
    legitimately blank cell, not an error."""
    cells: dict[str, list[Word]] = {}
    for word in row:
        for boundary in boundaries:
            if boundary.left <= word.x_center < boundary.right:
                cells.setdefault(boundary.field_name, []).append(word)
                break
    return {field_name: row_text(words) for field_name, words in cells.items()}
