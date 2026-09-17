import csv
import hashlib
import io
import zipfile

from app.models.schemas import Job, SupportedFileType
from app.services.csv_export import (
    LINE_ITEM_EXPORT_COLUMNS,
    build_filtered_zip_bytes,
    build_invoice_csv_bytes,
    find_group_by_invoice_number,
    generate_and_persist_exports,
    invoice_csv_filename,
    populate_job_with_dummy_data,
    resolve_selected_line_item_fields,
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


def test_csv_is_the_line_items_table_only_no_header_block_no_blank_rows():
    # Client-confirmed: no invoice/buyer header info in the CSV body at
    # all, and no blank separator rows -- just the table's own header row
    # followed immediately by one row per line item.
    job = _make_job()
    metadata = generate_and_persist_exports(job)

    first_group = job.invoice_groups[0]
    exported_file = metadata.invoice_files[str(first_group.group_id)]
    content = export_storage.read_bytes(job.job_id, exported_file.filename)
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))

    assert [] not in rows  # no blank rows anywhere in the file

    header_row = rows[0]
    assert header_row[0] == "Line #"
    assert header_row[1] == "Item Description"

    assert len(rows) == 1 + len(first_group.line_items)
    first_item_row = rows[1]
    assert first_item_row[1] == first_group.line_items[0].item_description
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


# ---------------------------------------------------------------------------
# Column-selection export (client-confirmed: a "choose which columns to
# export" dropdown, driven by LINE_ITEM_EXPORT_COLUMNS, defaulting to every
# column selected) -- see resolve_selected_line_item_fields/history.py's
# `columns` query param.
# ---------------------------------------------------------------------------


def test_line_item_export_columns_includes_pack_and_matches_csv_order():
    field_names = [c["field_name"] for c in LINE_ITEM_EXPORT_COLUMNS]
    assert "pack" in field_names
    assert field_names.index("item_description") < field_names.index("pack") < field_names.index("hsn_sac")
    labels = {c["field_name"]: c["label"] for c in LINE_ITEM_EXPORT_COLUMNS}
    assert labels["pack"] == "Pack"
    assert labels["item_description"] == "Item Description"


def test_line_item_export_columns_includes_pts_derived_quantity_columns():
    labels = {c["field_name"]: c["label"] for c in LINE_ITEM_EXPORT_COLUMNS}
    assert labels["pts_original_quantity"] == "Original Quantity"
    assert labels["pts_free_quantity"] == "Free Quantity"


def test_resolve_selected_line_item_fields_none_or_empty_means_no_filter():
    assert resolve_selected_line_item_fields(None) is None
    assert resolve_selected_line_item_fields([]) is None


def test_resolve_selected_line_item_fields_preserves_canonical_order_not_input_order():
    # Caller passes hsn_sac before item_description; the resolved list
    # must still come back in the CSV's own canonical column order.
    resolved = resolve_selected_line_item_fields(["hsn_sac", "item_description"])
    assert resolved.index("item_description") < resolved.index("hsn_sac")


def test_resolve_selected_line_item_fields_drops_unknown_names_but_keeps_known_ones():
    resolved = resolve_selected_line_item_fields(["item_description", "not_a_real_field"])
    assert resolved == ["item_description"]


def test_resolve_selected_line_item_fields_falls_back_to_all_when_nothing_recognized():
    assert resolve_selected_line_item_fields(["totally_bogus"]) is None


def test_build_invoice_csv_bytes_with_selected_fields_writes_only_those_columns():
    job = _make_job()
    group = job.invoice_groups[0]
    content = build_invoice_csv_bytes(group, ["line_number", "item_description", "taxable_value"])
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))

    header_row = rows[0]
    assert header_row == ["Line #", "Item Description", "Taxable Value"]

    first_item_row = rows[1]
    assert len(first_item_row) == 3
    assert first_item_row[1] == group.line_items[0].item_description


def test_build_invoice_csv_bytes_without_selected_fields_still_writes_every_column():
    job = _make_job()
    group = job.invoice_groups[0]
    content = build_invoice_csv_bytes(group)
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))

    header_row = rows[0]
    assert len(header_row) == len(LINE_ITEM_EXPORT_COLUMNS)


def test_invoice_csv_filename_matches_the_persisted_naming_convention():
    job = _make_job()
    group = job.invoice_groups[0]
    metadata = generate_and_persist_exports(job)
    persisted_filename = metadata.invoice_files[str(group.group_id)].filename

    on_demand_filename = invoice_csv_filename(job, group)
    # Same seed convention (Customer_Name_date_irnSuffix.csv), sanitized
    # the same way -- can't compare exactly since invoice_date/timestamp-
    # based fallback pieces can differ between the two calls, so just
    # check the customer-name prefix, which is stable.
    customer_name_prefix = group.header_fields["buyer_name"].replace(" ", "_")
    assert on_demand_filename.startswith(customer_name_prefix)
    assert persisted_filename.startswith(customer_name_prefix)
    assert on_demand_filename.endswith(".csv")


def test_build_filtered_zip_bytes_contains_only_selected_columns_for_every_group():
    job = _make_job()
    assert len(job.invoice_groups) > 1
    zip_bytes, filename = build_filtered_zip_bytes(job, ["line_number", "item_description"])
    assert filename.endswith(".zip")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert len(zf.namelist()) == len(job.invoice_groups)
        for name in zf.namelist():
            content = zf.read(name)
            rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
            assert rows[0] == ["Line #", "Item Description"]
