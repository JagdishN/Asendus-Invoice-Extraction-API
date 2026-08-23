from app.core.auth import TEST_PASSWORD, TEST_USERNAME
from tests.pdf_builders import SAMPLE_IRN, build_invoice_pdf_bytes


def test_login_with_wrong_password_returns_401(unauthenticated_client):
    resp = unauthenticated_client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password"


def test_login_with_unknown_username_returns_401(unauthenticated_client):
    resp = unauthenticated_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid username or password"


def test_login_with_correct_credentials_returns_bearer_token(unauthenticated_client):
    resp = unauthenticated_client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str) and len(body["access_token"]) > 20


def test_upload_without_token_is_rejected(unauthenticated_client):
    pdf_bytes = build_invoice_pdf_bytes([{
        "invoice_number": SAMPLE_IRN, "invoice_date": "01/01/2026", "party_name": "Acme",
        "gstin": "27AAAAA0000A1Z5", "items": [], "subtotal": "0", "total": "0",
    }])
    resp = unauthenticated_client.post(
        "/api/jobs",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 401
    assert "Not authenticated" in resp.json()["detail"]


def test_upload_with_invalid_token_is_rejected(unauthenticated_client):
    unauthenticated_client.headers.update({"Authorization": "Bearer not-a-real-token"})
    resp = unauthenticated_client.post(
        "/api/jobs",
        files={"file": ("x.pdf", b"whatever", "application/pdf")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 401
    assert "Invalid authentication token" in resp.json()["detail"]


def test_upload_with_valid_token_succeeds_and_sets_user_id(client):
    pdf_bytes = build_invoice_pdf_bytes([{
        "invoice_number": SAMPLE_IRN, "invoice_date": "01/01/2026", "party_name": "Acme",
        "gstin": "27AAAAA0000A1Z5", "items": [], "subtotal": "0", "total": "0",
    }])
    resp = client.post(
        "/api/jobs",
        files={"file": ("x.pdf", pdf_bytes, "application/pdf")},
        data={"confirmed_no_split": "true"},
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["user_id"] == TEST_USERNAME


def test_history_list_without_token_is_rejected(unauthenticated_client):
    resp = unauthenticated_client.get("/api/jobs")
    assert resp.status_code == 401


def test_history_list_with_token_succeeds(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_job_detail_without_token_is_rejected(unauthenticated_client):
    resp = unauthenticated_client.get("/api/jobs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 401
