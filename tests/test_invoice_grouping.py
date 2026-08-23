from app.models.schemas import ConfidenceBand, SplitRange
from app.services.invoice_grouping import PageInvoiceNumberResult, group_pages_into_invoices


def _page_result(page_number, invoice_number, confidence=ConfidenceBand.HIGH, has_text_layer=True):
    return PageInvoiceNumberResult(page_number, invoice_number, confidence, has_text_layer=has_text_layer)


def test_contiguous_continuation_of_same_invoice_stays_one_group():
    results = [_page_result(1, "INV-1"), _page_result(2, "INV-1"), _page_result(3, "INV-1")]
    groups = group_pages_into_invoices(results)
    assert len(groups) == 1
    assert groups[0].source_page_list == [1, 2, 3]
    assert groups[0].is_non_contiguous_merge is False


def test_new_invoice_number_starts_a_new_group():
    results = [_page_result(1, "INV-1"), _page_result(2, "INV-2")]
    groups = group_pages_into_invoices(results)
    assert [g.invoice_number for g in groups] == ["INV-1", "INV-2"]


def test_non_contiguous_repeat_merges_into_original_and_flags_it():
    results = [_page_result(1, "INV-1"), _page_result(2, "INV-2"), _page_result(3, "INV-1")]
    groups = group_pages_into_invoices(results)
    assert len(groups) == 2

    inv1_group = next(g for g in groups if g.invoice_number == "INV-1")
    assert inv1_group.source_page_list == [1, 3]
    assert inv1_group.is_non_contiguous_merge is True
    assert inv1_group.needs_user_review is True


def test_undetected_number_attaches_to_currently_open_group_and_flags_review():
    results = [_page_result(1, "INV-1"), _page_result(2, None, confidence=ConfidenceBand.NOT_FOUND)]
    groups = group_pages_into_invoices(results)
    assert len(groups) == 1
    assert groups[0].source_page_list == [1, 2]
    assert groups[0].needs_user_review is True


def test_leading_undetected_page_opens_its_own_unassigned_group():
    results = [_page_result(1, None, confidence=ConfidenceBand.NOT_FOUND)]
    groups = group_pages_into_invoices(results)
    assert len(groups) == 1
    assert groups[0].invoice_number is None
    assert groups[0].needs_user_review is True


def test_split_ranges_are_a_hard_fence_no_merge_across_boundary():
    split_ranges = [SplitRange(start_page=1, end_page=2), SplitRange(start_page=3, end_page=4)]
    results = [
        _page_result(1, "INV-1"),
        _page_result(2, "INV-2"),
        _page_result(3, "INV-1"),  # same number reappears in the second range
        _page_result(4, "INV-3"),
    ]
    groups = group_pages_into_invoices(results, split_ranges)

    inv1_groups = [g for g in groups if g.invoice_number == "INV-1"]
    assert len(inv1_groups) == 2  # NOT merged across the split-range boundary
    assert all(not g.is_non_contiguous_merge for g in inv1_groups)
