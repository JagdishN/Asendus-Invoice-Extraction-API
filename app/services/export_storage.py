"""
Persistence for generated export artifacts (per-invoice CSVs + job zip
archives), isolated behind a small interface so the backend can be swapped
for S3/MinIO later without touching callers (csv_export.py,
routers/history.py) -- they only ever call save_bytes/read_bytes/exists,
never touch a filesystem path directly. A future S3-backed implementation
just needs the same three methods, keyed the same way (job_id + filename).

No cleanup/retention policy exists yet -- exported files accumulate under
export_storage_root indefinitely. Flagging this as something to confirm
before this goes anywhere near production volume.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.core.config import settings


class LocalExportStorage:
    def __init__(self, root: str | Path):
        self._root = Path(root)

    def _job_dir(self, job_id: UUID) -> Path:
        return self._root / str(job_id)

    def save_bytes(self, job_id: UUID, filename: str, content: bytes) -> str:
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        file_path = job_dir / filename
        file_path.write_bytes(content)
        return str(file_path)

    def read_bytes(self, job_id: UUID, filename: str) -> bytes:
        return (self._job_dir(job_id) / filename).read_bytes()

    def exists(self, job_id: UUID, filename: str) -> bool:
        return (self._job_dir(job_id) / filename).exists()


export_storage = LocalExportStorage(settings.export_storage_root)
