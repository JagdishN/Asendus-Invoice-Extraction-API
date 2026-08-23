from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import debug_store, job_store
from app.core.auth import TEST_PASSWORD, TEST_USERNAME
from app.main import app
from app.services import export_storage as export_storage_module


def _tesseract_available() -> bool:
    """
    Real check (not just a path/PATH lookup) that the Tesseract OCR binary
    actually responds -- app.services.ocr_extraction attempts to
    auto-locate it on import (see that module), but this env may genuinely
    not have it installed at all (it's an OS-level dependency, not a pip
    package -- see requirements.txt / README).
    """
    try:
        import pytesseract

        import app.services.ocr_extraction  # noqa: F401 - triggers its tesseract_cmd auto-detect

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False


requires_tesseract = pytest.mark.skipif(
    not _tesseract_available(), reason="Tesseract OCR binary not installed in this environment"
)


@pytest.fixture(autouse=True)
def _reset_job_store():
    """Jobs live in an in-memory dict for phase 1 (see job_store.py) --
    clear it before and after every test so tests can't leak state into
    each other."""
    job_store._jobs.clear()
    debug_store._raw_text.clear()
    debug_store._table_parsing.clear()
    yield
    job_store._jobs.clear()
    debug_store._raw_text.clear()
    debug_store._table_parsing.clear()


@pytest.fixture(autouse=True)
def _isolate_export_storage(tmp_path, monkeypatch):
    """Redirects the export_storage singleton's root to a per-test tmp dir
    so test runs never write into the real project's storage/exports/."""
    monkeypatch.setattr(export_storage_module.export_storage, "_root", tmp_path / "exports")


@pytest.fixture
def unauthenticated_client() -> TestClient:
    """A client with no Authorization header -- for testing that protected
    routes correctly reject missing/invalid tokens."""
    return TestClient(app)


@pytest.fixture
def client(unauthenticated_client: TestClient) -> TestClient:
    """Every other existing test uses this fixture and expects to reach
    protected routes successfully, so it logs in once with the test
    credentials and carries the token as a default header -- no other test
    file needed to change when auth was added."""
    login_resp = unauthenticated_client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    token = login_resp.json()["access_token"]
    unauthenticated_client.headers.update({"Authorization": f"Bearer {token}"})
    return unauthenticated_client
