"""JPX public-source acquisition and snapshot persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

from baibai_engine.market.jpx_decode import (
    _EarningsCalendarFile,
    build_earnings_calendar_snapshot,
    build_regulation_snapshot,
    parse_earnings_calendar_excel,
    parse_regulation_rows,
    resolve_earnings_calendar_urls,
    validate_allowed_url,
    validate_earnings_calendar_url,
)
from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)

JPX_EARNINGS_CALENDAR_INDEX_URL = (
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
)
_HTTP_TIMEOUT_SECONDS = 30


class JPXProvider:
    """Acquire JPX public snapshots and persist their decoded observations."""

    def __init__(
        self,
        regulation_urls: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
        allow_stale_snapshot: bool = False,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._regulation_urls = dict(regulation_urls or {})
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only
        self._allow_stale_snapshot = allow_stale_snapshot

    def _http_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.reader import read_jpx_regulations

            cached = read_jpx_regulations(self._sqlite_path, asof_date)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jpx_regulation_flags", asof_date.isoformat())
        if not self._regulation_urls:
            raise JPXProviderError(
                "JPX regulation data is required but no public HTML URL is configured"
            )

        parsed_sources = {
            source_name: self._download_rows(source_name, url)
            for source_name, url in self._regulation_urls.items()
        }
        snapshot = build_regulation_snapshot(parsed_sources)
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.jpx import store_jpx_regulations

            store_jpx_regulations(
                self._sqlite_path,
                asof_date,
                flags_by_ticker=snapshot.flags_by_ticker,
                source_names=snapshot.source_names,
            )
        return snapshot

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.reader import (
                read_jpx_earnings_calendar_snapshot,
            )

            cached = read_jpx_earnings_calendar_snapshot(
                self._sqlite_path,
                asof_date,
                allow_stale=self._allow_stale_snapshot,
            )
            if cached is not None:
                return cached
        self._raise_if_cache_only("jpx_earnings_calendar", asof_date.isoformat())
        source_urls = self._resolve_earnings_calendar_urls()
        files = [self._download_earnings_calendar(url) for url in source_urls]
        snapshot = build_earnings_calendar_snapshot(
            files, asof_date=asof_date, source_urls=source_urls
        )
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.jpx import (
                store_jpx_earnings_calendar_snapshot,
            )

            store_jpx_earnings_calendar_snapshot(self._sqlite_path, snapshot)
        return snapshot

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise JPXProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: bootstrap or repair "
            "SQLite before running screening."
        )

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        earnings = self.get_earnings_calendar_snapshot(asof_date)
        snapshot = self.get_regulation_snapshot(asof_date)
        counts = {
            "earnings_calendar_rows": earnings.valid_record_count,
            "regulated_tickers": len(snapshot.flags_by_ticker),
        }
        # A cached read did no merge, so printing a zero here would read as
        # "nothing was superseded today" when nothing was measured.
        if earnings.superseded_record_count is not None:
            counts["earnings_calendar_superseded"] = earnings.superseded_record_count
        return counts

    def _resolve_earnings_calendar_urls(self) -> tuple[str, ...]:
        index_url = JPX_EARNINGS_CALENDAR_INDEX_URL
        validate_allowed_url(index_url)
        try:
            response = self._http_session().get(index_url, timeout=_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar index: {index_url}"
            ) from exc
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar index: {index_url} "
                f"(status={response.status_code})"
            )
        return resolve_earnings_calendar_urls(
            response.content, index_url, response.headers.get("content-type", "")
        )

    def _download_earnings_calendar(self, url: str) -> _EarningsCalendarFile:
        validate_earnings_calendar_url(url)
        try:
            response = self._http_session().get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise JPXProviderError(f"failed to download JPX earnings calendar: {url}") from exc
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar: {url} (status={response.status_code})"
            )
        return parse_earnings_calendar_excel(response.content, url)

    def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
        validate_allowed_url(url)
        response = self._http_session().get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX regulation source: {url} (status={response.status_code})"
            )
        suffix = Path(urlparse(url).path).suffix.lower()
        content_type_header = response.headers.get("content-type", "")
        content_type = content_type_header.split(";", 1)[0].strip().lower()
        if suffix == ".html" or content_type == "text/html":
            return parse_regulation_rows(source_name, response.content, url, content_type_header)
        raise JPXProviderError(f"unsupported JPX regulation source format: {url}")
