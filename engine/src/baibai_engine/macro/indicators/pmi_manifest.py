"""Find and verify the current month's S&P Global PMI release URLs for the manifest.

The `spglobal_pmi` provider reads each month's headline from the release PDF that
month's manifest entry names, and the manifest is maintained by hand. The provider
fails a refresh once a published month is missing from it, so the append is not a
task that can be forgotten — it is a task that recurs every month for four streams.

This finds the release the publisher's press-release index currently lists for each
stream and requires the release PDF to tie its headline to the month the entry would
claim before that entry is written. The index lists roughly the current month only,
so a month missing before it is a manual job, and appending across such a gap is
refused: the provider's own guard compares the newest manifest month against the
release calendar, so an entry written past a hole would silence it for good.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from baibai_engine.macro.indicators.providers.base import (
    FetchContext,
    IndicatorsProviderError,
    fetch_text,
)
from baibai_engine.macro.indicators.providers.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    MANIFEST_PATH,
    RELEASE_URL_RE,
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
# Every release opens with the moment it may be reported, in the publisher's local
# time zone ("Embargoed until 0930 JST 1 July 2026", "0945 EDT 1 July 2026"). It is
# the release's own account of when it was published.
_EMBARGO_RE = re.compile(
    r"Embargoed until\s+\d{3,4}\s+[A-Z]{2,4}\s+(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+"
    r"(?P<year>\d{4})"
)
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
class ReleaseCandidate:
    """A release the manifest does not name yet, and the month it should carry."""

    stream: str
    observed_at: date
    published_on: date
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
        url = unescape(match["url"])
        # The link comes from a page this tool does not control and ends up both in a
        # fetch and in the manifest, so anything that is not a release URL is dropped
        # here rather than carried to either.
        if published_on is None or RELEASE_URL_RE.fullmatch(url) is None:
            continue
        entries.append(
            IndexEntry(
                published_on=published_on,
                title=" ".join(unescape(match["title"]).split()),
                url=url,
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


def _add_months(value: date, months: int) -> date:
    total = value.month - 1 + months
    return date(value.year + total // 12, total % 12 + 1, 1)


def newest_release_candidate(
    entries: tuple[IndexEntry, ...], *, stream: str
) -> ReleaseCandidate | None:
    """The newest release the index lists for ``stream``, or None when it lists none."""

    title = STREAM_TITLES[stream]
    matching = [entry for entry in entries if entry.title == title]
    if not matching:
        return None
    newest = max(matching, key=lambda entry: entry.published_on)
    return ReleaseCandidate(
        stream=stream,
        observed_at=observed_month_of(newest.published_on),
        published_on=newest.published_on,
        url=newest.url,
    )


def verified_value(release_candidate: ReleaseCandidate, *, context: FetchContext) -> float:
    """The headline the release_candidate's PDF ties to the month the entry would claim.

    This is what makes the append safe to automate, and it only holds if the month is
    proven from the release text. ``extract_pmi_value`` reads a value the statement
    introduces without naming a month whenever the month asked for is the month the
    release reports, which would accept the release's own headline for any month at
    all. Naming a later month as the reported one leaves only the reader that proves
    the month from the text, so a release that does not tie its headline to this month
    yields nothing and the append falls back to being done by hand.
    """

    identity = f"{release_candidate.stream} {release_candidate.observed_at}"
    text = release_text(release_candidate.url, session=context.session, context=context)
    _require_release_identity(text, release_candidate=release_candidate)
    try:
        value = extract_pmi_value(
            text,
            expected_observed_at=release_candidate.observed_at,
            release_observed_at=_add_months(release_candidate.observed_at, 2),
        )
    except PmiExtractionError as exc:
        raise IndicatorsProviderError(f"{identity}: {exc} ({release_candidate.url})") from exc
    if value is None:
        raise IndicatorsProviderError(
            f"{identity}: no headline value for that month in {release_candidate.url}"
        )
    return value


def _require_release_identity(text: str, *, release_candidate: ReleaseCandidate) -> None:
    """Require the PDF to be the release of this PMI on the date the index gave.

    Proving the month is not enough on its own. The headline statement of a services
    release is read the same way as a manufacturing one, so a title mapped to the
    wrong PMI would still yield a value for the right month. The release names the
    PMI it belongs to and states its own embargo date, and both have to agree with
    what the index said before the headline is read at all.
    """

    identity = f"{release_candidate.stream} {release_candidate.observed_at}"
    title = STREAM_TITLES[release_candidate.stream]
    if title not in text:
        raise IndicatorsProviderError(
            f"{identity}: the release does not name {title!r} ({release_candidate.url})"
        )
    match = _EMBARGO_RE.search(text)
    if match is None:
        raise IndicatorsProviderError(
            f"{identity}: the release states no embargo "
            f"date to check the index against ({release_candidate.url})"
        )
    embargoed_on = _parse_published(f"{match['month']} {match['day']} {match['year']}")
    if embargoed_on is None or embargoed_on.replace(
        day=1
    ) != release_candidate.published_on.replace(day=1):
        raise IndicatorsProviderError(
            f"{identity}: the index dates the release "
            f"{release_candidate.published_on.isoformat()} but the release is embargoed until "
            f"{match[0]!r} ({release_candidate.url})"
        )


def manifest_with_entry(text: str, release_candidate: ReleaseCandidate) -> str:
    """The manifest text with ``release_candidate`` appended to the end of its stream block."""

    lines = text.splitlines(keepends=True)
    start = _stream_block_start(lines, stream=release_candidate.stream)
    end = _stream_block_end(lines, start=start)
    # A file whose last line has no newline would otherwise take the new entry onto
    # that line, which joins it to the value already there.
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    addition = (
        f"    - observed_at: {release_candidate.observed_at.isoformat()}\n"
        f"      url: {release_candidate.url}\n"
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
    context: FetchContext | None,
    manifest_path: Path,
    write: bool,
    today: date,
) -> StreamOutcome:
    stored = load_manifest(manifest_path).get(stream)
    newest_stored = max(release.observed_at for release in stored) if stored else None
    release_candidate = newest_release_candidate(entries, stream=stream)
    if release_candidate is None:
        return StreamOutcome(
            stream,
            f"the index lists no release titled {STREAM_TITLES[stream]!r}",
            resolved=False,
        )
    if newest_stored is not None and release_candidate.observed_at <= newest_stored:
        return StreamOutcome(
            stream,
            f"already current through {newest_stored.isoformat()}",
            resolved=True,
        )
    # A month cannot be reported before it has ended, so a release_candidate at or past the
    # current month is a misread date rather than a release. Accepting it would put
    # the manifest ahead of the calendar, which is what the provider's currency guard
    # reads to decide whether a month is missing.
    if release_candidate.observed_at >= today.replace(day=1):
        return StreamOutcome(
            stream,
            f"the index dates the newest release {release_candidate.observed_at.isoformat()}, "
            f"which is not a month that can have been reported yet",
            resolved=False,
        )
    # The provider compares only the newest manifest month against the release
    # calendar, so an entry written past a hole would satisfy that guard forever and
    # the skipped months would never be reported again.
    if newest_stored is not None and _previous_month(release_candidate.observed_at) > newest_stored:
        return StreamOutcome(
            stream,
            f"the manifest ends at {newest_stored.isoformat()} and the index only lists "
            f"{release_candidate.observed_at.isoformat()}; the months between have to be added "
            f"by hand first or the currency guard would stop reporting them",
            resolved=False,
        )
    if context is None:
        raise IndicatorsProviderError(
            f"{stream} needs a fetch to verify {release_candidate.observed_at} but no fetch "
            f"context was given"
        )
    value = verified_value(release_candidate, context=context)
    detail = f"{release_candidate.observed_at.isoformat()} = {value} from {release_candidate.url}"
    if not write:
        return StreamOutcome(stream, f"{detail} (not written)", resolved=True)
    _append_verified_entry(release_candidate, manifest_path=manifest_path)
    return StreamOutcome(stream, f"{detail} (appended)", resolved=True)


def _append_verified_entry(release_candidate: ReleaseCandidate, *, manifest_path: Path) -> None:
    """Add the entry to a copy, prove the copy loads, then move it into place.

    The manifest is edited as text so the hand-written file keeps its shape, which
    means the schema is only proven by loading it back. Proving it on a copy is what
    keeps a rejected entry from ever reaching the file the provider reads: the move
    is the only write to it, and it happens after the check.
    """

    original = manifest_path.read_text(encoding="utf-8")
    candidate_text = manifest_with_entry(original, release_candidate)
    # The copy lives beside the manifest so the move that follows stays within one
    # filesystem, and it is named before it is written so a failed write still has a
    # path to clean up. A temporary file is created private, so the manifest's own
    # permissions are carried over rather than replaced by the move.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed via the staged path below
        mode="w",
        encoding="utf-8",
        dir=manifest_path.parent,
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    staged = Path(handle.name)
    try:
        with handle:
            handle.write(candidate_text)
        staged.chmod(manifest_path.stat().st_mode & 0o7777)
        _require_entry_loads(staged, release_candidate)
        staged.replace(manifest_path)
    finally:
        staged.unlink(missing_ok=True)
        load_manifest.cache_clear()


def _require_entry_loads(staged: Path, release_candidate: ReleaseCandidate) -> None:
    identity = f"{release_candidate.stream} {release_candidate.observed_at}"
    load_manifest.cache_clear()
    try:
        stored = load_manifest(staged).get(release_candidate.stream, ())
    except (IndicatorsProviderError, yaml.YAMLError, ValueError) as exc:
        raise IndicatorsProviderError(
            f"{identity} would leave the manifest unreadable: {exc}"
        ) from exc
    if not any(
        release.observed_at == release_candidate.observed_at
        and release.url == release_candidate.url
        for release in stored
    ):
        raise IndicatorsProviderError(f"{identity} did not survive a manifest reload")


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
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    with FetchContext(purpose="refresh") as context:
        try:
            entries = release_index_entries(context)
        except (IndicatorsProviderError, ImportError) as exc:
            # Playwright is only needed for the browser fallback, so a missing
            # install surfaces here rather than at import time.
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
                        context=context,
                        manifest_path=args.manifest,
                        write=not args.dry_run,
                        today=today,
                    )
                )
            except (
                IndicatorsProviderError,
                OSError,
                yaml.YAMLError,
                ValueError,
                ImportError,
            ) as exc:
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
