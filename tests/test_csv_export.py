import csv
import hashlib
import io
import zipfile

from app.models.schemas import Job, SupportedFileType
from app.services.csv_export import (
    find_group_by_invoice_number,
    generate_and_persist_exports,
    populate_job_with_dummy_data,
)
from app.services.export_storage import export_storage


def _make_job() -> Job:
    job = Job(
        user_id="test-user",
        original_filename="sample invoice batch.pdf",
        file_type=SupportedFileType.PDF,
        page_count=5,
    )
    populate_job_with_dummy_data(job)
    return job


def test_generates_one_csv_per_invoice_group():
    job = _make_job()
    metadata = generate_and_persist_exports(job)
    assert len(metadata.invoice_files) == len(job.invoice_groups)
    for group in job.invoice_groups:
        assert str(group.group_id) in metadata.invoice_files


def test_multi_invoice_job_also_generates_a_zip_containing_every_csv():
    job = _make_job()
    assert len(job.invoice_groups) > 1
    metadata = generate_and_persist_exports(job)

    assert metadata.zip_file is not None
    zip_bytes = export_storage.read_bytes(job.job_id, metadata.zip_file.filename)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names_in_zip = set(zf.namelist())
    expected_names = {f.filename for f in metadata.invoice_files.values()}
    assert names_in_zip == expected_names


def test_single_invoice_job_has_no_zip():
    job = _make_job()
    job.invoice_groups = job.invoice_groups[:1]
    metadata = generate_and_persist_exports(job)
    assert metadata.zip_file is None
    assert len(metadata.invoice_files) == 1


def test_files_are_actually_persisted_to_disk_with_matching_size_and_checksum():
    job = _make_job()
    metadata = generate_and_persist_exports(job)

    for exported_file in metadata.invoice_files.values():
        content = export_storage.read_bytes(job.job_id, exported_file.filename)
        assert len(content) == exported_file.size_bytes
        assert hashlib.sha256(content).hexdigest() == exported_file.sha256_checksum


def test_csv_has_header_block_then_blank_row_then_line_items_table():
    job = _make_job()
    metadata = generate_and_persist_exports(job)

    first_group = job.invoice_groups[0]
    exported_file = metadata.invoice_files[str(first_group.group_id)]
    content = export_storage.read_bytes(job.job_id, exported_file.filename)
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))

    assert rows[0] == ["Invoice Number", first_group.invoice_number]
    assert rows[1][0] == "Source Pages"

    blank_row_index = next(i for i, row in enumerate(rows) if row == [])
    header_row = rows[blank_row_index + 1]
    assert header_row[0] == "Line #"
    assert header_row[1] == "Item Description"

    first_item_row = rows[blank_row_index + 2]
    assert first_item_row[1] == first_group.line_items[0].item_description
    # Header fields must not repeat on line-item rows (separate-header-block
    # style, not flat/repeated).
    assert "Invoice Number" not in first_item_row


def test_find_group_by_invoice_number_returns_first_match_or_none():
    job = _make_job()
    target = job.invoice_groups[0]
    found = find_group_by_invoice_number(job, target.invoice_number)
    assert found is target
    assert find_group_by_invoice_number(job, "does-not-exist") is None


def test_filenames_are_unique_even_when_invoice_number_is_none_for_multiple_groups():
    job = _make_job()
    for group in job.invoice_groups:
        group.invoice_number = None
    metadata = generate_and_persist_exports(job)
    filenames = [f.filename for f in metadata.invoice_files.values()]
    assert len(set(filenames)) == len(filenames)
