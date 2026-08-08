from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import cast

from pypdf import PdfReader

from baibai_engine.foundation.yaml_io import strict_safe_load

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    SourceWithheldError,
    fetch_bytes,
)
from .pmi_extraction import PmiExtractionError, extract_pmi_value

MANIFEST_PATH = Path(__file__).with_name("pmi_release_urls.yaml")
RELEASE_URL_RE = re.compile(
    r"https://www\.pmi\.spglobal\.com/Public/Home/PressRelease/[0-9a-f]{32}\Z"
)
MAX_PMI_PDF_BYTES = 5 * 1024 * 1024
# S&P Global publishes a month's final release in the first business days of the
# next month, so a refresh through a given month expects the manifest to reach the
# previous month once that release window has passed. Before the grace day the
# expectation stays one month further back, so the guard never fails for a release
# that does not exist yet.
MANIFEST_GRACE_DAY = 10


class SpGlobalPmiProvider:
    """S&P Global PMI headline index from the official monthly release PDFs.

    ``provider_series_id`` selects a PMI stream (e.g. ``jp_manufacturing``) in the
    release-URL manifest. Each observed month names its release PDF; the provider
    fetches the PDF (plain HTTP first, headless-browser fallback for WAF-gated
    months), extracts the headline value from a bounded context, and range-checks
    it before it can enter the store.

    Fetch cost scales with the number of months in the window (one PDF each), so an
    incremental refresh fetches only the months the store is missing. A final
    headline reading is not revised once published, so re-reading a stored month
    yields the same value; ``refresh --all-history`` re-reads every month and is the
    way to rebuild the stream from source.
    """

    # The manifest is the whole obtainable history, so the all-history floor has to
    # reach the oldest month it names; otherwise `--all-history` silently leaves the
    # months before the floor unfetchable. A test binds this date to the manifest.
    spec = ProviderSpec(name="spglobal_pmi", all_history_start=date(2022, 12, 1))
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        releases = load_manifest(MANIFEST_PATH).get(series.provider_series_id)
        if releases is None:
            supported = ", ".join(sorted(load_manifest(MANIFEST_PATH)))
            raise IndicatorsProviderError(
                f"spglobal_pmi has no release manifest for {series.provider_series_id!r}; "
                f"supported: {supported}"
            )
        if context is None or context.purpose != "read":
            _require_current_manifest(series, releases, end=end)
        stored_by_month = {
            observation.observed_at: observation
            for observation in _stored_observations(series, start=start, end=end, context=context)
        }
        fetched: list[ObservationRecord] = []
        refetched_months: set[date] = set()
        for entry in releases:
            if not start <= entry.observed_at <= end:
                continue
            stored = stored_by_month.get(entry.observed_at)
            if stored is not None and stored.source_url == entry.url:
                continue
            refetched_months.add(entry.observed_at)
            pdf_text = release_text(entry.url, session=session, context=context)
            try:
                value = extract_pmi_value(
                    pdf_text,
                    expected_observed_at=entry.observed_at,
                    release_observed_at=entry.release_observed_at,
                )
            except PmiExtractionError as exc:
                raise IndicatorsProviderError(
                    f"spglobal_pmi {series.series_id} {entry.observed_at}: {exc}"
                ) from exc
            if value is None:
                raise IndicatorsProviderError(
                    f"spglobal_pmi {series.series_id} {entry.observed_at}: "
                    f"no headline value found in {entry.url}"
                )
            fetched.append(
                ObservationRecord(
                    series_id=series.series_id,
                    observed_at=entry.observed_at,
                    period_start=entry.observed_at,
                    period_end=entry.observed_at,
                    value=value,
                    unit=series.unit,
                    source_url=entry.url,
                )
            )
        # Months that were not re-read are carried through from the store so the
        # returned window stays the complete window even though nothing was fetched
        # for them. Re-inserting them is a no-op for the store.
        carried = [
            observation
            for month, observation in stored_by_month.items()
            if month not in refetched_months
        ]
        return sorted(fetched + carried, key=lambda observation: observation.observed_at)


def _stored_observations(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext | None,
) -> tuple[ObservationRecord, ...]:
    """Observations already held for this series, whose months are not downloaded.

    A month is only skipped when the stored observation came from the release URL
    the manifest currently names, so correcting a URL re-reads that month. A
    rebuild re-reads every month regardless. Without a store reader every month in
    the window is fetched, so a caller that cannot supply one still gets correct
    data — only the download count changes.
    """

    if context is None or context.store_reader is None or context.purpose == "rebuild":
        return ()
    return context.store_reader(series.series_id, start, end)


def _require_current_manifest(
    series: SeriesDefinition, releases: tuple[Release, ...], *, end: date
) -> None:
    """Fail when the manifest has fallen behind the release calendar.

    The manifest is maintained by hand, and a month missing from it is otherwise
    invisible: the fetch simply returns nothing for that month and the series goes
    stale with no error anywhere. Comparing the newest manifest month against the
    month the release calendar implies for ``end`` turns that silence into a
    failure the batch reports.
    """

    newest = max(release.observed_at for release in releases)
    expected = _expected_newest_month(end)
    if newest < expected:
        raise IndicatorsProviderError(
            f"spglobal_pmi manifest for {series.provider_series_id} ends at "
            f"{newest.isoformat()} but a refresh through {end.isoformat()} expects "
            f"{expected.isoformat()}; add the published release URLs to "
            f"{MANIFEST_PATH.name}"
        )


