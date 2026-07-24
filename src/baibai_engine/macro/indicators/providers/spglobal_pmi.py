from __future__ import annotations

import io
import re
from collections.abc import Mapping
from datetime import date
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
    fetch_bytes,
)
from .pmi_extraction import PmiExtractionError, extract_pmi_value

MANIFEST_PATH = Path(__file__).with_name("pmi_release_urls.yaml")
_RELEASE_URL_RE = re.compile(
    r"https://www\.pmi\.spglobal\.com/Public/Home/PressRelease/[0-9a-f]{32}\Z"
)
MAX_PMI_PDF_BYTES = 5 * 1024 * 1024


class SpGlobalPmiProvider:
    """S&P Global PMI headline index from the official monthly release PDFs.

    ``provider_series_id`` selects a PMI stream (e.g. ``jp_manufacturing``) in the
    release-URL manifest. Each observed month names its release PDF; the provider
    fetches the PDF (plain HTTP first, headless-browser fallback for WAF-gated
    months), extracts the headline value from a bounded context, and range-checks
    it before it can enter the store. Replaces hand-entered manual observations.
    """

    spec = ProviderSpec(name="spglobal_pmi", all_history_start=date(2023, 7, 1))
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
        releases = _manifest().get(series.provider_series_id)
        if releases is None:
            supported = ", ".join(sorted(_manifest()))
            raise IndicatorsProviderError(
                f"spglobal_pmi has no release manifest for {series.provider_series_id!r}; "
                f"supported: {supported}"
            )
        observations: list[ObservationRecord] = []
        for entry in releases:
            if not start <= entry.observed_at <= end:
                continue
            pdf_text = _release_text(entry.url, session=session, context=context)
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
            observations.append(
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
        return observations


class _Release:
    __slots__ = ("observed_at", "release_observed_at", "url")

    def __init__(self, observed_at: date, release_observed_at: date, url: str) -> None:
        self.observed_at = observed_at
        self.release_observed_at = release_observed_at
        self.url = url


def _release_text(url: str, *, session: HttpSession, context: FetchContext | None) -> str:
    content = _fetch_pdf_bytes(url, session=session, context=context)
    return extract_pdf_text(content)


def _fetch_pdf_bytes(url: str, *, session: HttpSession, context: FetchContext | None) -> bytes:
    # Plain HTTP works for most months; a WAF-gated month returns non-PDF HTML,
    # so fall back to a real browser navigation before giving up.
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
    except IndicatorsProviderError:
        content = b""
    if content.startswith(b"%PDF"):
        if len(content) > MAX_PMI_PDF_BYTES:
            raise IndicatorsProviderError(f"spglobal_pmi PDF exceeds {MAX_PMI_PDF_BYTES} bytes")
        return content
    if context is None:
        raise IndicatorsProviderError(
            f"spglobal_pmi could not fetch a PDF from {url} and no browser is available"
        )
    return context.browser_fetcher().fetch_pdf(url)


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
def _manifest() -> dict[str, tuple[_Release, ...]]:
    raw = strict_safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise IndicatorsProviderError("PMI release manifest must use schema_version 2")
    series_node = raw.get("series")
    if not isinstance(series_node, dict):
        raise IndicatorsProviderError("PMI release manifest 'series' must be a mapping")
    result: dict[str, tuple[_Release, ...]] = {}
    for stream, entries in cast(Mapping[str, object], series_node).items():
        if not isinstance(entries, list):
            raise IndicatorsProviderError(f"PMI manifest stream {stream!r} must be a list")
        result[str(stream)] = _parse_stream(str(stream), entries)
    return result


def _parse_stream(stream: str, entries: list[object]) -> tuple[_Release, ...]:
    releases: list[_Release] = []
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
        if not isinstance(url, str) or _RELEASE_URL_RE.fullmatch(url) is None:
            raise IndicatorsProviderError(
                f"PMI manifest {stream} {observed_at} has an invalid release URL: {url!r}"
            )
        releases.append(_Release(observed_at, release_observed_at, url))
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
