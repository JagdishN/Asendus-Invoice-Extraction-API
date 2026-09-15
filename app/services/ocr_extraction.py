"""
OCR-based text extraction for invoice content with no usable native text
layer -- directly-uploaded scanned/photographed JPEG/PNG/WEBP images, and
PDF pages that native_pdf_extraction.py's text-layer detection
(has_usable_text_layer) flags as scanned. Feeds the EXACT SAME
label-matching/field-mapping logic already built for native PDFs
(native_pdf_extraction.extract_header_fields_from_text) -- this module
only owns "get text out of an image"; it does not duplicate any label
patterns.

Requires the Tesseract OCR engine installed at the OS level -- pytesseract
is only a thin Python wrapper around the `tesseract` binary, NOT the OCR
engine itself. Without it, any call here raises
pytesseract.TesseractNotFoundError at runtime, not import time. Install:
    Debian/Ubuntu (deployment target): apt-get install -y tesseract-ocr
    Windows (local dev):               winget install --id UB-Mannheim.TesseractOCR -e
See _configure_tesseract_cmd() below for why Windows needs a little help
finding it even after that install.

Reading order for tabular/form-style invoices (accuracy pass):
    Tesseract's plain image_to_string() reading order is line-detection
    driven and can interleave a multi-column form's columns/label-value
    pairs in the wrong order -- e.g. a label in one column and its value in
    another column on the SAME visual row can end up on different output
    lines, or separated by an unrelated word from a different column,
    which breaks this codebase's label-matching (_search_labeled_value is
    deliberately same-line-only, see native_pdf_extraction.py). Instead,
    this module calls pytesseract.image_to_data() to get each recognized
    word's bounding box, clusters words into rows purely by their vertical
    (top) position (_group_words_into_rows), and joins each row
    left-to-right by horizontal (left) position -- reconstructing "words
    that are visually near each other" rather than trusting Tesseract's own
    line/paragraph/block segmentation. This is a real accuracy improvement
    for dense forms, NOT a full layout engine: it does not detect columns,
    does not know about ruled table lines, and a row that's visually
    ambiguous (e.g. two adjacent narrow columns with similar row spacing)
    can still merge or split wrong. See preprocess_image_for_ocr and
    DEFAULT_PSM for the other accuracy levers (deskew, resolution/sharpening,
    page segmentation mode).

KNOWN LIMITATION -- line items and the tax-bracket summary table are NOT
extracted from OCR text. Both of those, for native PDFs, come from
PyMuPDF's find_tables(), which reads the PDF's actual vector/ruling-line
object model -- a structural feature of the PDF file, not something OCR's
flat output text has any equivalent of. Reconstructing table structure
from OCR would need real column-clustering on top of the row-clustering
already done here, which is a separate, meaningfully larger effort not
attempted here. OCR-sourced invoices get header fields but always an empty
line_items list -- consistent with this codebase's existing "don't guess"
philosophy for tables it can't confidently detect.

Confidence: every field extracted from OCR text is capped at REVIEW (LOW
for a malformed IRN) by native_pdf_extraction._cap_confidence_for_source --
never HIGH. See that function's docstring for why. That did NOT change
with the row-reconstruction accuracy pass above -- better raw text can mean
label-matching finds more fields, but OCR is still capped the same way
regardless of how clean any individual read turned out.
"""

from __future__ import annotations

import io
import logging
import os
import shutil

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
from pytesseract import Output

from app.core.config import settings
from app.models.schemas import ConfidenceBand, InvoiceLineItem

# _cap_confidence_for_source/_extract_invoice_number are underscore-prefixed
# ("module-private" by convention) but deliberately imported here anyway --
# this module exists specifically to reuse native_pdf_extraction.py's
# label-matching without duplicating it, so pulling in its single-field
# helpers is the intended shape, not a layering violation. The genuinely
# public/shared entry point is extract_header_fields_from_text.
from app.services.native_pdf_extraction import (
    ExtractionSource,
    _cap_confidence_for_source,
    _extract_invoice_number,
    extract_header_fields_from_text,
)

logger = logging.getLogger(__name__)

# 150-200 DPI is a reasonable starting point for OCR accuracy on typical
# invoice text sizes without being unnecessarily slow. FRAGILE: not tuned
# against a real scanned/photographed sample yet -- native_pdf_extraction's
# render_page_to_image_bytes() uses this same default for PDF pages.
DEFAULT_RENDER_DPI = 200

