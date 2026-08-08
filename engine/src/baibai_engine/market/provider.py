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
from collections.abc import Callable, Mapping, Sequence
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
    count_daily_bars,
    daily_bars_covered,
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

    def _ensure_eq_bars_daily_range(self, start: date, end: date) -> None:
        """Bring the SQLite cache to where it can serve `[start, end]`.

        The whole fetch decision lives here so the row-returning read and the
        count-only ensure below cannot drift apart. Coverage is asked through
        `daily_bars_covered`, which reads the stored dates rather than building a
        model per row: the answer is one boolean and the window is millions of rows.
        """
        if self._sqlite_path is None:
            return
        if daily_bars_covered(self._sqlite_path, start, end):
            latest = latest_daily_bar_date(self._sqlite_path, start, end)
            if self._cache_only or latest is None or latest >= end:
                return
            stored_latest = latest_stored_daily_bar_date(self._sqlite_path)
            if stored_latest is not None and end < stored_latest:
                # The window ends before days the store already holds, so the
                # edge gap is a closed market rather than a tail the provider
                # has since published. Reading it live would delete and rewrite
                # a historical cross-section on every pass over a past window.
                return
            # The coverage check tolerates a holiday-sized edge gap, so an
            # incremental asof can look covered while its own bar is not yet
            # stored. On a fetch-capable run, refresh the tail from the last
            # stored day so newly published trading days are picked up; a
            # cache-only `screening run` trusts the validated coverage instead.
            self._load_or_fetch_range("get_eq_bars_daily_range", latest, end)
            return
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
        self._fetch_missing_range_chunks("get_eq_bars_daily_range", start, end)

    def ensure_eq_bars_daily_range(self, start: date, end: date) -> int:
        """Cover `[start, end]` and answer how many usable rows the store holds for it.

        Bootstrap and backfill want the window filled and a number to report; they
        never look at a bar. Handing them the models instead costs ~13s per call on
        the production window and is discarded a line later.

        The coverage check after the fetch is what makes the answer mean "the window
        is filled" rather than "some rows exist". A provider that returns part of
        what was asked for leaves a count that looks healthy and a window that is
        not, and the run would then fail somewhere downstream, against a different
        window, instead of here.
        """
        if self._sqlite_path is None:
            self._raise_if_cache_only(
                "jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}"
            )
            return len(self._load_or_fetch_range("get_eq_bars_daily_range", start, end))
        self._ensure_eq_bars_daily_range(start, end)
        if not daily_bars_covered(self._sqlite_path, start, end):
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_daily_bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        return count_daily_bars(self._sqlite_path, start, end)

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        if self._sqlite_path is not None:
            self._ensure_eq_bars_daily_range(start, end)
            cached = read_daily_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_daily_bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
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

    def _missing_subranges(
        self, method: str, start: date, end: date
    ) -> tuple[tuple[date, date], ...]:
        """Which parts of the request a fetch still has to cover.

        The base answer is the whole request, because bars decide chunk by chunk
        from the stored rows and narrowing here would only repeat that test.
        Sources whose completeness lives in `source_coverage` override this: their
        request window is anchored on the as-of and slides a day at a time, so the
        chunk grid slides with it and the last chunk re-fetches up to a month to
        add a single day.
        """
        return ((start, end),)

    def _fetch_ranges_paced(
        self,
        method: str,
        ranges: Sequence[tuple[date, date]],
        *,
        skip_cached_chunks: bool,
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> None:
        """Fetch every range in chunks, with one pace between consecutive requests.

        Ranges are traversed as one sequence so the pacing holds across a boundary
        too: two ranges either side of a gap are still two requests to the same
        rate-limited API. The wait is taken before the next request rather than
        after the previous one, so a pass that ends on a fetch does not sleep on
        its way out.
        """
        chunk_days = self._RANGE_CHUNK_DAYS.get(method)
        requests: list[tuple[date, date]] = []
        for subrange_start, subrange_end in ranges:
            if chunk_days is None:
                requests.append((subrange_start, subrange_end))
                continue
            cursor = subrange_start
            while cursor <= subrange_end:
                chunk_end = min(cursor + timedelta(days=chunk_days - 1), subrange_end)
                if not skip_cached_chunks or not self._range_chunk_is_cached(
                    method, cursor, chunk_end
                ):
                    requests.append((cursor, chunk_end))
                cursor = chunk_end + timedelta(days=1)
        total = len(requests)
        for index, (chunk_start, chunk_end) in enumerate(requests, start=1):
            if index > 1:
                time.sleep(self._INTER_CHUNK_SLEEP_SECONDS)
            self._load_or_fetch(method, start_dt=chunk_start, end_dt=chunk_end)
            if progress is not None:
                progress(index, total, chunk_start, chunk_end)

    def _fetch_missing_range_chunks(self, method: str, start: date, end: date) -> None:
        if self._sqlite_path is None:
            return
        if self._RANGE_CHUNK_DAYS.get(method) is None:
            self._load_or_fetch(method, start_dt=start, end_dt=end)
            return
        self._fetch_ranges_paced(
            method,
            self._missing_subranges(method, start, end),
            skip_cached_chunks=True,
        )

    def _range_chunk_is_cached(self, method: str, start: date, end: date) -> bool:
        """Skip a chunk the store already holds, without materialising it.

        The truth source stays what it was per method: bars are covered when the
        stored dates say so, never when `source_coverage` says so. A store whose
        bookkeeping has holes but whose rows are complete must not be re-fetched.
        """
        if self._sqlite_path is None:
            return False
        if method == "get_eq_bars_daily_range":
            return daily_bars_covered(self._sqlite_path, start, end)
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
        for attempt in range(1, len(self._RATE_LIMIT_BACKOFF_SECONDS) + 2):
            attempts = attempt
            try:
                return call(**_stringify_dates(params))
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_jquants_error(exc) or attempt > len(
                    self._RATE_LIMIT_BACKOFF_SECONDS
                ):
                    break
                time.sleep(self._RATE_LIMIT_BACKOFF_SECONDS[attempt - 1])
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
