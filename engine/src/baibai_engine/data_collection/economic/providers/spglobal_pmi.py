from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import cast

from baibai_engine.data_collection.economic.documents import SourceDocument
from baibai_engine.data_collection.economic.provider_specs import ProviderSpec
from baibai_engine.data_collection.economic.sources import SourceDefinition
from baibai_engine.foundation.yaml_io import strict_safe_load

from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    SourceWithheldError,
    fetch_bytes,
)

MANIFEST_PATH = Path(__file__).with_name("pmi_release_urls.yaml")
RELEASE_URL_RE = re.compile(
    r"https://www\.pmi\.spglobal\.com/Public/Home/PressRelease/[0-9a-f]{32}\Z"
)
MAX_PMI_PDF_BYTES = 5 * 1024 * 1024
MANIFEST_GRACE_DAY = 10


class SpGlobalPmiSource:
    """Acquire original monthly PDFs; interpreting their headline belongs to L2.

    Every selected-range refresh rechecks known manifest URLs, including corrections
    published at an unchanged URL. Already received documents survive a later
    download or manifest-coverage failure because the caller stores each yield.
    """

    spec = ProviderSpec(name="spglobal_pmi", all_history_start=date(2022, 12, 1))
    name = spec.name

    def fetch_documents(
        self,
        source: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None,
        existing: Mapping[str, SourceDocument],
        force: bool,
        recheck_existing: bool = True,
    ) -> Iterator[SourceDocument]:
        manifest = load_manifest(MANIFEST_PATH)
        releases = manifest.get(source.provider_series_id)
        if releases is None:
            raise IndicatorsProviderError(
                f"spglobal_pmi has no release manifest for {source.provider_series_id!r}"
            )
        failures: list[str] = []
        for entry in releases:
            if not start <= entry.observed_at <= end:
                continue
            key = entry.observed_at.isoformat()
            metadata = {
                "observed_at": key,
                "release_observed_at": entry.release_observed_at.isoformat(),
            }
            stored = existing.get(key)
            if (
                not recheck_existing
                and not force
                and stored is not None
                and stored.source_url == entry.url
                and stored.metadata == metadata
                and stored.content_type == "application/pdf"
            ):
                continue
            try:
                body = fetch_pdf_bytes(entry.url, session=session, context=context)
            except ProviderFetchStoppedError:
                raise
            except SourceWithheldError as error:
                # This PMI route is currently refused with 403. Do not fan the
                # refusal out across the remaining monthly PDFs in this pass.
                raise ProviderFetchStoppedError(str(error)) from error
            except IndicatorsProviderError as error:
                failures.append(str(error))
                continue
            yield SourceDocument(
                source_id=source.source_id,
                request_key=key,
                source_url=entry.url,
                content_type="application/pdf",
                fetched_at=datetime.now(UTC),
                payload=body,
                metadata=metadata,
            )
        # Report an unavailable recent range without throwing away known originals.
        try:
            _require_current_manifest(source, releases, end=end)
            _require_target_months(releases, start=start, end=end)
        except IndicatorsProviderError as error:
            failures.append(str(error))
        if failures:
            raise IndicatorsProviderError(
                f"spglobal_pmi incomplete: {len(failures)} document/manifest failures; "
                + "; ".join(failures[:2])
            )


def _require_target_months(releases: tuple[Release, ...], *, start: date, end: date) -> None:
    first = max(start, SpGlobalPmiSource.spec.all_history_start or start)
    if first.day != 1:
        first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    expected = _expected_newest_month(end)
    available = {release.observed_at for release in releases}
    missing: list[str] = []
    while first <= expected:
        if first not in available:
            missing.append(first.isoformat())
        first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    if missing:
        raise IndicatorsProviderError(
            "spglobal_pmi manifest is missing target months: " + ", ".join(missing)
        )


def fetch_pdf_bytes(url: str, *, session: HttpSession, context: FetchContext | None) -> bytes:
    """Fetch a public PDF directly; refusal is not permission to bypass the source."""
    content = fetch_bytes(
        session,
        url,
        params=None,
        max_bytes=MAX_PMI_PDF_BYTES,
        headers={"Accept": "application/pdf"},
        context=context,
    )
    if not content.startswith(b"%PDF"):
        raise IndicatorsProviderError(f"spglobal_pmi response from {url} is not a PDF")
    return content


def _require_current_manifest(
    series: SourceDefinition, releases: tuple[Release, ...], *, end: date
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


@cache
def load_manifest(path: Path) -> dict[str, tuple[Release, ...]]:
    """The manifest at ``path``, parsed and validated.

    Parsed results are cached by the supplied path. Writers must clear this cache
    after replacing the file; distinct path spellings can refer to the same file.
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