# See app/core/config.py's ocr_psm for the measured comparison behind
# this default (4, "single column of variable-size text").
DEFAULT_PSM = settings.ocr_psm

_WINDOWS_DEFAULT_TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _configure_tesseract_cmd() -> None:
    """
    On the deployment target (Linux, via apt-get install tesseract-ocr),
    the binary lands on PATH automatically and pytesseract finds it with
    no extra config. On Windows local dev, the winget/installer package
    doesn't necessarily update PATH for an already-open shell, so fall
    back to the default install location if it's not already resolvable.
    No-op (and harmless) everywhere else.
    """
    if shutil.which("tesseract"):
        return
    if os.path.isfile(_WINDOWS_DEFAULT_TESSERACT_PATH):
        pytesseract.pytesseract.tesseract_cmd = _WINDOWS_DEFAULT_TESSERACT_PATH


_configure_tesseract_cmd()


# Most directly-uploaded photos/screenshots carry no DPI metadata at all
# (PIL's image.info won't have a "dpi" key), so effective resolution is
# estimated from pixel width against an assumed page width. 8.5" covers
# US Letter; A4 is close enough (8.27") that this doesn't need to
# distinguish between them for a rough estimate.
_ASSUMED_PAGE_WIDTH_INCHES = 8.5
_TARGET_EFFECTIVE_DPI = 300
_MAX_UPSCALE_FACTOR = 3.0

# Skew below this is noise (JPEG/photo capture jitter, not a real tilt) --
# correcting it would just add rotation-interpolation blur for no benefit.
# Above this, distrust the detection: a real hand-photographed invoice is
# typically tilted a few degrees, not tens of degrees, so a larger reading
# more likely means the line-detection picked up something that isn't
# actually page/text tilt (a diagonal fold shadow, a stray mark) --
# rotating on that basis would make a merely-tilted photo worse, not
# better.
_MIN_DESKEW_ANGLE_DEGREES = 0.5
_MAX_DESKEW_ANGLE_DEGREES = 15.0

# Only near-horizontal detected lines are treated as candidate evidence of
# page tilt (table rules, ruled lines, text baselines) -- a genuinely
# vertical or steeply-angled line is more likely a column divider, a
# staple/fold shadow, or unrelated photo content, not something a
# correctly-oriented invoice photo has many of.
_HOUGH_ANGLE_TOLERANCE_DEGREES = 15.0

# Below this many candidate lines, there isn't enough agreement to trust a
# single median angle -- e.g. a mostly-blank, very sparse, or corrupt
# image -- so skip deskewing rather than rotate based on one or two lines
# that might not represent the page's real orientation at all.
_MIN_HOUGH_LINES_FOR_DESKEW = 8


def _estimate_effective_dpi(image: Image.Image) -> float:
    dpi_metadata = image.info.get("dpi")
    if dpi_metadata and dpi_metadata[0]:
        return float(dpi_metadata[0])
    return image.width / _ASSUMED_PAGE_WIDTH_INCHES


