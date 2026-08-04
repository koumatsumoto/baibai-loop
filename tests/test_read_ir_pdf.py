from __future__ import annotations

import pytest
from tools.read_ir_pdf import find_keyword_contexts, parse_page_selection


def test_page_selection_expands_ranges_and_keeps_first_order() -> None:
    assert parse_page_selection("3,1-2,3", 10) == (3, 1, 2)


def test_page_selection_rejects_a_page_beyond_the_document() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        parse_page_selection("1-5", 4)


def test_page_selection_rejects_an_empty_specification() -> None:
    with pytest.raises(ValueError, match="no page selected"):
        parse_page_selection(" , ", 4)


def test_keyword_search_reports_the_page_number_and_collapses_whitespace() -> None:
    pages = ("front matter", "営業利益は\n2,541百万円\nとなりました")

    hits = find_keyword_contexts(pages, ["営業利益"], context_chars=40)

    assert len(hits) == 1
    assert hits[0].page_number == 2
    assert hits[0].context == "営業利益は 2,541百万円 となりました"


def test_repeated_keyword_inside_one_paragraph_is_reported_once() -> None:
    pages = ("自己株式の取得について、自己株式の取得は取締役会決議に基づく",)

    hits = find_keyword_contexts(pages, ["自己株式"], context_chars=200)

    assert len(hits) == 1


def test_separate_mentions_on_one_page_are_both_reported() -> None:
    pages = ("自己株式の取得" + "あ" * 600 + "自己株式の消却",)

    hits = find_keyword_contexts(pages, ["自己株式"], context_chars=100)

    assert len(hits) == 2
    assert hits[0].context.startswith("自己株式の取得")
    assert hits[1].context.endswith("自己株式の消却")


def test_a_keyword_absent_from_every_page_yields_no_hit() -> None:
    assert find_keyword_contexts(("売上高", "経常利益"), ["のれん"]) == ()
