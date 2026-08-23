"""
Validates uploaded files by actual content signature (magic bytes), not just
the filename extension -- a mismatched/spoofed extension should be rejected,
per the security requirements carried over from the original blueprint.
"""

from __future__ import annotations

import io

from app.models.schemas import SupportedFileType

_SIGNATURES: dict[bytes, SupportedFileType] = {
    b"%PDF-": SupportedFileType.PDF,
    b"\xff\xd8\xff": SupportedFileType.JPEG,
    b"\x89PNG\r\n\x1a\n": SupportedFileType.PNG,
    # WEBP: "RIFF" .... "WEBP" -- checked separately below since the
    # WEBP marker isn't at offset 0.
}


class FileValidationError(Exception):
    """User-safe validation failure message."""


def detect_and_validate_file_type(
    filename: str, contents: bytes, max_size_bytes: int
) -> SupportedFileType:
    if len(contents) == 0:
        raise FileValidationError("The uploaded file is empty.")
    if len(contents) > max_size_bytes:
        max_mb = max_size_bytes // (1024 * 1024)
        raise FileValidationError(f"File exceeds the {max_mb} MB size limit.")

    for signature, file_type in _SIGNATURES.items():
        if contents.startswith(signature):
            return file_type

    if contents[0:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return SupportedFileType.WEBP

    raise FileValidationError(
        "This file is not a supported PDF/JPEG/PNG/WEBP. "
        "(Supported formats: PDF, JPEG, PNG, WEBP)"
    )


def get_page_count(contents: bytes, file_type: SupportedFileType) -> int:
    if file_type != SupportedFileType.PDF:
        return 1

    try:
        import pypdf  # lightweight, page-count-only use; extraction uses PyMuPDF elsewhere

        reader = pypdf.PdfReader(io.BytesIO(contents))
        if reader.is_encrypted:
            raise FileValidationError(
                "This PDF is password-protected. Please upload an unlocked copy."
            )
        return len(reader.pages)
    except FileValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a user-safe message
        raise FileValidationError(
            "The file couldn't be read. It may be corrupted."
        ) from exc
