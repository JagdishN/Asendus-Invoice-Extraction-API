"""
Parses the user-entered split value into validated SplitRange objects.

Supported formats (V1):
    "5"              -> single page
    "1-5"            -> simple range
    "1-3, 4-6, 7-9"  -> multiple ranges, comma-separated

Validation rules enforced:
    - page numbers must be within [1, page_count]
    - ranges must not overlap
    - start must be <= end within a single range (auto-swapped if reversed... no,
      we reject reversed ranges explicitly rather than silently "fixing" user intent)
"""

from __future__ import annotations

import re

from app.models.schemas import SplitRange


class SplitValueError(Exception):
    """Raised with a user-safe message describing exactly what's wrong."""


_TOKEN_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def parse_split_value(raw_value: str | None, page_count: int) -> list[SplitRange]:
    """
    Returns an empty list if raw_value is None/blank (caller is responsible
    for triggering the "proceed without split value?" confirmation prompt
    at the API/UI layer -- this function only handles actual parsing).
    """
    if raw_value is None or not raw_value.strip():
        return []

    tokens = [t for t in raw_value.split(",") if t.strip()]
    if not tokens:
        return []

    ranges: list[SplitRange] = []
    for token in tokens:
        match = _TOKEN_RE.match(token)
        if not match:
            raise SplitValueError(
                f"'{token.strip()}' isn't a valid page or range. "
                f"Use a single page (e.g. 5) or a range (e.g. 1-5)."
            )
        start_str, end_str = match.group(1), match.group(2)
        start = int(start_str)
        end = int(end_str) if end_str is not None else start

        if start > end:
            raise SplitValueError(
                f"'{token.strip()}' has a start page greater than its end page."
            )
        if start < 1 or end > page_count:
            raise SplitValueError(
                f"'{token.strip()}' is out of range. This file has {page_count} pages."
            )
        ranges.append(SplitRange(start_page=start, end_page=end))

    _reject_overlaps(ranges)
    ranges.sort(key=lambda r: r.start_page)
    return ranges


def _reject_overlaps(ranges: list[SplitRange]) -> None:
    sorted_ranges = sorted(ranges, key=lambda r: r.start_page)
    for i in range(len(sorted_ranges) - 1):
        current, nxt = sorted_ranges[i], sorted_ranges[i + 1]
        if current.end_page >= nxt.start_page:
            raise SplitValueError(
                f"Ranges {current.start_page}-{current.end_page} and "
                f"{nxt.start_page}-{nxt.end_page} overlap. Fix before continuing."
            )
