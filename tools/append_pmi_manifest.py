"""Find and verify the current month's S&P Global PMI release URLs for the manifest.

The `spglobal_pmi` provider reads each month's headline from the release PDF that
month's manifest entry names, and the manifest is maintained by hand. The provider
fails a refresh once a published month is missing from it, so the append is not a
task that can be forgotten — it is a task that recurs every month for four streams.

This finds the release the publisher's press-release index currently lists for each
stream, proves it is the release it claims to be by reading the headline out of the
PDF for the expected month, and writes the verified entry into the manifest. The
index lists roughly the current month only, so an earlier month that is missing
stays a manual job; that gap is reported rather than papered over.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from html import unescape
from pathlib import Path

from baibai_engine.macro.indicators.providers.base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
)
from baibai_engine.macro.indicators.providers.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    MANIFEST_PATH,
    load_manifest,
    release_text,
)

RELEASE_INDEX_URL = "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=en"

# The index page states each release as a date, a title, and the release link. The
# title is the only thing that says which PMI a release belongs to, so a stream is
# pinned to the exact title its publisher uses rather than to a keyword.
STREAM_TITLES = {
    "jp_manufacturing": "S&P Global Japan Manufacturing PMI",
    "jp_services": "S&P Global Japan Services PMI",
    "us_manufacturing": "S&P Global US Manufacturing PMI",
    "us_services": "S&P Global US Services PMI",
}

_LIST_ITEM_RE = re.compile(
    r'<div class="listItem">\s*'
    r'<span class="releaseDate">(?P<published>.*?)</span>\s*'
    r'<span class="releaseTitle">(?P<title>.*?)</span>\s*'
    r'<span class="greenListItem"><a href="(?P<url>[^"]+)"',
    re.S,
)
_PUBLISHED_RE = re.compile(r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+(?P<year>\d{4})")
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# The manifest nests stream keys two spaces under `series:`, and each entry is a
# list item indented four. A stream's block runs until the next key at that depth.
_STREAM_KEY_RE = re.compile(r"^  (?P<stream>\w+):$")
_MAX_INDEX_BYTES = 4 * 1024 * 1024
_INDEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One release the publisher's index currently lists."""

    published_on: date
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A release the manifest does not name yet, and the month it should carry."""

    stream: str
    observed_at: date
    url: str


@dataclass(frozen=True, slots=True)
class StreamOutcome:
    stream: str
    detail: str
    resolved: bool


def release_index_entries(context: FetchContext) -> tuple[IndexEntry, ...]:
    """The releases the index lists, over plain HTTP or through a real browser.

    The publisher's edge WAF answers a plain HTTP client with a challenge page
    carrying a 200, exactly as it does for the release PDFs. A page that names no
    release is therefore treated as a page that was not served, and the same
    headless browser the PDF path falls back to is asked for the rendered HTML.
    """

    try:
        html = fetch_text(
            context.session,
            RELEASE_INDEX_URL,
            params=None,
            max_bytes=_MAX_INDEX_BYTES,
            headers=_INDEX_HEADERS,
            context=context,
        )
    except (IndicatorsProviderError, UnicodeDecodeError):
        html = ""
    entries = parse_release_index(html)
    if entries:
        return entries
    # A challenge page caches as if it were the index, so the retry must not read it.
    context.discard_cached_bytes()
    return parse_release_index(context.browser_fetcher().fetch_html(RELEASE_INDEX_URL))


def parse_release_index(html: str) -> tuple[IndexEntry, ...]:
    """The releases the index lists, in the order the page states them."""

    entries: list[IndexEntry] = []
    for match in _LIST_ITEM_RE.finditer(html):
        published_on = _parse_published(unescape(match["published"]).replace("\xa0", " "))
        if published_on is None:
            continue
        entries.append(
            IndexEntry(
                published_on=published_on,
                title=" ".join(unescape(match["title"]).split()),
                url=unescape(match["url"]),
            )
        )
    return tuple(entries)


def _parse_published(raw: str) -> date | None:
    match = _PUBLISHED_RE.search(raw)
    if match is None:
        return None
    try:
        month = _MONTH_NAMES.index(match["month"].capitalize()) + 1
    except ValueError:
        return None
    return date(int(match["year"]), month, int(match["day"]))


def observed_month_of(published_on: date) -> date:
    """The month a release reports, which is the month before it is published."""

    return _previous_month(published_on)


def _previous_month(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def newest_candidate(entries: tuple[IndexEntry, ...], *, stream: str) -> Candidate | None:
    """The newest release the index lists for ``stream``, or None when it lists none."""

    title = STREAM_TITLES[stream]
    matching = [entry for entry in entries if entry.title == title]
    if not matching:
        return None
    newest = max(matching, key=lambda entry: entry.published_on)
    return Candidate(
        stream=stream,
        observed_at=observed_month_of(newest.published_on),
        url=newest.url,
    )


def verified_value(
    candidate: Candidate, *, session: HttpSession, context: FetchContext | None
) -> float:
    """The headline the candidate's PDF states for the month it is claimed to carry.

    This is what makes the append safe to automate: a title that maps to the wrong
    release, or a release whose month is not the month the publication date implies,
    cannot produce a reading for the expected month.
    """

    text = release_text(candidate.url, session=session, context=context)
    try:
        value = extract_pmi_value(
            text,
            expected_observed_at=candidate.observed_at,
            release_observed_at=candidate.observed_at,
        )
    except PmiExtractionError as exc:
        raise IndicatorsProviderError(
            f"{candidate.stream} {candidate.observed_at}: {exc} ({candidate.url})"
        ) from exc
    if value is None:
        raise IndicatorsProviderError(
            f"{candidate.stream} {candidate.observed_at}: no headline value for that month "
            f"in {candidate.url}"
        )
    return value


def manifest_with_entry(text: str, candidate: Candidate) -> str:
    """The manifest text with ``candidate`` appended to the end of its stream block."""

    lines = text.splitlines(keepends=True)
    start = _stream_block_start(lines, stream=candidate.stream)
    end = _stream_block_end(lines, start=start)
    addition = (
        f"    - observed_at: {candidate.observed_at.isoformat()}\n      url: {candidate.url}\n"
    )
    return "".join(lines[:end]) + addition + "".join(lines[end:])


def _stream_block_start(lines: list[str], *, stream: str) -> int:
    for index, line in enumerate(lines):
        match = _STREAM_KEY_RE.match(line.rstrip("\n"))
        if match is not None and match["stream"] == stream:
            return index + 1
    raise IndicatorsProviderError(f"PMI manifest has no stream named {stream!r}")


def _stream_block_end(lines: list[str], *, start: int) -> int:
    for index in range(start, len(lines)):
        if _STREAM_KEY_RE.match(lines[index].rstrip("\n")) is not None:
            return index
    return len(lines)


def resolve_stream(
    stream: str,
    entries: tuple[IndexEntry, ...],
    *,
    session: HttpSession,
    context: FetchContext | None,
    manifest_path: Path,
    write: bool,
) -> StreamOutcome:
    stored = load_manifest(manifest_path).get(stream)
    newest_stored = max(release.observed_at for release in stored) if stored else None
    candidate = newest_candidate(entries, stream=stream)
    if candidate is None:
        return StreamOutcome(
            stream,
            f"the index lists no release titled {STREAM_TITLES[stream]!r}",
            resolved=False,
        )
    if newest_stored is not None and candidate.observed_at <= newest_stored:
        return StreamOutcome(
            stream,
            f"already current through {newest_stored.isoformat()}",
            resolved=True,
        )
    value = verified_value(candidate, session=session, context=context)
    detail = f"{candidate.observed_at.isoformat()} = {value} from {candidate.url}"
    if newest_stored is not None and _previous_month(candidate.observed_at) > newest_stored:
        detail += (
            f"; months after {newest_stored.isoformat()} and before "
            f"{candidate.observed_at.isoformat()} are still missing and the index no "
            f"longer lists them"
        )
    if not write:
        return StreamOutcome(stream, f"{detail} (not written)", resolved=True)
    _append_verified_entry(candidate, manifest_path=manifest_path)
    return StreamOutcome(stream, f"{detail} (appended)", resolved=True)


def _append_verified_entry(candidate: Candidate, *, manifest_path: Path) -> None:
    """Write the entry, then re-read the manifest through the provider's own loader.

    The manifest is edited as text so the hand-written file keeps its shape, which
    means the schema is only proven by loading it back. A load that fails leaves the
    file as it was rather than half-edited.
    """

    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(manifest_with_entry(original, candidate), encoding="utf-8")
    load_manifest.cache_clear()
    try:
        stored = load_manifest(manifest_path)[candidate.stream]
    except (IndicatorsProviderError, KeyError):
        manifest_path.write_text(original, encoding="utf-8")
        load_manifest.cache_clear()
        raise
    if not any(
        release.observed_at == candidate.observed_at and release.url == candidate.url
        for release in stored
    ):
        manifest_path.write_text(original, encoding="utf-8")
        load_manifest.cache_clear()
        raise IndicatorsProviderError(
            f"{candidate.stream} {candidate.observed_at} did not survive a manifest reload"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="append the current month's verified PMI release URLs to the manifest"
    )
    parser.add_argument(
        "--stream",
        action="append",
        choices=sorted(STREAM_TITLES),
        help="limit to one stream (repeatable); default is every stream",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify and report without writing the manifest",
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    streams = args.stream or sorted(STREAM_TITLES)
    outcomes: list[StreamOutcome] = []
    with FetchContext(purpose="refresh") as context:
        try:
            entries = release_index_entries(context)
        except IndicatorsProviderError as exc:
            print(f"error: could not read the PMI release index: {exc}", file=sys.stderr)
            return 1
        if not entries:
            print(
                "error: the PMI release index answered but named no releases; the page "
                "structure has changed and the append stays manual",
                file=sys.stderr,
            )
            return 1
        for stream in streams:
            try:
                outcomes.append(
                    resolve_stream(
                        stream,
                        entries,
                        session=context.session,
                        context=context,
                        manifest_path=args.manifest,
                        write=not args.dry_run,
                    )
                )
            except (IndicatorsProviderError, OSError) as exc:
                outcomes.append(StreamOutcome(stream, str(exc), resolved=False))
    for outcome in outcomes:
        marker = "ok" if outcome.resolved else "unresolved"
        print(f"{marker}: {outcome.stream}: {outcome.detail}")
    unresolved = [outcome.stream for outcome in outcomes if not outcome.resolved]
    if unresolved:
        print(
            f"error: {len(unresolved)} stream(s) still need a hand-added entry: "
            f"{', '.join(unresolved)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
