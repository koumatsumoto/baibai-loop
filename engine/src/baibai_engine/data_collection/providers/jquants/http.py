"""Receive J-Quants JSON pages before SDK/DataFrame coercion or L2 selection."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime

import requests
from requests.adapters import HTTPAdapter

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsRateLimitError,
    JQuantsSource,
    page_envelope,
)
from baibai_engine.data_collection.transport.http import ReadRetry, check_acquisition_deadline

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
                    max_retries=ReadRetry(
                        total=5,
                        allowed_methods=frozenset({"GET"}),
                        status_forcelist=(429, 500, 502, 503, 504),
                        # Keep the final status after this GET's retry budget,
                        # rather than losing shared quota exhaustion in RetryError.
                        raise_on_status=False,
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
            check_acquisition_deadline()
            failure_kind: str | None = None
            page: JQuantsPage | None = None
            try:
                with self._session.get(
                    _API_BASE + path,
                    params=query,
                    headers={"x-api-key": self._api_key},
                    timeout=30,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    # Inspect status before reading an error body: a body timeout
                    # must not hide the final 429 from the acquisition caller.
                    if response.status_code == 429:
                        raise JQuantsRateLimitError("J-Quants rate-limited (HTTP 429)")
                    if response.status_code != 200:
                        raise JQuantsAcquisitionError(
                            f"J-Quants HTTP status {response.status_code}"
                        )
                    body = response.content
                    page = JQuantsPage(source, query, self._clock(), body)
            except requests.RequestException as exc:
                # Exception text may contain request headers or other credential data.
                failure_kind = type(exc).__name__
            if failure_kind is not None:
                raise JQuantsAcquisitionError(f"J-Quants request failed: {failure_kind}")
            assert page is not None
            yield page
            payload = page_envelope(page)
            cursor = payload.get("pagination_key")
            if cursor is None or cursor == "":
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise JQuantsAcquisitionError("invalid or repeated J-Quants pagination key")
            seen.add(cursor)
            query = {**base_query, "pagination_key": cursor}
