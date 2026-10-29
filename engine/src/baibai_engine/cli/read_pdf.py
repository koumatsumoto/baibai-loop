"""取得済みPDFのpage・keyword文脈を表示する。空抽出は原本確認を促す。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pypdf import PdfReader

from baibai_engine.investment_judgment.company_research.pdf_reader import (
    DEFAULT_CONTEXT_CHARS,
    find_keyword_contexts,
    missing_keywords,
    parse_page_selection,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baibai_engine.cli.read_pdf",
        description=__doc__,
    )
    parser.add_argument("pdf", type=Path, help="path to a locally downloaded PDF")
    parser.add_argument("--pages", help="1-based page selection, e.g. 1-3,10")
    parser.add_argument("--search", nargs="+", help="keywords to locate with context")
    parser.add_argument(
        "--show-hit-pages",
        action="store_true",
        help="print matching pages in full; requires --search; ignores --context for output",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=DEFAULT_CONTEXT_CHARS,
        help=f"characters of context per hit (default {DEFAULT_CONTEXT_CHARS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.show_hit_pages and not args.search:
        parser.error("--show-hit-pages requires --search")
    if args.context < 0:
        parser.error("--context must be non-negative")
    if args.search is not None and "" in args.search:
        parser.error("search keyword must not be empty")

    if not args.pdf.is_file():
        print(f"no such PDF: {args.pdf}", file=sys.stderr)
        return 2
    reader = PdfReader(args.pdf)
    page_count = len(reader.pages)
    print(f"{args.pdf} — {page_count} page(s)")

    try:
        if args.pages is not None:
            selection = parse_page_selection(args.pages, page_count)
        elif args.search:
            selection = tuple(range(1, page_count + 1))
        else:
            selection = (1,)
    except ValueError as exc:
        parser.error(str(exc))

    if args.search:
        keywords = tuple(dict.fromkeys(args.search))
        pages = tuple(reader.pages[number - 1].extract_text() or "" for number in selection)
        empty_pages = [
            number for number, text in zip(selection, pages, strict=True) if not text.strip()
        ]
        if empty_pages:
            print(
                f"no searchable extracted text on pages: {empty_pages}; inspect the original PDF",
                file=sys.stderr,
            )
        hits = find_keyword_contexts(pages, keywords, context_chars=args.context)
        missing = missing_keywords(keywords, hits)
        if missing:
            print(f"no hit for: {list(missing)}", file=sys.stderr)
        if not hits:
            print("no match in the selected pages' extracted text", file=sys.stderr)
            return 1
        if args.show_hit_pages:
            for index, text in enumerate(pages, start=1):
                matched = tuple(
                    dict.fromkeys(hit.keyword for hit in hits if hit.page_number == index)
                )
                if matched:
                    print(f"=== page {selection[index - 1]} [{', '.join(matched)}]")
                    print(text)
        else:
            for hit in hits:
                print(f"--- p{selection[hit.page_number - 1]} [{hit.keyword}]")
                print(hit.context)
        return 0

    empty_pages = []
    for page_number in selection:
        print(f"=== page {page_number}")
        text = reader.pages[page_number - 1].extract_text() or ""
        print(text)
        if not text.strip():
            empty_pages.append(page_number)
    if empty_pages:
        print(
            f"no searchable extracted text on pages: {empty_pages}; inspect the original PDF",
            file=sys.stderr,
        )
    return 1 if len(empty_pages) == len(selection) else 0


if __name__ == "__main__":
    raise SystemExit(main())
