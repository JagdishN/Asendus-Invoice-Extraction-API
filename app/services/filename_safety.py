"""
Sanitizes arbitrary strings into safe, unique filenames.

Originally this only sanitized Excel worksheet names (hence the collision
algorithm's shape); now it backs per-invoice CSV/zip export filenames too,
so the invalid-character set covers general filesystem safety (Windows is
the strictest of the common filesystems, so its reserved set is used as
the superset): \\ / : * ? " < > |

Invoice numbers and party names frequently contain '/' or other reserved
characters (e.g. "INV/2026/001"), so this isn't cosmetic -- without
sanitization, file creation would fail outright on some platforms.
"""

from __future__ import annotations

import re

_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE_RE = re.compile(r"\s+")
# Leaves headroom for a file extension plus OS path-length limits once this
# is joined under a directory (e.g. storage/exports/{job_id}/...).
_MAX_FILENAME_LEN = 150


def sanitize_filename(value: str | None, fallback_index: int, fallback_prefix: str = "Unnamed") -> str:
    if not value:
        return f"{fallback_prefix}_{fallback_index}"

    cleaned = _INVALID_CHARS_RE.sub("-", value).strip().strip("'")
    cleaned = _WHITESPACE_RE.sub("_", cleaned)
    if not cleaned:
        cleaned = f"{fallback_prefix}_{fallback_index}"

    return cleaned[:_MAX_FILENAME_LEN]


def resolve_unique_filenames(values: list[str | None], fallback_prefix: str = "Unnamed") -> list[str]:
    """
    Sanitizes a batch of raw name strings and appends _2, _3, ... on
    collision. Two different values can sanitize to the same string, or a
    genuine duplicate can occur -- e.g. the same invoice number appearing
    in two split-range-fenced InvoiceGroups within one job -- either way,
    two generated files can't collide on disk (or as worksheet names).
    """
    used: dict[str, int] = {}
    resolved: list[str] = []

    for idx, value in enumerate(values, start=1):
        base_name = sanitize_filename(value, idx, fallback_prefix)
        candidate = base_name

        if candidate in used:
            used[base_name] += 1
            # Reserve room for the "_N" suffix within the length limit.
            suffix = f"_{used[base_name]}"
            candidate = base_name[: _MAX_FILENAME_LEN - len(suffix)] + suffix
        else:
            used[base_name] = 1

        resolved.append(candidate)

    return resolved
