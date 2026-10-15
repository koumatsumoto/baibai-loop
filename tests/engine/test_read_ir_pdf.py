from __future__ import annotations

from types import SimpleNamespace

import pytest

from baibai_engine.research.pdf_reader import (
    find_keyword_contexts,
    main,
    missing_keywords,
    parse_page_selection,
)


def test_page_selection_expands_ranges_and_keeps_first_order() -> None:
    assert parse_page_selection("3,1-2,3", 10) == (3, 1, 2)


def test_page_selection_clamps_a_range_that_runs_past_the_last_page() -> None:
    assert parse_page_selection("1-5", 4) == (1, 2, 3, 4)


def test_page_selection_rejects_a_range_that_starts_beyond_the_document() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        parse_page_selection("7-9", 4)


def test_page_selection_rejects_a_single_page_beyond_the_document() -> None:
    with pytest.raises(ValueError, match="out of bounds"):
        parse_page_selection("5", 4)


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


def test_a_comma_joined_keyword_list_is_named_back_as_the_miss() -> None:
    pages = ("売上高は増加し経常利益は減少しました",)
    keywords = ["売上高,経常利益"]

    hits = find_keyword_contexts(pages, keywords)

    assert hits == ()
    assert missing_keywords(keywords, hits) == ("売上高,経常利益",)


def test_only_the_keywords_that_matched_nothing_are_named() -> None:
    pages = ("売上高は増加しました",)
    keywords = ["売上高", "のれん", "減損損失"]

    hits = find_keyword_contexts(pages, keywords)

    assert missing_keywords(keywords, hits) == ("のれん", "減損損失")


def test_keyword_generator_is_searched_on_every_page() -> None:
    hits = find_keyword_contexts(("営業利益1", "営業利益2"), iter(["営業利益"]))
    assert [hit.page_number for hit in hits] == [1, 2]


@pytest.mark.parametrize("pages", [(), ("営業利益",)])
def test_empty_keyword_is_rejected_before_searching(pages) -> None:
    with pytest.raises(ValueError, match="search keyword must not be empty"):
        find_keyword_contexts(pages, [""])


def test_cli_rejects_empty_search_before_reading_pdf(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "not-read.pdf"), "--search", ""])
    assert error.value.code == 2
    assert "search keyword must not be empty" in capsys.readouterr().err


def test_negative_context_is_rejected_instead_of_reporting_an_empty_hit() -> None:
    with pytest.raises(ValueError, match="context_chars must be non-negative"):
        find_keyword_contexts(("営業利益",), ["営業利益"], context_chars=-1)


def test_zero_context_returns_the_matched_keyword() -> None:
    hits = find_keyword_contexts(("前文営業利益後文",), ["営業利益"], context_chars=0)
    assert hits[0].context == "営業利益"


def test_cli_rejects_negative_context_before_reading_pdf(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "not-read.pdf"), "--search", "営業利益", "--context", "-1"])
    assert error.value.code == 2
    assert "--context must be non-negative" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [([], [1]), (["--pages", "3,1"], [3, 1]), (["--search", "営業利益"], [1, 2, 3])],
)
def test_cli_extracts_only_requested_pages_or_all_pages_for_search(
    tmp_path, monkeypatch, capsys, arguments, expected
):
    from baibai_engine.research import pdf_reader

    calls = []

    class Page:
        def __init__(self, number):
            self.number = number

        def extract_text(self):
            calls.append(self.number)
            return f"営業利益{self.number}"

    path = tmp_path / "synthetic.pdf"
    path.touch()
    monkeypatch.setattr(
        pdf_reader, "PdfReader", lambda _path: SimpleNamespace(pages=[Page(i) for i in (1, 2, 3)])
    )
    assert main([str(path), *arguments]) == 0
    assert calls == expected
    output = capsys.readouterr().out
    assert "3 page(s)" in output
    for number in expected:
        assert f"営業利益{number}" in output
