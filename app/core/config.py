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

    # Tesseract page segmentation mode. Default 6 ("assume a single uniform
    # block of text") tends to behave better than Tesseract's own default
    # (3, full automatic page segmentation with OSD) on dense, form-style
    # invoices -- see ocr_extraction.py. Override via env var to try 4
    # (single column of variable-size text) or 11/12 (sparse text) against
    # a real sample if 6 doesn't hold up.
    ocr_psm: int = int(os.getenv("OCR_PSM", "6"))


settings = Settings()
