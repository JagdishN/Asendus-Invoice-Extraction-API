import csv
import io
import uuid
import zipfile

from app.core.job_store import save_job
from app.models.schemas import Job, SupportedFileType
from tests.pdf_builders import build_invoice_pdf_bytes

SAMPLE_INVOICE = dict(
    invoice_number="INV/2026/010",
    invoice_date="01/01/2026",
    party_name="Acme Corp",
    gstin="27AAAAA0000A1Z5",
    items=[("Widget", "1234", "1", "100.00", "100.00", "9.00", "9.00", "118.00")],
    subtotal="100.00",
    total="118.00",
)


def _upload(client, file_bytes, filename="invoice.pdf", content_type="application/pdf", **form):
    form.setdefault("confirmed_no_split", "true")
    return client.post(
        "/api/jobs",
        files={"file": (filename, file_bytes, content_type)},
        data=form,
    )


def _upload_single_invoice_job(client) -> str:
    pdf_bytes = build_invoice_pdf_bytes([SAMPLE_INVOICE])
    return _upload(client, pdf_bytes).json()["job_id"]


def _upload_multi_invoice_job(client, numbers) -> str:
    invoices = [dict(SAMPLE_INVOICE, invoice_number=n) for n in numbers]
    pdf_bytes = build_invoice_pdf_bytes(invoices)
    return _upload(client, pdf_bytes).json()["job_id"]


def test_export_default_returns_single_csv_for_one_invoice_job(client):
    job_id = _upload_single_invoice_job(client)
    resp = client.get(f"/api/jobs/{job_id}/export")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content[:2] != b"PK"  # not a zip


def test_export_default_returns_zip_for_multi_invoice_job(client):
    job_id = _upload_multi_invoice_job(client, ["INV/2026/020", "INV/2026/021"])
    resp = client.get(f"/api/jobs/{job_id}/export")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert len(zf.namelist()) == 2
        for name in zf.namelist():
            assert name.endswith(".csv")


def test_export_zip_endpoint_returns_zip_for_multi_invoice_job(client):
    job_id = _upload_multi_invoice_job(client, ["INV/2026/030", "INV/2026/031"])
    resp = client.get(f"/api/jobs/{job_id}/export/zip")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


def test_export_zip_endpoint_404_for_single_invoice_job(client):
    job_id = _upload_single_invoice_job(client)
    resp = client.get(f"/api/jobs/{job_id}/export/zip")
    assert resp.status_code == 404


def test_export_single_invoice_by_number_returns_that_invoices_csv(client):
    job_id = _upload_multi_invoice_job(client, ["INV/2026/040", "INV/2026/041"])

    resp = client.get(f"/api/jobs/{job_id}/export/INV/2026/040")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    # The CSV body is table-only now (no invoice-number text in it at all)
    # -- verify the correct group was selected via the filename instead,
    # which still encodes the invoice number's last 5 chars.
    assert "040" in resp.headers["content-disposition"]
    assert "041" not in resp.headers["content-disposition"]


def test_export_unknown_invoice_number_404s(client):
    job_id = _upload_single_invoice_job(client)
    resp = client.get(f"/api/jobs/{job_id}/export/DOES-NOT-EXIST")
    assert resp.status_code == 404


def test_export_404_for_job_without_invoice_groups_yet(client):
    # Every successful upload now runs some extraction (native or OCR) and
    # always produces at least one InvoiceGroup, so this state can no
    # longer be reached through the upload endpoint itself -- seed the
    # store directly to exercise the defensive check in history.py.
    job = Job(user_id="test", original_filename="x.pdf", file_type=SupportedFileType.PDF)
    save_job(job)

    for path in ("export", "export/zip", "export/anything"):
        assert client.get(f"/api/jobs/{job.job_id}/{path}").status_code == 404


def test_export_404_for_unknown_job(client):
    unknown_id = uuid.uuid4()
    for path in ("export", "export/zip", "export/anything"):
        assert client.get(f"/api/jobs/{unknown_id}/{path}").status_code == 404


