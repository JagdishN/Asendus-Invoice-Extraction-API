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
    DEFAULT_PSM for the other two accuracy levers (resolution, page
    segmentation mode).

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

import pytesseract
from PIL import Image, ImageEnhance
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

# "Assume a single uniform block of text" -- tends to behave better than
# Tesseract's own default (3, full-page automatic segmentation with
# orientation/script detection) on a dense, form-style invoice, which is
# not laid out like a paragraph of prose. Override via OCR_PSM env var
# (see app/core/config.py) to try 4 (single column of variable-size text)
# or 11/12 (sparse text, no particular order) against a real sample if 6
# doesn't hold up -- NOT validated against a real invoice yet, just the
# commonly-recommended starting point for form/receipt-style text.
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


def _estimate_effective_dpi(image: Image.Image) -> float:
    dpi_metadata = image.info.get("dpi")
    if dpi_metadata and dpi_metadata[0]:
        return float(dpi_metadata[0])
    return image.width / _ASSUMED_PAGE_WIDTH_INCHES


def _upscale_if_low_resolution(image: Image.Image) -> Image.Image:
    """
    Tesseract accuracy drops off noticeably below ~300 DPI-equivalent for
    typical invoice text sizes. FRAGILE: _estimate_effective_dpi is a rough
    heuristic (assumed physical page width, not a real DPI read) -- good
    enough to catch an obviously low-res phone photo or small screenshot,
    not a substitute for validating against real invoice photos of known
    size. Capped at _MAX_UPSCALE_FACTOR so a tiny/corrupt image doesn't
    get blown up to something absurd.
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
    return image.resize(new_size, Image.LANCZOS)


def preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    """
    OCR accuracy preprocessing: upscale if the image is low-resolution,
    convert to grayscale, boost contrast -- kept isolated from the OCR call
    itself so it's easy to tune later without touching
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
    adaptive/Otsu thresholding (needs numpy) specifically against a real
    scanned/photographed invoice, where uneven lighting is more likely to
    be the dominant problem than it is on a clean synthetic test image.
    """
    upscaled = _upscale_if_low_resolution(image)
    grayscale = upscaled.convert("L")
    return ImageEnhance.Contrast(grayscale).enhance(2.0)


def _group_words_into_rows(ocr_data: dict, row_tolerance_ratio: float = 0.6) -> list[str]:
    """
    Reconstructs visual rows from pytesseract.image_to_data()'s per-word
    bounding boxes, ignoring Tesseract's own block/paragraph/line
    segmentation -- on a dense multi-column form, that segmentation can
    fragment a single visual row across multiple "lines" (or merge
    unrelated ones), which is exactly the failure mode that breaks
    label-matching on tabular invoices. Words are clustered purely by
    vertical (top) proximity, in top-to-bottom scan order, then each row is
    joined left-to-right by horizontal (left) position -- reconstructing
    "words that are visually near each other on the same row" rather than
    trusting Tesseract's read order.

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

    return [" ".join(w["text"] for w in sorted(row, key=lambda w: w["left"])) for row in rows]


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
