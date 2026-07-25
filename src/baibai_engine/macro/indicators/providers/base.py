from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal, Protocol

import requests

from ..db import ObservationRecord
from ..definitions import SeriesDefinition

if TYPE_CHECKING:
    from .browser import BrowserFetcher

HTTP_TIMEOUT_SECONDS = 30
MAX_CSV_RESPONSE_BYTES = 8_000_000
MAX_ZIP_RESPONSE_BYTES = 16_000_000

# Reads a stored series' observations (latest ok vintage per observed_at) for a
# derived provider that computes from other series. Bound to the live connection
# by the service so a derived fetch sees inputs already committed this run.
type StoreReader = Callable[[str, date, date], tuple[ObservationRecord, ...]]


class IndicatorsProviderError(RuntimeError):
    """Raised when an indicator provider cannot return requested observations."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSpec:
    """A provider's fetch/store capabilities, declared next to its parser.

    The service reads behavior from this spec instead of branching on the
    provider name, so adding a provider means writing one module (parser + spec)
    and registering it once — no edits to the service or store layers.
    """

    name: str
    # ``http`` fetches from an external source; ``local`` computes from other
    # stored series (derived). The batch refreshes every ``http`` series before
    # any ``local`` one so a derived series reads fresh inputs.
    kind: Literal["http", "local"] = "http"
    # All-history refresh floor. ``all_history_start`` is the reproducible fixed
    # start a bulk source exposes; ``all_history_rolling_years`` derives the floor
    # from today instead (a licensed rolling window such as J-Quants Light's 5
    # years).
    all_history_start: date | None = None
    all_history_rolling_years: int | None = None
    # Store-rewrite policy for an all-history refresh. ``trim_before_first`` drops
    # observations older than the first the provider returns (FRED's licensed
    # window defines its reproducible start). ``replace_requested_range`` deletes
    # the requested window before insert so a re-published vintage supersedes
    # earlier rows (J-Quants weekly flows).
    trim_before_first: bool = False
    replace_requested_range: bool = False
    # Reads clamp to observations whose vintage is on/before the read cutoff, so a
    # publish-lagged series stays point-in-time correct (J-Quants weekly flows).
    point_in_time_vintage: bool = False
    # Credential env var names required to fetch (diagnostics only; values are
    # read from the environment by the provider, never stored in the registry).
    required_env: tuple[str, ...] = field(default=())


class FetchContext:
    """Shared HTTP session, a per-run bytes cache, and a lazy headless browser.

    One context serves a whole refresh pass. The bytes cache lets bulk-file
    providers (FRB H.15, ECB FX) download one shared file once and reuse it
    across every series that maps to it. The browser is launched only when a
    WAF-gated provider first asks for it and is reused for the rest of the pass,
    so a pass pays at most one browser launch.

    ``refetch_stored`` tells a provider whose fetch cost scales with the number
    of observations (spglobal_pmi downloads one PDF per month) whether it must
    re-read observations the store already holds. An all-history refresh sets it
    so the store is rebuilt from source; an incremental refresh leaves it off so
    only new or missing observations are fetched.
    """

    def __init__(
        self, *, store_reader: StoreReader | None = None, refetch_stored: bool = False
    ) -> None:
        self.session = requests.Session()
        self.bytes_cache: dict[tuple[str, tuple[tuple[str, str], ...]], bytes] = {}
        self.store_reader = store_reader
        self.refetch_stored = refetch_stored
        self._browser: BrowserFetcher | None = None

    def browser_fetcher(self) -> BrowserFetcher:
        # Lazy import breaks the base <-> browser module cycle and keeps
        # Playwright off the import path until a browser-backed series runs.
        if self._browser is None:
            from .browser import BrowserFetcher

            self._browser = BrowserFetcher()
        return self._browser

    def close(self) -> None:
        self.session.close()
        if self._browser is not None:
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
    return content.decode("utf-8-sig")


def fetch_bytes(
    session: HttpSession,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    context: FetchContext | None = None,
) -> bytes:
    key = (url, tuple(sorted((params or {}).items())))
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
