"""Read a locally downloaded IR PDF (決算短信 等) as text.

The reader uses `pypdf[crypto]` for local PDFs, including supported AES-encrypted
files. Keyword search extracts figures with surrounding context; whole-page output
is available for reading a section end to end. Text extraction is not OCR and does
not establish that a term is absent from images or unreadable page content.

The file must already be on disk — this tool does not fetch URLs, so the retrieval step
stays visible in the session that recorded `retrieved_at` for the assessment source.

    uv run python -m baibai_engine.research.pdf_reader <pdf> --pages 1-3
    uv run python -m baibai_engine.research.pdf_reader <pdf> --search 減価償却費 自己株式の取得
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

DEFAULT_CONTEXT_CHARS = 320


@dataclass(frozen=True, slots=True)
class KeywordHit:
    page_number: int
    keyword: str
    context: str


def parse_page_selection(spec: str, page_count: int) -> tuple[int, ...]:
    """Turn `1-3,10` into 1-based page numbers.

    A range that starts inside the document but runs past its end is clamped: the
    page count is not known until the file is open, so `--pages 1-4` is how a caller
    asks for "the opening section" of a 短信 whose length varies by company. A range
    that starts past the end, or a single page past the end, still raises — there the
    caller is asking for something that does not exist rather than for a tail.
    """

    pages: list[int] = []
    for part in spec.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_text, _, end_text = chunk.partition("-")
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(chunk)
        if start < 1 or end < start or start > page_count:
            raise ValueError(f"page range out of bounds: {chunk} (document has {page_count})")
        pages.extend(range(start, min(end, page_count) + 1))
    if not pages:
        raise ValueError("no page selected")
    return tuple(dict.fromkeys(pages))


def find_keyword_contexts(
    pages: Sequence[str], keywords: Iterable[str], *, context_chars: int = DEFAULT_CONTEXT_CHARS
) -> tuple[KeywordHit, ...]:
    """Collect context windows for each keyword, keeping page order.

    Occurrences whose window overlaps one already reported for the same keyword and page
    are skipped: a 短信 repeats a term several times inside one paragraph, and printing
    that paragraph once per occurrence buries the other pages. Separate mentions on the
    same page still come through, because a figure quoted in the summary and in the
    statements are two different readings and which one an assessment cites matters.
    """

    if context_chars < 0:
        raise ValueError("context_chars must be non-negative")
    keywords = tuple(keywords)
    if "" in keywords:
        raise ValueError("search keyword must not be empty")
    hits: list[KeywordHit] = []
    for index, text in enumerate(pages, start=1):
        for keyword in keywords:
            start = 0
            reported_until = -1
            while True:
                position = text.find(keyword, start)
                if position < 0:
                    break
                start = position + len(keyword)
                window_start = max(0, position - context_chars // 2)
                window_end = min(len(text), position + len(keyword) + context_chars)
                if window_start < reported_until:
                    continue
                reported_until = window_end
                hits.append(
                    KeywordHit(
                        page_number=index,
                        keyword=keyword,
                        context=" ".join(text[window_start:window_end].split()),
                    )
                )
    return tuple(hits)


def missing_keywords(keywords: Sequence[str], hits: Sequence[KeywordHit]) -> tuple[str, ...]:
    """Keywords that matched nothing, in the order they were requested.

    `--search` takes space-separated keywords, so a comma-joined list arrives as one
    long keyword that hits nothing — indistinguishable from "the document does not say
    this" unless the failing string is echoed back. Naming the misses also separates a
    genuinely absent term from a mistyped one when other keywords did hit.
    """

    found = {hit.keyword for hit in hits}
    return tuple(keyword for keyword in keywords if keyword not in found)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m baibai_engine.research.pdf_reader", description=__doc__
    )
    parser.add_argument("pdf", type=Path, help="path to a locally downloaded PDF")
    parser.add_argument("--pages", help="1-based page selection, e.g. 1-3,10")
    parser.add_argument("--search", nargs="+", help="keywords to locate with context")
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

    if args.search:
        pages = tuple(page.extract_text() or "" for page in reader.pages)
        hits = find_keyword_contexts(pages, args.search, context_chars=args.context)
        missing = missing_keywords(args.search, hits)
        if missing:
            print(f"no hit for: {list(missing)}", file=sys.stderr)
        if not hits:
            return 1
        for hit in hits:
            print(f"--- p{hit.page_number} [{hit.keyword}]")
            print(hit.context)
        return 0

    selection = parse_page_selection(args.pages, page_count) if args.pages else (1,)
    for page_number in selection:
        print(f"=== page {page_number}")
        print(reader.pages[page_number - 1].extract_text() or "")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
