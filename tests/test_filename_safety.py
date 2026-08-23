from app.services.filename_safety import resolve_unique_filenames, sanitize_filename


def test_sanitizes_invalid_filesystem_characters():
    assert sanitize_filename("INV/2026/001", fallback_index=1) == "INV-2026-001"
    assert sanitize_filename('A:B*C?D"E<F>G|H\\I', fallback_index=1) == "A-B-C-D-E-F-G-H-I"


def test_replaces_whitespace_with_underscore():
    assert sanitize_filename("Sundar Traders Pvt Ltd", fallback_index=1) == "Sundar_Traders_Pvt_Ltd"


def test_blank_value_falls_back_to_prefix_and_index():
    assert sanitize_filename(None, fallback_index=3) == "Unnamed_3"
    assert sanitize_filename("", fallback_index=3, fallback_prefix="Invoice") == "Invoice_3"


def test_truncates_to_length_limit():
    result = sanitize_filename("X" * 300, fallback_index=1)
    assert len(result) <= 150


def test_resolve_unique_filenames_dedupes_on_collision():
    # "/" and "\\" both sanitize to "-", so these two collide post-sanitization.
    names = resolve_unique_filenames(["INV/1", "INV\\1", "INV-2"])
    assert names[0] == "INV-1"
    assert names[1] == "INV-1_2"
    assert names[2] == "INV-2"
    assert len(set(names)) == len(names)


def test_resolve_unique_filenames_handles_none_entries_without_colliding():
    names = resolve_unique_filenames([None, None, "INV-1"], fallback_prefix="invoice")
    assert names[0] == "invoice_1"
    assert names[1] == "invoice_2"
    assert len(set(names)) == len(names)
