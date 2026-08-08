from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, Protocol

import requests

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from ..provider_specs import ProviderSpec as ProviderSpec
from ..provider_specs import RangeReplacementPolicy as RangeReplacementPolicy

if TYPE_CHECKING:
    from .browser import BrowserFetcher

HTTP_TIMEOUT_SECONDS = 30
MAX_CSV_RESPONSE_BYTES = 8_000_000
MAX_ZIP_RESPONSE_BYTES = 16_000_000

# Reads a stored series' observations (latest ok vintage per observed_at) for a
# derived provider that computes from other series. Bound to the live connection
# by the service so a derived fetch sees inputs already committed this run.
type StoreReader = Callable[[str, date, date], tuple[ObservationRecord, ...]]

# What a fetch pass is for. ``read`` serves a query whose window the store cannot
# answer yet, ``refresh`` keeps a series current, ``rebuild`` re-derives a series
# from source. Providers whose cost or strictness depends on this read it from the
# fetch context: a rebuild re-reads observations the store already holds, and a
# source-currency guard fails a refresh or rebuild but never blocks a read.
type FetchPurpose = Literal["read", "refresh", "rebuild"]


class IndicatorsProviderError(RuntimeError):
    """Raised when an indicator provider cannot return requested observations."""


class SourceWithheldError(IndicatorsProviderError):
    """A source declined to serve this client rather than failing to serve at all.

    An edge bot-mitigation withholds data it holds in two shapes: a success
    status carrying no body, and a 403. Both say the resource exists and that
    another route may still reach it, which is what separates them from a size
    cap, a 404, a 5xx or a network fault — none of those give a fallback anything
    to work with, so they stay plain :class:`IndicatorsProviderError` and no
    provider reads them as an invitation to fetch the same thing a costlier way.
    """


class BrowserUnavailableError(IndicatorsProviderError):
    """No browser could be started, so nothing was learned about the source.

    A caller that remembers which sources turned its navigations away must not
    remember this one: the navigation never happened. The next series has the
    same reason to try as this one did.
    """


type BytesCacheKey = tuple[str, tuple[tuple[str, str], ...]]


class FetchContext:
    """Shared HTTP session, a per-run bytes cache, and a lazy headless browser.

    One context serves a whole refresh pass. The bytes cache lets bulk-file
    providers (FRB H.15, ECB FX) download one shared file once and reuse it
    across every series that maps to it. The browser is launched only when a
    WAF-gated provider first asks for it and is reused for the rest of the pass,
    so a pass pays at most one browser launch.

    ``purpose`` carries what the pass is for to the providers that need it (see
    :data:`FetchPurpose`).
    """

    def __init__(
        self,
        *,
        store_reader: StoreReader | None = None,
        purpose: FetchPurpose = "read",
    ) -> None:
        self.session = requests.Session()
        self.bytes_cache: dict[BytesCacheKey, bytes] = {}
        # Navigation URL -> why the browser was turned away there. The reason is
        # kept, not just the fact: a later series that skips the navigation is
        # the only one reporting, so the evidence has to travel with the record.
        self.blocked_browser_urls: dict[str, str] = {}
        self.store_reader = store_reader
        self.purpose = purpose
        self._browser: BrowserFetcher | None = None

    def discard_cached_bytes(self) -> None:
        """Drop every cached response before a retry.

        A source can answer HTTP 200 with a block page (FRB's edge does this for
        datacenter IPs), which caches as if it were data. Keeping it would make the
        retry re-read the same bad bytes and would fail every later series that
        shares the URL, so a failed fetch invalidates the cache instead.

        The blocked-browser record survives: a browser navigation costs a minute
        to fail, and an edge that turned one away turns the next one away too.
        What a retry is worth re-testing is the plain request, not that.
        """

        self.bytes_cache.clear()

    def browser_fetcher(self) -> BrowserFetcher:
        # Lazy import breaks the base <-> browser module cycle and keeps
        # Playwright off the import path until a browser-backed series runs.
        if self._browser is None:
            from .browser import BrowserFetcher

            self._browser = BrowserFetcher()
        return self._browser

    def close(self) -> None:
        # Releasing resources must never mask the pass's own result, so a failure
        # to close is swallowed here rather than propagating out of the `with`.
        with suppress(OSError):
            self.session.close()
        if self._browser is not None:
            with suppress(OSError):
                self._browser.close()

    def __enter__(self) -> FetchContext:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class HttpSession(Protocol):
    """Structural seam for HTTP fetching so tests can inject fake sessions."""

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> requests.Response: ...


