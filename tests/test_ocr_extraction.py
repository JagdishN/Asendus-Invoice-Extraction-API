"""
Tests for the OCR extraction path: confidence-capping logic (pure, no
tesseract needed), the OCR module itself (needs real tesseract -- skipped
gracefully via @requires_tesseract if it's not installed in this
environment), and page-level routing (native vs OCR, via a spy rather than
relying on real OCR accuracy for a pass/fail signal).
"""
from app.models.schemas import ConfidenceBand
from app.services.native_pdf_extraction import ExtractionSource, _cap_confidence_for_source
from tests.conftest import requires_tesseract
from tests.pdf_builders import SAMPLE_IRN, build_invoice_pdf_bytes, build_valid_png_bytes

# ---------------------------------------------------------------------------
# Confidence-capping logic -- pure functions, no OCR call involved.
# ---------------------------------------------------------------------------


def test_native_source_confidence_is_never_capped():
    assert _cap_confidence_for_source("invoice_total", 100.0, ConfidenceBand.HIGH, ExtractionSource.NATIVE_PDF_TEXT_LAYER) == ConfidenceBand.HIGH


def test_ocr_source_caps_high_to_review_for_non_irn_field():
    capped = _cap_confidence_for_source("invoice_total", 100.0, ConfidenceBand.HIGH, ExtractionSource.OCR)
    assert capped == ConfidenceBand.REVIEW


def test_ocr_source_never_produces_high_even_for_a_perfectly_shaped_irn():
    capped = _cap_confidence_for_source("invoice_number", SAMPLE_IRN, ConfidenceBand.HIGH, ExtractionSource.OCR)
    assert capped == ConfidenceBand.REVIEW
    assert capped != ConfidenceBand.HIGH


def test_ocr_source_malformed_irn_gets_low_not_review():
    capped = _cap_confidence_for_source("invoice_number", "NOT-A-VALID-IRN", ConfidenceBand.REVIEW, ExtractionSource.OCR)
    assert capped == ConfidenceBand.LOW


def test_ocr_source_not_found_stays_not_found():
    capped = _cap_confidence_for_source("invoice_number", None, ConfidenceBand.NOT_FOUND, ExtractionSource.OCR)
    assert capped == ConfidenceBand.NOT_FOUND


def test_ocr_source_review_stays_review_not_downgraded():
    capped = _cap_confidence_for_source("invoice_date", "13/08/2026", ConfidenceBand.REVIEW, ExtractionSource.OCR)
    assert capped == ConfidenceBand.REVIEW


# ---------------------------------------------------------------------------
# Preprocessing -- pure PIL logic, no tesseract call.
# ---------------------------------------------------------------------------


def test_preprocess_converts_to_grayscale():
    from PIL import Image

    from app.services.ocr_extraction import preprocess_image_for_ocr

    rgb_image = Image.new("RGB", (50, 50), color="red")
    result = preprocess_image_for_ocr(rgb_image)
    assert result.mode == "L"


# ---------------------------------------------------------------------------
# Deskew -- pure opencv/PIL logic, no tesseract call. Uses a synthetic image
# with several horizontal ruled lines (stand-ins for table rules/text
# baselines) rather than a real invoice photo, so the test is deterministic
# and doesn't depend on any real sample file being present.
# ---------------------------------------------------------------------------


def _build_ruled_lines_image(width: int = 800, height: int = 600, num_lines: int = 10):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(image)
    for i in range(num_lines):
        y = int(height * (i + 1) / (num_lines + 1))
        draw.line([(width * 0.1, y), (width * 0.9, y)], fill="black", width=3)
    return image


def _measure_dominant_line_angle(image) -> float | None:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100, minLineLength=gray.shape[1] // 4, maxLineGap=20)
    if lines is None:
        return None
    angles = [
        float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        for x1, y1, x2, y2 in lines.reshape(-1, 4)
        if abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) <= 15
    ]
    return float(np.median(angles)) if angles else None


def test_deskew_corrects_a_rotated_ruled_lines_image():
    from app.services.ocr_extraction import _deskew_image

    tilted = _build_ruled_lines_image().rotate(5, expand=False, fillcolor=(255, 255, 255))
    before = _measure_dominant_line_angle(tilted)
    assert before is not None and abs(before) > 2  # confirm the fixture really is tilted

    corrected = _deskew_image(tilted)
    after = _measure_dominant_line_angle(corrected)
    assert after is not None
    assert abs(after) < abs(before)
    assert abs(after) < 1.5


def test_deskew_leaves_an_already_upright_image_unchanged():
    import numpy as np

    from app.services.ocr_extraction import _deskew_image

    upright = _build_ruled_lines_image()
    corrected = _deskew_image(upright)
    diff = np.array(upright.convert("RGB")).astype(int) - np.array(corrected.convert("RGB")).astype(int)
    assert int(np.abs(diff).max()) == 0


