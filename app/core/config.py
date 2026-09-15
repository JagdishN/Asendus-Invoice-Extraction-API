"""
Central settings. Deliberately explicit rather than left unbounded --
max file size and max pages/file were flagged during requirements review
as values that need a conscious decision, not silent defaults.

Adjust these once real numbers are confirmed; they're read from env vars
so ops can tune them per-deployment without a code change.
"""

from __future__ import annotations

import os


class Settings:
    max_file_size_bytes: int = int(
        os.getenv("MAX_FILE_SIZE_MB", "100")
    ) * 1024 * 1024
    max_pages_per_file: int = int(os.getenv("MAX_PAGES_PER_FILE", "150"))
    daily_page_limit: int = int(os.getenv("DAILY_PAGE_LIMIT", "20000"))

    confidence_high_threshold: float = float(os.getenv("CONFIDENCE_HIGH", "0.90"))
    confidence_review_threshold: float = float(os.getenv("CONFIDENCE_REVIEW", "0.75"))

    # Local-filesystem root for generated export artifacts (per-invoice
    # CSVs + job zip archives), keyed by job_id underneath this root -- see
    # services/export_storage.py. Relative to the process's working
    # directory unless overridden with an absolute path via env var.
    # Swapping this for S3/MinIO later means a new storage class, not a
    # change here.
    export_storage_root: str = os.getenv("EXPORT_STORAGE_ROOT", "storage/exports")

    # JWT signing secret for the temporary testing-phase auth stopgap (see
    # app/core/auth.py -- to be replaced by real Supabase-based auth later).
    # This default is a placeholder, NOT a secret -- it MUST be overridden
    # via env var in any shared/staging/production deployment.
    jwt_secret_key: str = os.getenv(
        "JWT_SECRET_KEY", "local-dev-only-placeholder-secret-override-me-before-deploying"
    )
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiry_hours: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

    # Debug/diagnostic mode -- OFF by default everywhere, including
    # deployment. When on: (1) app/main.py sets logging to DEBUG level, so
    # the full raw OCR text extracted per page is printed to the console
    # (see app/services/ocr_extraction.py) instead of just a length summary;
    # (2) upload.py records that same raw per-page text (both native and
    # OCR-sourced) into app/core/debug_store.py, keyed by job_id, so it can
    # be pulled back via GET /api/jobs/{job_id}/debug/raw-text -- that route
    # itself 404s when this is off, so it's never accidentally exposed.
    # Raw OCR text can be sizeable and isn't real job data, so it's only
    # held in memory when explicitly asked for via this flag.
    debug_mode: bool = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

    # Tesseract page segmentation mode. 4 ("single column of variable-size
    # text") is the CONFIRMED best default against a real photographed
    # invoice sample (a dense pharma form with handwritten annotations and
    # visible skew/fold damage) -- see ocr_extraction.py. Measured directly
    # against that sample: PSM 6 ("assume a single uniform block of text",
    # the previous default) produced noticeably more garbage/noise tokens
    # from the handwritten margin notes bleeding into the form's own text
    # blocks, while 4 kept the header block meaningfully more readable
    # (e.g. "Invoice Date : 01/09/2026" came through cleanly under 4,
    # unrecognizable under 6). Override via env var to try 3 (Tesseract's
    # own full automatic page segmentation with OSD) or 11/12 (sparse text)
    # against a different real sample if 4 doesn't hold up there.
    ocr_psm: int = int(os.getenv("OCR_PSM", "4"))


settings = Settings()
