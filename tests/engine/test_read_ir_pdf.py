from __future__ import annotations

from types import SimpleNamespace

import pytest

from baibai_engine.investment_judgment.pdf_reader import (
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


@pytest.mark.parametrize("gap", [18, 22])
def test_overlapping_context_keeps_a_mention_not_fully_shown_before(gap) -> None:
    pages = ("営業利益" + "あ" * gap + "営業利益200",)
    hits = find_keyword_contexts(pages, ["営業利益"], context_chars=20)
    assert len(hits) == 2
    assert hits[1].context.endswith("営業利益200")


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
    from baibai_engine.investment_judgment import pdf_reader

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


def _mock_pdf(tmp_path, monkeypatch, texts):
    from baibai_engine.investment_judgment import pdf_reader

    calls = []

    class Page:
        def __init__(self, number, text):
            self.number = number
            self.text = text

        def extract_text(self):
            calls.append(self.number)
            return self.text

    path = tmp_path / "synthetic.pdf"
    path.touch()
    monkeypatch.setattr(
        pdf_reader,
        "PdfReader",
        lambda _path: SimpleNamespace(
            pages=[Page(number, text) for number, text in enumerate(texts, start=1)]
        ),
    )
    return path, calls


@pytest.mark.parametrize("full", [False, True])
def test_search_scope_preserves_physical_page_order_and_extracts_once(
    tmp_path, monkeypatch, capsys, full
):
    path, calls = _mock_pdf(tmp_path, monkeypatch, [f"営業利益{i}" for i in range(1, 8)])
    args = [str(path), "--pages", "7,3,7", "--search", "営業利益"]
    if full:
        args.append("--show-hit-pages")
    assert main(args) == 0
    assert calls == [7, 3]
    out = capsys.readouterr().out
    prefix = "=== page " if full else "--- p"
    assert out.index(prefix + "7") < out.index(prefix + "3")
    assert prefix + "1" not in out
    assert out.count(prefix) == 2


@pytest.mark.parametrize("context", [0, 500])
def test_hit_page_output_preserves_lines_and_deduplicates_keywords_and_pages(
    tmp_path, monkeypatch, capsys, context
):
    text = "2026年度 前期 当期\n単位：百万円\n売上高 100 120\n営業利益 -4 ---\n脚注 営業利益"
    path, calls = _mock_pdf(tmp_path, monkeypatch, [text])
    assert (
        main(
            [
                str(path),
                "--search",
                "営業利益",
                "売上高",
                "営業利益",
                "--show-hit-pages",
                "--context",
                str(context),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert calls == [1]
    assert out.count("=== page 1 [営業利益, 売上高]") == 1
    assert out.count(text) == 1
    assert "--- p" not in out


@pytest.mark.parametrize("partial", [False, True])
def test_scoped_search_distinguishes_no_hit_from_partial_keyword_miss(
    tmp_path, monkeypatch, capsys, partial
):
    path, calls = _mock_pdf(tmp_path, monkeypatch, ["のれん", "営業利益"])
    keywords = ["営業利益", "のれん"] if partial else ["のれん"]
    assert main([str(path), "--pages", "2", "--search", *keywords]) == (0 if partial else 1)
    assert calls == [2]
    err = capsys.readouterr().err
    assert "no hit for: ['のれん']" in err
    assert ("no match in the selected pages' extracted text" in err) is (not partial)


def test_empty_extracted_pages_warn_without_claiming_absence(tmp_path, monkeypatch, capsys):
    path, calls = _mock_pdf(tmp_path, monkeypatch, [None, "  \n", "営業利益"])
    assert main([str(path), "--pages", "2,1,3", "--search", "営業利益"]) == 0
    assert calls == [2, 1, 3]
    err = capsys.readouterr().err
    assert "no searchable extracted text on pages: [2, 1]" in err
    assert "inspect the original PDF" in err


@pytest.mark.parametrize("selection", ["9", ""])
def test_search_rejects_invalid_page_selection(tmp_path, monkeypatch, capsys, selection):
    path, calls = _mock_pdf(tmp_path, monkeypatch, ["営業利益"])
    with pytest.raises(SystemExit) as error:
        main([str(path), "--pages", selection, "--search", "営業利益"])
    assert error.value.code == 2
    assert calls == []


@pytest.mark.parametrize(
    "arguments",
    [["--show-hit-pages"], ["--search", "営業利益", "--show-hit-pages", "--context", "-1"]],
)
def test_invalid_full_page_options_fail_before_pdf_read(tmp_path, arguments):
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "not-read.pdf"), *arguments])
    assert error.value.code == 2


def test_all_empty_extracted_pages_are_not_a_successful_absence_check(
    tmp_path, monkeypatch, capsys
):
    path, calls = _mock_pdf(tmp_path, monkeypatch, [None, " "])
    assert main([str(path), "--search", "営業利益", "--show-hit-pages"]) == 1
    assert calls == [1, 2]
    err = capsys.readouterr().err
    assert "no searchable extracted text on pages: [1, 2]" in err
    assert "no match in the selected pages' extracted text" in err


def test_full_hit_pages_exclude_nonmatching_pages_and_pair_numbers_with_bodies(
    tmp_path, monkeypatch, capsys
):
    texts = ["unused"] * 7
    texts[6] = "p7本文\n営業利益700"
    texts[2] = "p3だけの非一致本文"
    texts[4] = "p5本文\n営業利益500"
    path, calls = _mock_pdf(tmp_path, monkeypatch, texts)
    assert main([str(path), "--pages", "7,3,5", "--search", "営業利益", "--show-hit-pages"]) == 0
    assert calls == [7, 3, 5]
    out = capsys.readouterr().out
    first = "=== page 7 [営業利益]\np7本文\n営業利益700"
    second = "=== page 5 [営業利益]\np5本文\n営業利益500"
    assert first in out
    assert second in out
    assert out.index(first) < out.index(second)
    assert "=== page 3" not in out
    assert texts[2] not in out


@pytest.mark.parametrize("arguments", [[], ["--pages", "2,1"]])
def test_whole_page_read_warns_and_fails_when_selected_text_is_empty(
    tmp_path, monkeypatch, capsys, arguments
):
    path, calls = _mock_pdf(tmp_path, monkeypatch, [None, " \n"])

    assert main([str(path), *arguments]) == 1

    selected = [2, 1] if arguments else [1]
    assert calls == selected
    output = capsys.readouterr()
    assert f"no searchable extracted text on pages: {selected}" in output.err
    assert "inspect the original PDF" in output.err
    for number in selected:
        assert f"=== page {number}" in output.out


def test_whole_page_read_keeps_readable_pages_and_names_empty_pages(tmp_path, monkeypatch, capsys):
    path, calls = _mock_pdf(tmp_path, monkeypatch, [None, "年度見出し\n営業利益200"])

    assert main([str(path), "--pages", "2,1"]) == 0

    assert calls == [2, 1]
    output = capsys.readouterr()
    assert "=== page 2\n年度見出し\n営業利益200" in output.out
    assert "no searchable extracted text on pages: [1]" in output.err