def _expected_newest_month(end: date) -> date:
    months_back = 1 if end.day >= MANIFEST_GRACE_DAY else 2
    month = end.replace(day=1)
    for _ in range(months_back):
        month = (month - timedelta(days=1)).replace(day=1)
    return month


@dataclass(frozen=True, slots=True)
class Release:
    """One month of a PMI stream and the release PDF it is read from."""

    observed_at: date
    release_observed_at: date
    url: str


def release_text(url: str, *, session: HttpSession, context: FetchContext | None) -> str:
    content = _fetch_pdf_bytes(url, session=session, context=context)
    return extract_pdf_text(content)


def _fetch_pdf_bytes(url: str, *, session: HttpSession, context: FetchContext | None) -> bytes:
    # Plain HTTP works for most months; a WAF-gated month returns non-PDF HTML,
    # so fall back to a real browser navigation before giving up. Only a source
    # that withheld the file is worth asking again a costlier way: a size cap, a
    # 404 or a 5xx would answer the same, and routing a capped response through
    # the browser would have it written to disk with no cap ahead of it.
    try:
        content = fetch_bytes(
            session,
            url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            context=context,
        )
    except SourceWithheldError:
        content = b""
    if content.startswith(b"%PDF"):
        if len(content) > MAX_PMI_PDF_BYTES:
            raise IndicatorsProviderError(f"spglobal_pmi PDF exceeds {MAX_PMI_PDF_BYTES} bytes")
        return content
    if context is None:
        raise IndicatorsProviderError(
            f"spglobal_pmi could not fetch a PDF from {url} and no browser is available"
        )
    # A WAF-gated month always arrives this way, so the browser route is normal
    # operation rather than an exceptional one and carries the same ceiling.
    return context.browser_fetcher().fetch_pdf(url, max_bytes=MAX_PMI_PDF_BYTES)


def extract_pdf_text(content: bytes) -> str:
    if not content.startswith(b"%PDF"):
        raise IndicatorsProviderError("spglobal_pmi response is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        # pypdf raises opaque third-party errors on a malformed PDF; convert so
        # the CLI/batch never leaks a traceback.
        raise IndicatorsProviderError(f"spglobal_pmi PDF could not be read: {exc}") from exc
    return " ".join(text.split())


@cache
def load_manifest(path: Path) -> dict[str, tuple[Release, ...]]:
    """The manifest at ``path``, parsed and validated.

    The path is required rather than defaulted so one file cannot end up behind
    two cache keys, which would let a caller read a copy the writer has replaced.
    """

    raw = strict_safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise IndicatorsProviderError("PMI release manifest must use schema_version 2")
    series_node = raw.get("series")
    if not isinstance(series_node, dict):
        raise IndicatorsProviderError("PMI release manifest 'series' must be a mapping")
    result: dict[str, tuple[Release, ...]] = {}
    for stream, entries in cast(Mapping[str, object], series_node).items():
        if not isinstance(entries, list):
            raise IndicatorsProviderError(f"PMI manifest stream {stream!r} must be a list")
        result[str(stream)] = _parse_stream(str(stream), entries)
    return result


def _parse_stream(stream: str, entries: list[object]) -> tuple[Release, ...]:
    releases: list[Release] = []
    seen: set[date] = set()
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise IndicatorsProviderError(f"PMI manifest {stream} entry {index} must be a mapping")
        entry = cast(Mapping[str, object], raw_entry)
        observed_at = _entry_date(entry.get("observed_at"), stream=stream)
        if observed_at.day != 1:
            raise IndicatorsProviderError(
                f"PMI manifest {stream} observed_at must be the first of a month: {observed_at}"
            )
        if observed_at in seen:
            raise IndicatorsProviderError(
                f"PMI manifest {stream} has duplicate month {observed_at}"
            )
        seen.add(observed_at)
        release_raw = entry.get("release_observed_at")
        release_observed_at = (
            _entry_date(release_raw, stream=stream) if release_raw is not None else observed_at
        )
        url = entry.get("url")
        if not isinstance(url, str) or RELEASE_URL_RE.fullmatch(url) is None:
            raise IndicatorsProviderError(
                f"PMI manifest {stream} {observed_at} has an invalid release URL: {url!r}"
            )
        releases.append(Release(observed_at, release_observed_at, url))
    return tuple(sorted(releases, key=lambda item: item.observed_at))


def _entry_date(value: object, *, stream: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise IndicatorsProviderError(
                f"PMI manifest {stream} date is not ISO format: {value!r}"
            ) from exc
    raise IndicatorsProviderError(f"PMI manifest {stream} date must be a date: {value!r}")
