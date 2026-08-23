"""
In-memory store for debugging-only extraction artifacts -- ONLY populated
when settings.debug_mode is on (see app/core/config.py). Two independent
stores, backing two independent debug routes:
    - raw per-page text (GET /api/jobs/{job_id}/debug/raw-text): exactly
      what PyMuPDF/Tesseract produced BEFORE any label-matching runs, for
      diagnosing why header fields did or didn't get found.
    - table-parsing diagnostics (GET /api/jobs/{job_id}/debug/table-parsing):
      what the line-item table extraction (find_tables() and, if it ran,
      the header-row-driven positional fallback) actually did for each
      invoice group -- header detection, matched column positions,
      computed boundaries, and the first few data rows' attempted column
      assignment -- for diagnosing why the line-items list came back
      empty or wrong on a real invoice. See
      native_pdf_extraction._extract_line_items_positional's diagnostics
      param for exactly what gets recorded.

Deliberately separate from job_store.py rather than fields on Job itself:
this is debugging data, not real job data, and can be sizeable -- keeping
it out of the Job model means it's never accidentally serialized into
normal API responses or export data.

Same non-persistence caveat as job_store.py: in-memory only, cleared on
process restart. Fine for a debugging aid.
"""

from __future__ import annotations

from uuid import UUID

_raw_text: dict[UUID, list[dict]] = {}
_table_parsing: dict[UUID, list[dict]] = {}


def record_page_text(job_id: UUID, page_number: int, source: str, text: str) -> None:
    _raw_text.setdefault(job_id, []).append(
        {"page_number": page_number, "source": source, "text": text}
    )


def get_raw_text(job_id: UUID) -> list[dict]:
    return _raw_text.get(job_id, [])


def record_table_parsing_diagnostics(job_id: UUID, diagnostics: dict) -> None:
    _table_parsing.setdefault(job_id, []).append(diagnostics)


def get_table_parsing_diagnostics(job_id: UUID) -> list[dict]:
    return _table_parsing.get(job_id, [])


def clear(job_id: UUID) -> None:
    _raw_text.pop(job_id, None)
    _table_parsing.pop(job_id, None)