class MacroDataProvider(Protocol):
    """A macro data source. Each provider isolates its own auth/parse/quirks."""

    name: str
    spec: ProviderSpec

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
    headers: Mapping[str, str] | None = None,
    context: FetchContext | None = None,
) -> str:
    content = fetch_bytes(
        session, url, params=params, max_bytes=max_bytes, headers=headers, context=context
    )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # A decode failure belongs to the response, not to the program. Left as a
        # UnicodeDecodeError it escapes the refresh's retry, which only knows
        # about provider errors, and the bytes that caused it stay in the shared
        # cache for every later series to decode and fail on again.
        raise IndicatorsProviderError(f"response from {url} is not UTF-8: {exc}") from exc


def _bytes_cache_key(url: str, params: Mapping[str, str] | None) -> BytesCacheKey:
    return (url, tuple(sorted((params or {}).items())))


def store_fetched_bytes(
    context: FetchContext,
    url: str,
    params: Mapping[str, str] | None,
    content: bytes,
    *,
    max_bytes: int,
) -> None:
    """Share bytes obtained outside :func:`fetch_bytes` with the rest of the pass.

    For a provider that reaches the same URL by another route — a browser
    navigation after the plain client is turned away — so every series mapped to
    that URL reads the one copy. The guards are the ones the plain path applies,
    because a cache entry is read without them: whoever fills it owes the same
    checks, or an empty or oversized body enters through the side door and every
    later series inherits it.
    """

    _guard_response(content, url=url, max_bytes=max_bytes)
    context.bytes_cache[_bytes_cache_key(url, params)] = content


def _guard_response(content: bytes, *, url: str, max_bytes: int, detail: str = "") -> None:
    if not content:
        raise SourceWithheldError(f"empty response body from {url}{detail}")
    if len(content) > max_bytes:
        raise IndicatorsProviderError(f"indicator response too large: {len(content)} bytes")


def fetch_bytes(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    context: FetchContext | None = None,
) -> bytes:
    key = _bytes_cache_key(url, params)
    if context is not None and key in context.bytes_cache:
        return context.bytes_cache[key]
    try:
        response = session.get(
            url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS, stream=True
        )
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
        # A source can answer 2xx with no body at all: federalreserve.gov's edge
        # does this to datacenter IPs instead of serving the CSV. No parser has a
        # reading for zero bytes, so failing here keeps a blocked fetch from being
        # reported as a format change, keeps the empty body out of the cache every
        # series sharing the URL reads, and makes the fetch retryable. The status
        # and content type name what the source actually answered with.
        _guard_response(
            content,
            url=url,
            max_bytes=max_bytes,
            detail=(
                f" (status {response.status_code}, "
                f"content-type {response.headers.get('Content-Type')!r})"
            ),
        )
    except requests.RequestException as exc:
        raise IndicatorsProviderError(f"failed to fetch {url}: {exc}") from exc
    if context is not None:
        context.bytes_cache[key] = content
    return content


def _raise_for_response(response: requests.Response, url: str) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        if response.status_code == HTTPStatus.FORBIDDEN:
            # An edge bot-mitigation says "not you" this way as readily as it
            # says it with an empty body, so a provider that can try another
            # route gets to recognise both as the same situation.
            raise SourceWithheldError(f"source withheld {url}: {exc}") from exc
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