def _deskew_image(image: Image.Image) -> Image.Image:
    """
    Corrects small ROTATIONAL skew -- the image's content is tilted a few
    degrees, the common case for a hand-photographed (rather than flatbed-
    scanned) invoice -- via Hough line detection: find straight edge
    segments (Canny + HoughLinesP), keep the ones close to horizontal
    (table rules, ruled lines, text baselines -- see
    _HOUGH_ANGLE_TOLERANCE_DEGREES), and rotate by their median angle.

    An earlier version of this function used cv2.minAreaRect over the
    whole thresholded "ink" mask instead of Hough lines. Measured directly
    against the real test photos this was built for: it degenerates to a
    ~0-degree reading whenever ink pixels are scattered across most of the
    frame (dense text + handwritten margin notes + a decorative border --
    exactly what real invoice photos look like), because the minimum-area
    rectangle around a near-full-frame scattered mask just comes out
    axis-aligned regardless of the actual visual tilt. Hough line detection
    instead measures the angle of concrete straight features and was
    verified (via a synthetic known-angle rotation test against a real
    sample image) to correctly recover a 3-6 degree induced tilt down to
    under 1 degree residual -- the minAreaRect version recovered nothing.

    Deliberately does NOT attempt full 4-corner perspective/keystone
    correction (detecting the document's four physical corners and warping
    them into a rectangle, the "phone document scanner" effect) -- that
    needs a page boundary reliably distinguishable from its background,
    which a real test photo (invoice paper cropped tight in frame,
    similar-toned surface, folds/creases) doesn't reliably offer. A wrong
    4-corner guess warps the image into something worse than just leaving
    it tilted; a wrong rotation angle in this simpler approach at worst
    leaves it tilted by roughly the same amount it already was.

    Every failure mode here (no/too few candidate lines, an out-of-range
    angle, any cv2 error) falls back to returning the original image
    unchanged and logs at DEBUG rather than raising -- this is a best-
    effort accuracy improvement layered in front of OCR, not a step that's
    allowed to turn a previously-working image into a worse one, or to
    take down the OCR call it precedes.
    """
    try:
        gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        min_line_length = gray.shape[1] // 4
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180, threshold=100, minLineLength=min_line_length, maxLineGap=20
        )
        if lines is None:
            return image

        candidate_angles = []
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            line_angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if abs(line_angle) <= _HOUGH_ANGLE_TOLERANCE_DEGREES:
                candidate_angles.append(line_angle)
        if len(candidate_angles) < _MIN_HOUGH_LINES_FOR_DESKEW:
            return image

        angle = float(np.median(candidate_angles))
        if abs(angle) < _MIN_DESKEW_ANGLE_DEGREES or abs(angle) > _MAX_DESKEW_ANGLE_DEGREES:
            return image

        height, width = gray.shape
        rotation_matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        rotated = cv2.warpAffine(
            np.array(image.convert("RGB")),
            rotation_matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        logger.debug("Deskewed image by %.2f degrees (%d candidate lines)", angle, len(candidate_angles))
        return Image.fromarray(rotated)
    except Exception:
        logger.debug("Deskew step failed; using original image unchanged", exc_info=True)
        return image


def _upscale_if_low_resolution(image: Image.Image) -> Image.Image:
    """
    Tesseract accuracy drops off noticeably below ~300 DPI-equivalent for
    typical invoice text sizes. FRAGILE: _estimate_effective_dpi is a rough
    heuristic (assumed physical page width, not a real DPI read) -- good
    enough to catch an obviously low-res phone photo or small screenshot,
    not a substitute for validating against real invoice photos of known
    size. Capped at _MAX_UPSCALE_FACTOR so a tiny/corrupt image doesn't
    get blown up to something absurd.

    Lanczos resampling (the standard high-quality upscale filter) still
    softens edges when it's manufacturing new pixels -- a mild unsharp
    mask afterward counteracts that, so an upscaled image doesn't end up
    with MORE pixels but SOFTER text edges than the original, which would
    work against Tesseract's stroke-edge-driven character recognition
    rather than for it.
    """
    effective_dpi = _estimate_effective_dpi(image)
    if effective_dpi >= _TARGET_EFFECTIVE_DPI:
        return image
    factor = min(_TARGET_EFFECTIVE_DPI / effective_dpi, _MAX_UPSCALE_FACTOR)
    new_size = (max(1, int(image.width * factor)), max(1, int(image.height * factor)))
    logger.debug(
        "Upscaling image %sx%s -> %sx%s (estimated effective DPI %.0f, target %d)",
        image.width, image.height, new_size[0], new_size[1], effective_dpi, _TARGET_EFFECTIVE_DPI,
    )
    resized = image.resize(new_size, Image.LANCZOS)
    return resized.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))


def preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    """
    OCR accuracy preprocessing pipeline: deskew if the content is
    rotationally tilted, upscale (with a sharpening pass) if the image is
    low-resolution, convert to grayscale, boost contrast -- kept isolated
    from the OCR call itself so it's easy to tune later without touching
    extract_text_from_image_bytes.

    Deliberately does NOT also apply a fixed-threshold binarization step,
    even though that's a commonly-recommended addition for scanned forms:
    tried it here (a plain global cutoff, not adaptive/Otsu) and measured
    it actively FRAGMENTING recognized text on the available test image
    that wasn't fragmented without it (repeated "1a2b3c4d..." run splitting
    mid-token where the unthresholded version read it cleanly) -- a global
    threshold with no adaptive/lighting-aware component apparently costs
    more than it buys on top of the contrast boost already here. Left out
    rather than shipped on unverified assumption; worth retrying with
    adaptive/Otsu thresholding specifically against a real
    scanned/photographed invoice, where uneven lighting is more likely to
    be the dominant problem than it is on a clean synthetic test image.
    (_deskew_image above already pulls in the Otsu-thresholding building
    block this would need, via opencv -- see that function if revisiting.)
    """
    deskewed = _deskew_image(image)
    upscaled = _upscale_if_low_resolution(deskewed)
    grayscale = upscaled.convert("L")
    return ImageEnhance.Contrast(grayscale).enhance(2.0)


# A horizontal gap at least this many times a row's own word height marks
# a column boundary WITHIN that row -- splitting one Y-clustered row into
# left-to-right column segments (mirrors spatial_text.
# split_row_into_column_segments's role for the native-PDF path) so a
# same-line label:value search (_search_labeled_value is deliberately
# same-line-only) can't cross from one form column into an unrelated
# neighboring one just because row-clustering put them on the same visual
# row. Real invoices routinely lay several side-by-side blocks on one row
# -- e.g. "Details of Receiver (Billed to)" next to "Details of Consignee
# (Shiped to)", or an "Invoice No./Order No./Ref No." block next to an
# "Invoice Date/Order Date/Ref Date" block -- and without this, a label's
# same-row value search can grab a neighboring column's text instead of
# (or in addition to) its own value, or find nothing at all if the real
# value ends up several unrelated words away on the joined line. Scaled
# by word height (not a fixed pixel value), since OCR coordinates are in
# PIXELS at whatever resolution/upscale factor applies to a given image,
# unlike the native-PDF path's fixed-point coordinate space. FRAGILE: not
# validated against a range of real invoice photos yet -- too small and
# normal word-to-word spacing within one column fragments; too large and
# genuinely separate side-by-side form columns stay merged.
_COLUMN_GAP_HEIGHT_RATIO = 6.0


def _split_row_into_column_segments(row: list[dict]) -> list[list[dict]]:
    words_sorted = sorted(row, key=lambda w: w["left"])
    segments: list[list[dict]] = [[words_sorted[0]]]
    for word in words_sorted[1:]:
        prev = segments[-1][-1]
        prev_right = prev["left"] + prev["width"]
        gap_threshold = max(prev["height"], word["height"]) * _COLUMN_GAP_HEIGHT_RATIO
        if word["left"] - prev_right > gap_threshold:
            segments.append([word])
        else:
            segments[-1].append(word)
    return segments


