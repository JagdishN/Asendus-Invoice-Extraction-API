import pytest

from app.models.schemas import SupportedFileType
from app.services.file_validation import (
    FileValidationError,
    detect_and_validate_file_type,
    get_page_count,
)
from tests.pdf_builders import build_blank_pdf_bytes, build_valid_png_bytes

PNG_BYTES = build_valid_png_bytes()


def test_detects_png_by_content_signature():
    result = detect_and_validate_file_type("x.png", PNG_BYTES, max_size_bytes=10_000_000)
    assert result == SupportedFileType.PNG


def test_detects_pdf_by_content_signature():
    pdf_bytes = build_blank_pdf_bytes(page_count=1)
    result = detect_and_validate_file_type("x.pdf", pdf_bytes, max_size_bytes=10_000_000)
    assert result == SupportedFileType.PDF


def test_rejects_empty_file():
    with pytest.raises(FileValidationError):
        detect_and_validate_file_type("x.png", b"", max_size_bytes=10_000_000)


def test_rejects_oversized_file():
    with pytest.raises(FileValidationError):
        detect_and_validate_file_type("x.png", PNG_BYTES, max_size_bytes=10)


def test_rejects_unsupported_content():
    with pytest.raises(FileValidationError):
        detect_and_validate_file_type("x.pdf", b"this is plain text, not a real file", max_size_bytes=10_000_000)


def test_pdf_page_count_matches_actual_pages():
    pdf_bytes = build_blank_pdf_bytes(page_count=3)
    assert get_page_count(pdf_bytes, SupportedFileType.PDF) == 3


def test_image_page_count_is_always_one():
    assert get_page_count(PNG_BYTES, SupportedFileType.PNG) == 1
