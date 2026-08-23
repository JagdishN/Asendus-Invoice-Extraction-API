"""
Groups pages into invoices based on a per-page invoice-number detection pass.

Algorithm (confirmed with client):
    1. Walk pages in order (within scope -- see split-range handling below).
    2. When a page's detected invoice number differs from the currently open
       group, start a new group.
    3. If a page's invoice number matches an ALREADY-SEEN group (not just the
       immediately preceding one), merge it back into that original group and
       flag `is_non_contiguous_merge=True` -- this drives a "confirm this
       merge" prompt in the preview UI rather than merging silently, since a
       false merge would silently inflate totals in the exported worksheet.
    4. A page with no detected invoice number attaches to the currently open
       group (continuation-page case), and is flagged for review.

Split-value scoping:
    - If the user gave a split value, detection runs independently WITHIN
      each specified range. A repeated invoice number in a later range does
      NOT merge across ranges -- the split value is treated as a hard fence,
      since the user explicitly scoped the operation.
    - If no split value was given, detection runs across the whole document.
"""

from __future__ import annotations

from app.models.schemas import ConfidenceBand, InvoiceGroup, SplitRange


class PageInvoiceNumberResult:
    """Output of the cheap, per-page invoice-number-only detection pass."""

    def __init__(
        self,
        page_number: int,
        invoice_number: str | None,
        confidence: ConfidenceBand,
        has_text_layer: bool = True,
    ):
        self.page_number = page_number
        self.invoice_number = invoice_number
        self.confidence = confidence
        # False signals "this page has little/no extractable text -- likely
        # scanned, will need the (not-yet-built) OCR path" as distinct from
        # "has text, we just couldn't find an invoice number on it."
        self.has_text_layer = has_text_layer


def group_pages_into_invoices(
    page_results: list[PageInvoiceNumberResult],
    split_ranges: list[SplitRange] | None = None,
) -> list[InvoiceGroup]:
    """
    page_results must be sorted by page_number ascending and cover every
    page in scope (the caller is responsible for restricting page_results
    to only the pages within split_ranges, if any were given).
    """
    if not split_ranges:
        return _group_sequential(page_results)

    # Split-value given: detection scoped independently per range,
    # no merging across range boundaries.
    all_groups: list[InvoiceGroup] = []
    for split_range in split_ranges:
        scoped_results = [
            r for r in page_results if split_range.contains(r.page_number)
        ]
        all_groups.extend(_group_sequential(scoped_results))
    return all_groups


def _group_sequential(page_results: list[PageInvoiceNumberResult]) -> list[InvoiceGroup]:
    groups: list[InvoiceGroup] = []
    # invoice_number -> index into `groups`, so we can detect and merge
    # non-contiguous repeats rather than creating duplicate groups.
    seen_numbers: dict[str, int] = {}
    current_group: InvoiceGroup | None = None

    for result in page_results:
        number = result.invoice_number

        if number is None:
            # Continuation page / undetected number: attach to whatever
            # group is currently open. Flag for review since it's a guess.
            if current_group is None:
                current_group = InvoiceGroup(
                    invoice_number=None,
                    invoice_number_confidence=ConfidenceBand.NOT_FOUND,
                    needs_user_review=True,
                )
                groups.append(current_group)
            current_group.source_page_list.append(result.page_number)
            current_group.needs_user_review = True
            continue

        if current_group is not None and current_group.invoice_number == number:
            # Contiguous continuation of the same invoice -- check this
            # BEFORE the seen_numbers lookup, since the current group's
            # own number is always in seen_numbers too.
            current_group.source_page_list.append(result.page_number)
            continue

        if number in seen_numbers:
            # Non-contiguous repeat of an EARLIER, now-closed group --
            # merge into the ORIGINAL group and flag it, don't silently
            # combine.
            target = groups[seen_numbers[number]]
            target.source_page_list.append(result.page_number)
            target.is_non_contiguous_merge = True
            target.needs_user_review = True
            current_group = target
            continue

        # New invoice number -> new group.
        new_group = InvoiceGroup(
            invoice_number=number,
            invoice_number_confidence=result.confidence,
            source_page_list=[result.page_number],
            needs_user_review=result.confidence != ConfidenceBand.HIGH,
        )
        groups.append(new_group)
        seen_numbers[number] = len(groups) - 1
        current_group = new_group

    return groups
