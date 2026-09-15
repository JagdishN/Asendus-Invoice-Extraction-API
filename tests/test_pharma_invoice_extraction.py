"""
Tests for the confirmed pharma/GST invoice format: IRN-shape confidence,
vendor-vs-buyer split, blank-tolerant ack/e-way-bill/FSSAI fields, the
tax-bracket summary, and the Sold/Free-as-separate-rows line-item mapping.
"""
from app.models.schemas import ConfidenceBand
from app.services.csv_export import _invoice_filename_seed, generate_and_persist_exports
from app.services.native_pdf_extraction import extract_invoice_group_fields, extract_page_invoice_numbers
from tests.pdf_builders import SAMPLE_IRN, build_pharma_invoice_pdf_bytes


def test_irn_is_detected_with_high_confidence():
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    results = extract_page_invoice_numbers(pdf_bytes)

    assert results[0].invoice_number == SAMPLE_IRN
    assert results[0].confidence == ConfidenceBand.HIGH


def test_ack_number_extracted():
    pdf_bytes = build_pharma_invoice_pdf_bytes(ack_number="112010098765432")
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["ack_number"] == "112010098765432"
    assert header_field_confidences["ack_number"] == ConfidenceBand.HIGH


def test_blank_eway_bill_and_fssai_are_not_found_not_an_error():
    # Defaults leave these blank, matching the confirmed real sample.
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    for field in ("eway_bill_number", "eway_bill_date", "fssai_number"):
        assert header_fields[field] is None
        assert header_field_confidences[field] == ConfidenceBand.NOT_FOUND


def test_eway_bill_and_fssai_extracted_when_present():
    pdf_bytes = build_pharma_invoice_pdf_bytes(
        eway_bill_number="341009876543",
        eway_bill_date="10/08/2026",
        fssai_number="12345678901234",
    )
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["eway_bill_number"] == "341009876543"
    assert header_field_confidences["eway_bill_number"] == ConfidenceBand.HIGH
    assert header_fields["eway_bill_date"] == "10/08/2026"
    assert header_field_confidences["eway_bill_date"] == ConfidenceBand.HIGH
    assert header_fields["fssai_number"] == "12345678901234"


def test_vendor_name_found_via_signature_line():
    pdf_bytes = build_pharma_invoice_pdf_bytes(vendor_name="VIJAY SAI MEDICAL DISTRIBUTORS")
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["party_name"] == "VIJAY SAI MEDICAL DISTRIBUTORS"
    assert header_field_confidences["party_name"] == ConfidenceBand.HIGH


def test_buyer_fields_not_found_when_no_buyer_section_present():
    # Matches the real (cropped) sample this format was built from -- no
    # buyer section visible, so this must NOT guess.
    pdf_bytes = build_pharma_invoice_pdf_bytes(include_buyer_section=False)
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["buyer_name"] is None
    assert header_field_confidences["buyer_name"] == ConfidenceBand.NOT_FOUND
    assert header_fields["buyer_address"] is None
    assert header_field_confidences["buyer_address"] == ConfidenceBand.NOT_FOUND


def test_buyer_fields_found_when_buyer_section_present():
    # UNVALIDATED against a full real sample -- see native_pdf_extraction.py
    # module docstring. This just confirms the label-matching mechanism
    # itself works when the section IS present.
    pdf_bytes = build_pharma_invoice_pdf_bytes(
        include_buyer_section=True,
        buyer_name="Krishna Pharma Retail",
        buyer_address="12 MG Road Bengaluru",
    )
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["buyer_name"] == "Krishna Pharma Retail"
    assert header_field_confidences["buyer_name"] == ConfidenceBand.HIGH
    assert header_fields["buyer_address"] == "12 MG Road Bengaluru"


def test_totals_block_fields_including_negative_adjustment():
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    header_fields, header_field_confidences, _ = extract_invoice_group_fields(pdf_bytes, [1])

    assert header_fields["total_amount"] == 2450.0
    assert header_fields["discount_amount"] == 49.0
    assert header_fields["tcs_amount"] == 10.0
    assert header_fields["invoice_amount"] == 2724.12
    assert header_fields["adjustment_amount"] == -0.12  # signed value must survive parsing
    assert header_fields["invoice_total"] == 2724.0  # "Net Payable Amount" -> invoice_total
    for field in ("total_amount", "discount_amount", "tcs_amount", "invoice_amount", "adjustment_amount", "invoice_total"):
        assert header_field_confidences[field] == ConfidenceBand.HIGH