def _group_words_into_rows(ocr_data: dict, row_tolerance_ratio: float = 0.6) -> list[str]:
    """
    Reconstructs visual rows from pytesseract.image_to_data()'s per-word
    bounding boxes, ignoring Tesseract's own block/paragraph/line
    segmentation -- on a dense multi-column form, that segmentation can
    fragment a single visual row across multiple "lines" (or merge
    unrelated ones), which is exactly the failure mode that breaks
    label-matching on tabular invoices. Words are clustered purely by
    vertical (top) proximity, in top-to-bottom scan order; each Y-cluster
    is then split into left-to-right COLUMN segments wherever a large
    horizontal gap suggests a different form column rather than the same
    one (_split_row_into_column_segments) -- each segment becomes its own
    output line, rather than joining the whole Y-cluster into one line
    regardless of how many unrelated side-by-side blocks it spans.

    row_tolerance_ratio: how close two words' vertical centers need to be
    (relative to word height) to count as "the same row". FRAGILE: a fixed
    ratio, not validated against real invoice photos -- too tight and a
    single row splits in two; too loose and two visually-close but
    genuinely separate rows merge into one, which is just as bad for
    label-matching as splitting.
    """
    words = []
    n = len(ocr_data.get("text", []))
    for i in range(n):
        text = (ocr_data["text"][i] or "").strip()
        if not text:
            continue
        conf_raw = ocr_data.get("conf", ["-1"] * n)[i]
        try:
            if float(conf_raw) < 0:
                continue  # Tesseract's own marker for "not a real word box"
        except (TypeError, ValueError):
            pass
        words.append(
            {
                "text": text,
                "top": ocr_data["top"][i],
                "left": ocr_data["left"][i],
                "width": ocr_data["width"][i],
                "height": max(ocr_data["height"][i], 1),
            }
        )

    if not words:
        return []

    words.sort(key=lambda w: (w["top"], w["left"]))

    rows: list[list[dict]] = [[words[0]]]
    row_running_top = float(words[0]["top"])
    for word in words[1:]:
        tolerance = max(rows[-1][-1]["height"], word["height"]) * row_tolerance_ratio
        if abs(word["top"] - row_running_top) <= tolerance:
            rows[-1].append(word)
            row_running_top = sum(w["top"] for w in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            row_running_top = float(word["top"])

    lines: list[str] = []
    for row in rows:
        for segment in _split_row_into_column_segments(row):
            lines.append(" ".join(w["text"] for w in sorted(segment, key=lambda w: w["left"])))
    return lines


def extract_text_from_image_bytes(image_bytes: bytes, psm: int = DEFAULT_PSM) -> str:
    """
    OCRs raw image bytes -- a directly-uploaded JPEG/PNG/WEBP, or a
    PyMuPDF-rendered PDF page (see native_pdf_extraction.render_page_to_image_bytes)
    -- and returns row-reconstructed text (see _group_words_into_rows).
    This is the "get text" half of the OCR path; field-mapping happens
    separately (extract_page_invoice_number/extract_invoice_fields below,
    or directly via native_pdf_extraction.extract_header_fields_from_text
    for the per-page-within-a-PDF case).

    Logs the full raw text at DEBUG level (only reaches the console when
    settings.debug_mode is on -- see app/main.py's logging config) so it
    can be inspected directly when label-matching isn't finding fields it
    should; always logs a one-line length summary at INFO regardless, so
    there's at least a trace of every OCR call even outside debug mode.
    """
    image = Image.open(io.BytesIO(image_bytes))
    preprocessed = preprocess_image_for_ocr(image)
    ocr_data = pytesseract.image_to_data(preprocessed, config=f"--psm {psm}", output_type=Output.DICT)
    lines = _group_words_into_rows(ocr_data)
    text = "\n".join(lines)

    logger.info("OCR extracted %d chars across %d reconstructed rows (psm=%d)", len(text), len(lines), psm)
    logger.debug("----- RAW OCR TEXT START -----\n%s\n----- RAW OCR TEXT END -----", text)

    return text


def extract_page_invoice_number_from_text(text: str) -> tuple[str | None, ConfidenceBand]:
    invoice_number, confidence = _extract_invoice_number(text)
    confidence = _cap_confidence_for_source("invoice_number", invoice_number, confidence, ExtractionSource.OCR)
    return invoice_number, confidence


def extract_page_invoice_number(image_bytes: bytes) -> tuple[str | None, ConfidenceBand]:
    """
    OCR equivalent of native_pdf_extraction's cheap per-page invoice-number
    pass, for a directly-uploaded image (JPEG/PNG/WEBP) -- there's no PDF
    page/doc here, so this doesn't go through extract_page_invoice_numbers
    at all, just OCRs once and reuses the same label-matching + confidence
    cap directly.
    """
    text = extract_text_from_image_bytes(image_bytes)
    return extract_page_invoice_number_from_text(text)


def extract_invoice_fields_from_text(text: str) -> tuple[dict, dict[str, ConfidenceBand], list[InvoiceLineItem]]:
    header_fields, header_field_confidences = extract_header_fields_from_text(text, ExtractionSource.OCR)
    header_fields["tax_bracket_summary"] = []
    header_field_confidences["tax_bracket_summary"] = ConfidenceBand.NOT_FOUND
    return header_fields, header_field_confidences, []


def extract_invoice_fields(image_bytes: bytes) -> tuple[dict, dict[str, ConfidenceBand], list[InvoiceLineItem]]:
    """
    OCR equivalent of native_pdf_extraction.extract_invoice_group_fields,
    for a directly-uploaded image (JPEG/PNG/WEBP). Runs OCR once, then the
    exact same shared field-mapper used by native PDFs. line_items and
    tax_bracket_summary are always empty -- see this module's docstring's
    KNOWN LIMITATION on table extraction from OCR text.
    """
    text = extract_text_from_image_bytes(image_bytes)
    return extract_invoice_fields_from_text(text)