def test_export_is_generated_once_and_reused_on_subsequent_calls(client):
    job_id = _upload_single_invoice_job(client)

    first = client.get(f"/api/jobs/{job_id}/export")
    detail_after_first = client.get(f"/api/jobs/{job_id}").json()
    generated_at_first = detail_after_first["export"]["generated_at"]

    second = client.get(f"/api/jobs/{job_id}/export")
    detail_after_second = client.get(f"/api/jobs/{job_id}").json()

    assert first.content == second.content
    assert detail_after_second["export"]["generated_at"] == generated_at_first


def test_export_columns_endpoint_lists_field_names_and_labels(client):
    resp = client.get("/api/jobs/export/columns")
    assert resp.status_code == 200
    columns = resp.json()["columns"]

    field_names = [c["field_name"] for c in columns]
    assert "item_description" in field_names
    assert "pack" in field_names
    labels = {c["field_name"]: c["label"] for c in columns}
    assert labels["item_description"] == "Item Description"
    assert labels["pack"] == "Pack"


def test_export_with_columns_param_returns_only_selected_line_item_columns(client):
    job_id = _upload_single_invoice_job(client)
    resp = client.get(f"/api/jobs/{job_id}/export?columns=line_number,item_description")

    assert resp.status_code == 200
    content = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(content.splitlines()))
    assert rows[0] == ["Line #", "Item Description"]


def test_export_with_columns_param_is_not_cached_as_the_default_export(client):
    # A column-filtered download must never overwrite/be served back as
    # the job's stable "every column" default export.
    job_id = _upload_single_invoice_job(client)
    filtered = client.get(f"/api/jobs/{job_id}/export?columns=item_description")
    default = client.get(f"/api/jobs/{job_id}/export")

    assert filtered.status_code == default.status_code == 200
    filtered_rows = filtered.content.decode("utf-8-sig").splitlines()
    default_rows = default.content.decode("utf-8-sig").splitlines()
    assert len(filtered_rows) == len(default_rows)  # same number of items
    assert filtered.content != default.content  # but not the same columns


def test_export_with_unknown_columns_falls_back_to_every_column(client):
    job_id = _upload_single_invoice_job(client)
    resp = client.get(f"/api/jobs/{job_id}/export?columns=not_a_real_field")
    default = client.get(f"/api/jobs/{job_id}/export")
    assert resp.content == default.content


def test_export_zip_with_columns_param_filters_every_csv_in_the_zip(client):
    job_id = _upload_multi_invoice_job(client, ["INV/2026/050", "INV/2026/051"])
    resp = client.get(f"/api/jobs/{job_id}/export/zip?columns=line_number,item_description")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert len(zf.namelist()) == 2
        for name in zf.namelist():
            content = zf.read(name).decode("utf-8-sig")
            rows = list(csv.reader(content.splitlines()))
            assert rows[0] == ["Line #", "Item Description"]


def test_export_single_invoice_by_number_with_columns_param(client):
    job_id = _upload_multi_invoice_job(client, ["INV/2026/060", "INV/2026/061"])
    resp = client.get(f"/api/jobs/{job_id}/export/INV/2026/060?columns=item_description")

    assert resp.status_code == 200
    content = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(content.splitlines()))
    assert rows[0] == ["Item Description"]


def test_debug_populate_dummy_data_endpoint_adds_groups_and_clears_stale_export(client):
    job_id = _upload_single_invoice_job(client)
    client.get(f"/api/jobs/{job_id}/export")  # generate export for the real single invoice

    resp = client.post(f"/api/jobs/{job_id}/_debug/populate-dummy-data")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["invoice_groups"]) >= 2
    assert body["export"] is None  # stale export cleared, forces regeneration

    export_resp = client.get(f"/api/jobs/{job_id}/export")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"] == "application/zip"
