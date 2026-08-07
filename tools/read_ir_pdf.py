"""Read a locally downloaded IR PDF (決算短信 等) as text.

TDnet and company IR distribute earnings releases as AES-encrypted PDFs with an empty
owner password, so the reader needs `pypdf[crypto]`. A 短信 runs 20-30 pages while a
thesis needs a handful of figures, so keyword search with surrounding context is the
mode that gets used; whole-page output is there for reading a section end to end.

The file must already be on disk — this tool does not fetch URLs, so the retrieval step
stays visible in the session that recorded `retrieved_at` for the thesis source.

    uv run python tools/read_ir_pdf.py <pdf> --pages 1-3
    uv run python tools/read_ir_pdf.py <pdf> --search 減価償却費 自己株式の取得
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
    statements are two different readings and which one a thesis cites matters.
    """

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


def _page_texts(path: Path) -> tuple[str, ...]:
    reader = PdfReader(path)
    return tuple(page.extract_text() or "" for page in reader.pages)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python tools/read_ir_pdf.py", description=__doc__)
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
    args = build_parser().parse_args(argv)

    if not args.pdf.is_file():
        print(f"no such PDF: {args.pdf}", file=sys.stderr)
        return 2
    pages = _page_texts(args.pdf)
    print(f"{args.pdf} — {len(pages)} page(s)")

    if args.search:
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

    selection = parse_page_selection(args.pages, len(pages)) if args.pages else (1,)
    for page_number in selection:
        print(f"=== page {page_number}")
        print(pages[page_number - 1])
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