def test_tax_bracket_summary_kept_separate_from_line_item_taxes():
    pdf_bytes = build_pharma_invoice_pdf_bytes(
        tax_brackets=[("12%", "2401.00", "288.12"), ("5%", "500.00", "25.00")]
    )
    header_fields, header_field_confidences, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    summary = header_fields["tax_bracket_summary"]
    assert summary == [
        {"rate": 12.0, "taxable_amount": 2401.0, "tax_amount": 288.12},
        {"rate": 5.0, "taxable_amount": 500.0, "tax_amount": 25.0},
    ]
    assert header_field_confidences["tax_bracket_summary"] == ConfidenceBand.HIGH
    # No consolidation: line items keep their own per-row tax fields too.
    assert line_items[0].cgst_rate == 6.0


def test_sold_and_free_quantities_stay_as_separate_line_items_not_merged():
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    assert len(line_items) == 4  # 2 products x (Sold row + Free row), not merged

    sold_row, free_row = line_items[0], line_items[1]
    assert sold_row.item_description == free_row.item_description == "Paracetamol 500mg Tab"
    assert sold_row.batch_number == free_row.batch_number == "B2201"
    # Raw "12/2027" (month/year, no day) normalizes to short-date "Dec-2027".
    assert sold_row.expiry_date == free_row.expiry_date == "Dec-2027"

    assert sold_row.quantity_sold == 100.0
    assert sold_row.quantity_free is None
    assert free_row.quantity_sold is None
    assert free_row.quantity_free == 10.0


def test_line_item_pharma_fields_mapped_correctly():
    pdf_bytes = build_pharma_invoice_pdf_bytes()
    _, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    sold_row = line_items[0]
    assert sold_row.hsn_sac == "3004"
    assert sold_row.mrp == 15.0
    assert sold_row.ptr == 10.5
    assert sold_row.taxable_value == 1029.0
    assert sold_row.discount_amount == 21.0
    assert sold_row.cgst_rate == 6.0
    assert sold_row.cgst_amount == 61.74
    assert sold_row.sgst_rate == 6.0
    assert sold_row.sgst_amount == 61.74
    assert sold_row.igst_rate is None  # not present in this invoice's data
    assert sold_row.igst_amount is None
    assert sold_row.line_total == 1050.0  # "Total Amt" column -> line_total
    # rate_pts meaning is unconfirmed; this invoice leaves it blank.
    assert sold_row.rate_pts is None


def test_filename_uses_buyer_name_date_and_irn_suffix():
    pdf_bytes = build_pharma_invoice_pdf_bytes(irn=SAMPLE_IRN)
    header_fields, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    from app.models.schemas import InvoiceGroup, Job, SupportedFileType
    from datetime import datetime, timezone

    group = InvoiceGroup(invoice_number=SAMPLE_IRN, header_fields=header_fields, line_items=line_items)
    job = Job(user_id="test", original_filename="whatever.pdf", file_type=SupportedFileType.PDF)

    seed = _invoice_filename_seed(job, group, datetime.now(timezone.utc))
    assert seed == f"Customer_Unspecified_10/08/2026_{SAMPLE_IRN[-5:]}"


def test_filename_uses_buyer_name_when_present_end_to_end():
    pdf_bytes = build_pharma_invoice_pdf_bytes(
        irn=SAMPLE_IRN, include_buyer_section=True, buyer_name="Krishna Pharma Retail"
    )
    header_fields, _, line_items = extract_invoice_group_fields(pdf_bytes, [1])

    from app.models.schemas import InvoiceGroup, Job, SupportedFileType

    group = InvoiceGroup(invoice_number=SAMPLE_IRN, header_fields=header_fields, line_items=line_items)
    job = Job(
        user_id="test",
        original_filename="whatever.pdf",
        file_type=SupportedFileType.PDF,
        invoice_groups=[group],
    )

    metadata = generate_and_persist_exports(job)
    filenames = [f.filename for f in metadata.invoice_files.values()]
    assert any("Krishna_Pharma_Retail" in name for name in filenames)
    assert any(name.endswith(f"{SAMPLE_IRN[-5:]}.csv") for name in filenames)