def test_deskew_falls_back_to_original_on_a_blank_image_with_no_lines():
    from PIL import Image

    from app.services.ocr_extraction import _deskew_image

    blank = Image.new("RGB", (200, 200), color="white")
    result = _deskew_image(blank)
    assert result.size == blank.size
    assert list(result.getdata()) == list(blank.getdata())


# ---------------------------------------------------------------------------
# Real OCR calls -- need the tesseract binary.
# ---------------------------------------------------------------------------


@requires_tesseract
def test_extract_text_from_image_bytes_reads_rendered_text():
    from app.services.ocr_extraction import extract_text_from_image_bytes

    # Checks for the label text, not exact digits -- OCR can and does
    # misread individual digits at small font sizes (that's the whole
    # reason field confidence gets capped for this source), so asserting
    # on a specific number here would make this test flaky rather than
    # actually verify "OCR extracted something readable".
    png_bytes = build_valid_png_bytes(text="Total Amount: 500.00")
    text = extract_text_from_image_bytes(png_bytes)
    assert "Total" in text and "Amount" in text


@requires_tesseract
def test_extract_page_invoice_number_via_ocr_finds_irn_capped_at_review():
    from app.services.ocr_extraction import extract_page_invoice_number

    png_bytes = build_valid_png_bytes(text=f"IRN No: {SAMPLE_IRN}", size=(900, 100))
    invoice_number, confidence = extract_page_invoice_number(png_bytes)
    assert invoice_number is not None
    assert confidence != ConfidenceBand.HIGH


@requires_tesseract
def test_extract_invoice_fields_via_ocr_full_pipeline_caps_confidence():
    from app.services.ocr_extraction import extract_invoice_fields

    png_bytes = build_valid_png_bytes(
        text=f"IRN No: {SAMPLE_IRN}\nAck No: 112010098765432\nInvoice Date: 13/08/2026\nFor Test Vendor Co\nTotal Amount: 2450.00",
        size=(900, 300),
    )
    header_fields, header_field_confidences, line_items = extract_invoice_fields(png_bytes)

    # Never HIGH, regardless of how clean the OCR read was.
    assert all(conf != ConfidenceBand.HIGH for conf in header_field_confidences.values())
    # Table structure has no OCR equivalent -- always empty, not guessed.
    assert line_items == []
    assert header_fields["tax_bracket_summary"] == []


# ---------------------------------------------------------------------------
# Routing: native-text pages must NOT be OCR'd; text-less pages must be.
# Uses a spy instead of relying on OCR accuracy for the pass/fail signal.
# ---------------------------------------------------------------------------


@requires_tesseract
def test_pdf_page_with_text_layer_does_not_invoke_ocr(client, monkeypatch):
    import app.services.ocr_extraction as ocr_extraction

    calls = []
    original = ocr_extraction.extract_text_from_image_bytes
    monkeypatch.setattr(
        ocr_extraction, "extract_text_from_image_bytes", lambda b: (calls.append(1), original(b))[1]
    )

    invoice = dict(
        invoice_number=SAMPLE_IRN,
        invoice_date="01/01/2026",
        party_name="Acme Corp",
        gstin="27AAAAA0000A1Z5",
        items=[],
        subtotal="0",
        total="0",
    )
    pdf_bytes = build_invoice_pdf_bytes([invoice])
    resp = client.post(
        "/api/jobs",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 201
    assert calls == []  # native text layer present -- OCR never invoked


@requires_tesseract
def test_pdf_page_without_text_layer_invokes_ocr(client, monkeypatch):
    import app.services.ocr_extraction as ocr_extraction
    from tests.pdf_builders import build_blank_pdf_bytes

    calls = []
    original = ocr_extraction.extract_text_from_image_bytes
    monkeypatch.setattr(
        ocr_extraction, "extract_text_from_image_bytes", lambda b: (calls.append(1), original(b))[1]
    )

    pdf_bytes = build_blank_pdf_bytes(page_count=1)
    resp = client.post(
        "/api/jobs",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 201
    assert len(calls) >= 1  # no text layer -- OCR fallback invoked


@requires_tesseract
def test_plain_image_upload_always_invokes_ocr(client, monkeypatch):
    import app.services.ocr_extraction as ocr_extraction

    calls = []
    original = ocr_extraction.extract_text_from_image_bytes
    monkeypatch.setattr(
        ocr_extraction, "extract_text_from_image_bytes", lambda b: (calls.append(1), original(b))[1]
    )

    png_bytes = build_valid_png_bytes(text="Some invoice text")
    resp = client.post(
        "/api/jobs",
        files={"file": ("x.png", png_bytes, "image/png")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 201
    assert len(calls) >= 1
