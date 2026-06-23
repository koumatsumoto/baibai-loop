from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol

import requests

from ..db import ObservationRecord
from ..definitions import SeriesDefinition

HTTP_TIMEOUT_SECONDS = 30
MAX_CSV_RESPONSE_BYTES = 8_000_000
MAX_ZIP_RESPONSE_BYTES = 16_000_000


class IndicatorsProviderError(RuntimeError):
    """Raised when an indicator provider cannot return requested observations."""


class FetchContext:
    """Shared HTTP session plus a per-run bytes cache.

    The bytes cache lets bulk-file providers (FRB H.15, ECB FX) download one
    shared file once and reuse it across every series that maps to it.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.bytes_cache: dict[tuple[str, tuple[tuple[str, str], ...]], bytes] = {}

    def close(self) -> None:
        self.session.close()


class HttpSession(Protocol):
    """Structural seam for HTTP fetching so tests can inject fake sessions."""

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> requests.Response: ...


class MacroDataProvider(Protocol):
    """A macro data source. Each provider isolates its own auth/parse/quirks."""

    name: str

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]: ...


def fetch_text(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    context: FetchContext | None = None,
) -> str:
    content = fetch_bytes(session, url, params=params, max_bytes=max_bytes, context=context)
    return content.decode("utf-8-sig")


def fetch_bytes(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    context: FetchContext | None = None,
) -> bytes:
    key = (url, tuple(sorted((params or {}).items())))
    if context is not None and key in context.bytes_cache:
        return context.bytes_cache[key]
    try:
        response = session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS, stream=True)
        _raise_for_response(response, url)
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError:
                parsed_length = None
            if parsed_length is not None and parsed_length > max_bytes:
                raise IndicatorsProviderError(
                    f"indicator response too large: {parsed_length} bytes"
                )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise IndicatorsProviderError(f"indicator response too large: {total} bytes")
            chunks.append(chunk)
        content = b"".join(chunks)
    except requests.RequestException as exc:
        raise IndicatorsProviderError(f"failed to fetch {url}: {exc}") from exc
    if context is not None:
        context.bytes_cache[key] = content
    return content


def _raise_for_response(response: requests.Response, url: str) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise IndicatorsProviderError(f"failed to fetch {url}: {exc}") from exc


def parse_float(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except ValueError as exc:
        raise IndicatorsProviderError(f"invalid numeric value: {value!r}") from exc


def parse_optional_float(value: str | None) -> float | None:
    if value is None or not value or value in {"ND", "."}:
        return None
    return parse_float(value)


def record_observation(
    series: SeriesDefinition, *, observed_at: date, value: float
) -> ObservationRecord:
    return ObservationRecord(
        series_id=series.series_id,
        observed_at=observed_at,
        period_start=observed_at,
        period_end=observed_at,
        value=value,
        unit=series.unit,
        vintage_at=datetime.now(UTC),
        fetch_status="ok",
        source_url=series.source_url,
    )
