"""Cache-first J-Quants adapter for price/calendar market data.

`JQuantsMarketProvider` owns the shared fetch engine (retry/backoff, chunked
range fetch, SQLite read-through and store dispatch) plus the daily-bar and
market-calendar endpoints. Screening's `JQuantsProvider` extends it with the
fundamentals endpoints (master / fin summary), so both
paths share one J-Quants client contract while market never depends on
screening.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

from baibai_engine.market.bars import JQuantsDailyBar, JQuantsMarketCalendarDay
from baibai_engine.market.config import JQUANTS_CLIENT_V2_METHODS
from baibai_engine.market.jquants import (
    JQuantsProviderError as JQuantsProviderError,
)
from baibai_engine.market.jquants import (
    _is_retryable_jquants_error,
    _parse_yyyymmdd_param,
    _payload_to_records,
    _stringify_dates,
    normalize_daily_bar,
    normalize_market_calendar,
)
from baibai_engine.market.sqlite import (
    store_jquants_daily_bars,
    store_jquants_market_calendar,
)
from baibai_engine.market.store import (
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_daily_bars,
    read_market_calendar,
)


class JQuantsMarketProvider:
    """Thin cache-first adapter for the J-Quants ClientV2 price/calendar endpoints."""

    _RANGE_CHUNK_DAYS: ClassVar[Mapping[str, int]] = {
        # ClientV2 range helpers fan out to per-day API calls internally.
        # Long windows can hit J-Quants 429s, so persist smaller chunks to make
        # retries resumable and keep the behavior understandable from static code.
        # Both methods use 31 days to equalize per-chunk burst and make recovery
        # from 429 predictable across method transitions.
        "get_eq_bars_daily_range": 31,
        "get_fin_summary_range": 31,
    }
    # Backoff to absorb J-Quants Light 429s. Observed in practice that the
    # rate window can be several minutes, so the tail of the tuple keeps
    # climbing past 5 minutes. Total worst-case wait per chunk ~= 17.5 min.
    _RATE_LIMIT_BACKOFF_SECONDS = (30, 60, 120, 300, 600)
    _INTER_CHUNK_SLEEP_SECONDS = 3.0

    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        client: Any | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
    ) -> None:
        self._api_key = api_key
        self._cache_dir = Path(cache_dir) / "jquants"
        self._client = client
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        if self._sqlite_path is not None:
            cached = read_daily_bars(self._sqlite_path, start, end)
            if cached is not None:
                latest = latest_daily_bar_date(self._sqlite_path, start, end)
                if self._cache_only or latest is None or latest >= end:
                    return cached
                stored_latest = latest_stored_daily_bar_date(self._sqlite_path)
                if stored_latest is not None and end < stored_latest:
                    # The window ends before days the store already holds, so the
                    # edge gap is a closed market rather than a tail the provider
                    # has since published. Reading it live would delete and rewrite
                    # a historical cross-section on every pass over a past window.
                    return cached
                # `read_daily_bars` tolerates a holiday-sized edge gap, so an
                # incremental asof can look covered while its own bar is not yet
                # stored. On a fetch-capable run, refresh the tail from the last
                # stored day so newly published trading days are picked up; a
                # cache-only `screening run` trusts the validated coverage instead.
                self._load_or_fetch_range("get_eq_bars_daily_range", latest, end)
                refreshed = read_daily_bars(self._sqlite_path, start, end)
                return refreshed if refreshed is not None else cached
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_eq_bars_daily_range", start, end)
            cached = read_daily_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_daily_bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_eq_bars_daily_range", start, end)
        return [bar for record in records if (bar := normalize_daily_bar(record)) is not None]

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        if self._sqlite_path is not None:
            cached = read_market_calendar(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_market_calendar", f"{start.isoformat()}..{end.isoformat()}"
        )
        # holiday_division フィルタは掛けない。"1"=営業日だけでなく "2"=半日営業 (大納会など)
        # も取引あり扱いすべきで、事前フィルタで "2" を落とすと正しい営業日で asof が
        # reject される。normalize 側で "1"/"2" を営業扱いにする。
        records = self._load_or_fetch(
            "get_mkt_calendar",
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
        return [normalize_market_calendar(record) for record in records]

    def _load_or_fetch_range(self, method: str, start: date, end: date) -> list[dict[str, Any]]:
        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        if chunk_days is None or (end - start).days < chunk_days:
            return self._load_or_fetch(method, start_dt=start, end_dt=end)

        records: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            records.extend(self._load_or_fetch(method, start_dt=cursor, end_dt=chunk_end))
            cursor = chunk_end + timedelta(days=1)
            # Pace provider fetches to avoid tripping J-Quants rate limits.
            if cursor <= end:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)
        return records

    def _fetch_missing_range_chunks(self, method: str, start: date, end: date) -> None:
        if self._sqlite_path is None:
            return
        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        if chunk_days is None:
            self._load_or_fetch(method, start_dt=start, end_dt=end)
            return

        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            did_fetch = False
            if not self._range_chunk_is_cached(method, cursor, chunk_end):
                self._load_or_fetch(method, start_dt=cursor, end_dt=chunk_end)
                did_fetch = True
            cursor = chunk_end + timedelta(days=1)
            if did_fetch and cursor <= end:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)

    def _range_chunk_is_cached(self, method: str, start: date, end: date) -> bool:
        if self._sqlite_path is None:
            return False
        if method == "get_eq_bars_daily_range":
            return read_daily_bars(self._sqlite_path, start, end) is not None
        return False

    def _load_or_fetch(
        self,
        method: str,
        *,
        store_params: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        if method not in JQUANTS_CLIENT_V2_METHODS:
            raise JQuantsProviderError(f"unsupported ClientV2 method: {method}")

        client = self._get_client()
        call = getattr(client, method, None)
        if call is None:
            raise JQuantsProviderError(f"ClientV2 missing method: {method}")
        payload = self._call_with_retry(method, call, **params)

        records = _payload_to_records(payload)
        self._store_records(method, records, store_params or params)
        return records

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise JQuantsProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: bootstrap or repair "
            "SQLite before running screening."
        )

    def _store_records(
        self,
        method: str,
        records: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> None:
        if self._sqlite_path is None:
            return
        if method == "get_eq_bars_daily_range":
            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                return
            store_jquants_daily_bars(
                self._sqlite_path,
                records,
                requested_start=start,
                requested_end=end,
            )
            return
        if method == "get_mkt_calendar":
            start = _parse_yyyymmdd_param(params.get("from_yyyymmdd"))
            end = _parse_yyyymmdd_param(params.get("to_yyyymmdd"))
            if start is not None and end is not None:
                store_jquants_market_calendar(
                    self._sqlite_path,
                    records,
                    requested_start=start,
                    requested_end=end,
                )

    def _call_with_retry(self, method: str, call: Any, **params: Any) -> Any:
        last_exc: Exception | None = None
        attempts = 0
        for attempt, delay_seconds in enumerate((0, *self._RATE_LIMIT_BACKOFF_SECONDS), start=1):
            attempts = attempt
            try:
                return call(**_stringify_dates(params))
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_jquants_error(exc):
                    break
                if delay_seconds:
                    time.sleep(delay_seconds)
        # api_key / id_token 等の secret が exception 文字列に含まれる可能性に備えて
        # sanitize、さらに `from None` で原因チェーンを切って traceback 漏洩も遮断する。
        sanitized = self._sanitize_secret(str(last_exc)) if last_exc else ""
        exception_name = type(last_exc).__name__ if last_exc else "unknown"
        raise JQuantsProviderError(
            f"failed to fetch J-Quants payload via {method} after {attempts} attempts: "
            f"{exception_name}: {sanitized}"
        ) from None

    def _sanitize_secret(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "<redacted>")
        return text

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import jquantsapi
        except ModuleNotFoundError as exc:
            raise JQuantsProviderError("jquantsapi is not installed") from exc
        self._client = jquantsapi.ClientV2(api_key=self._api_key)
        return self._client
