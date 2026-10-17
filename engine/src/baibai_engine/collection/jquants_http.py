"""Receive J-Quants JSON pages before SDK/DataFrame coercion or L2 selection."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal, cast

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry

JQuantsSource = Literal[
    "master",
    "daily_bars",
    "financial_summaries",
    "market_calendar",
    "margin_interest",
    "margin_alerts",
    "short_sale_reports",
]
_API_BASE = "https://api.jquants.com/v2"
_ENDPOINTS: dict[JQuantsSource, tuple[str, frozenset[str]]] = {
    "master": ("/equities/master", frozenset({"code", "date"})),
    "daily_bars": ("/equities/bars/daily", frozenset({"code", "date", "from", "to"})),
    "financial_summaries": ("/fins/summary", frozenset({"code", "date"})),
    "market_calendar": ("/markets/calendar", frozenset({"hol_div", "from", "to"})),
    "margin_interest": ("/markets/margin-interest", frozenset({"code", "date", "from", "to"})),
    "margin_alerts": ("/markets/margin-alert", frozenset({"code", "date", "from", "to"})),
    "short_sale_reports": (
        "/markets/short-sale-report",
        frozenset({"code", "disc_date", "disc_date_from", "disc_date_to", "calc_date"}),
    ),
}
_BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class JQuantsAcquisitionError(RuntimeError):
    """The source request or its JSON envelope could not be completed."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class _ReadRetry(Retry):
    """Retry one GET, never replay an already received pagination sequence."""

    def get_backoff_time(self) -> float:
        if not self.history:
            return 0.0
        return float(_BACKOFF_SECONDS[min(len(self.history), 5) - 1])

    def sleep(self, response: BaseHTTPResponse | None = None) -> None:
        delay = self.get_backoff_time()
        if response is not None and self.respect_retry_after_header:
            try:
                retry_after = self.get_retry_after(response)
            except InvalidHeader:
                retry_after = None
            if retry_after is not None:
                delay = retry_after
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True, slots=True)
class JQuantsPage:
    """Decompressed HTTP response content, with no credentials or response headers.

    The query identifies this page, including its provider pagination token. The
    body is retained before JSON decoding; it is not an SDK-converted record set.
    """

    source: JQuantsSource
    query: Mapping[str, str]
    received_at: datetime
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", MappingProxyType(dict(self.query)))
        if self.received_at.utcoffset() is None:
            raise ValueError("receipt time must be timezone-aware")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


def _envelope(page: JQuantsPage) -> dict[str, Any]:
    try:
        payload = json.loads(page.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JQuantsAcquisitionError("J-Quants response is not JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise JQuantsAcquisitionError("J-Quants response lacks a data array")
    if any(not isinstance(row, dict) for row in payload["data"]):
        raise JQuantsAcquisitionError("J-Quants data contains a non-record row")
    return payload


def page_records(page: JQuantsPage) -> list[dict[str, Any]]:
    """Read all source fields without normalizing dates, numbers or missing values."""
    return cast(list[dict[str, Any]], _envelope(page)["data"])


class JQuantsHTTP:
    """Explicit source-only HTTP receiver, independent of storage and calculation."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not api_key:
            raise JQuantsAcquisitionError("JQUANTS_API_KEY is required for acquisition")
        self._api_key = api_key
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        if self._owns_session:
            self._session.mount(
                "https://",
                HTTPAdapter(
                    max_retries=_ReadRetry(
                        total=5,
                        allowed_methods=frozenset({"GET"}),
                        status_forcelist=(429, 500, 502, 503, 504),
                    )
                ),
            )

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def pages(self, source: JQuantsSource, params: Mapping[str, str]) -> Iterator[JQuantsPage]:
        """Yield each body before interpreting it, so callers can retain failed parses.

        A consumer must exhaust the iterator before marking the request complete.
        Receiving a page alone never establishes full range or universe coverage.
        """
        if source not in _ENDPOINTS:
            raise ValueError("unsupported J-Quants source")
        path, allowed = _ENDPOINTS[source]
        if not set(params) <= allowed or any(not isinstance(v, str) for v in params.values()):
            raise ValueError("unsupported J-Quants query parameters")
        base_query = dict(params)
        query = dict(base_query)
        seen: set[str] = set()
        while True:
            failure_kind: str | None = None
            page: JQuantsPage | None = None
            try:
                with self._session.get(
                    _API_BASE + path,
                    params=query,
                    headers={"x-api-key": self._api_key},
                    timeout=30,
                    allow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise JQuantsAcquisitionError(
                            f"J-Quants HTTP status {response.status_code}"
                        )
                    page = JQuantsPage(source, query, self._clock(), response.content)
            except requests.RequestException as exc:
                # Exception text may contain request headers or other credential data.
                failure_kind = type(exc).__name__
            if failure_kind is not None:
                raise JQuantsAcquisitionError(f"J-Quants request failed: {failure_kind}")
            assert page is not None
            yield page
            payload = _envelope(page)
            cursor = payload.get("pagination_key")
            if cursor is None or cursor == "":
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise JQuantsAcquisitionError("invalid or repeated J-Quants pagination key")
            seen.add(cursor)
            query = {**base_query, "pagination_key": cursor}
