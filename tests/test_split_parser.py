import pytest

from app.services.split_parser import SplitValueError, parse_split_value


def test_blank_value_returns_empty_list():
    assert parse_split_value(None, page_count=10) == []
    assert parse_split_value("   ", page_count=10) == []


def test_single_page():
    ranges = parse_split_value("5", page_count=10)
    assert len(ranges) == 1
    assert ranges[0].start_page == 5
    assert ranges[0].end_page == 5


def test_simple_range():
    ranges = parse_split_value("1-5", page_count=10)
    assert ranges[0].start_page == 1
    assert ranges[0].end_page == 5


def test_multiple_ranges_are_sorted_by_start_page():
    ranges = parse_split_value("7-9, 1-3, 4-6", page_count=10)
    assert [(r.start_page, r.end_page) for r in ranges] == [(1, 3), (4, 6), (7, 9)]


def test_reversed_range_is_rejected_not_silently_fixed():
    with pytest.raises(SplitValueError):
        parse_split_value("5-1", page_count=10)


def test_out_of_range_page_is_rejected():
    with pytest.raises(SplitValueError):
        parse_split_value("1-20", page_count=10)


def test_overlapping_ranges_are_rejected():
    with pytest.raises(SplitValueError):
        parse_split_value("1-5, 4-8", page_count=10)


def test_invalid_token_is_rejected():
    with pytest.raises(SplitValueError):
        parse_split_value("abc", page_count=10)
