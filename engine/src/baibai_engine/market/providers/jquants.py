"""J-Quantsのprice/calendar・財務・信用入力を取得する単一adapter。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent as JQuantsAdjustmentFactorEvent,
)
from baibai_engine.market.bars import (
    JQuantsDailyBar,
    JQuantsMarketCalendarDay,
)
from baibai_engine.market.config import JQUANTS_CLIENT_V2_METHODS
from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.master_snapshot import validate_master_snapshot
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.providers.jquants_decode import (
    JQuantsProviderError as JQuantsProviderError,
)
from baibai_engine.market.providers.jquants_decode import (
    _is_retryable_jquants_error,
    _parse_yyyymmdd_param,
    _payload_to_records,
    _stringify_dates,
    coalesce_field,
    first_period_float,
    first_value,
    has_period_slot,
    normalize_daily_bar,
    normalize_market_calendar,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    to_float,
    to_period,
)
from baibai_engine.market.providers.jquants_decode import (
    normalize_adjustment_factor_event as normalize_adjustment_factor_event,
)
from baibai_engine.market.providers.jquants_decode import (
    parse_jquants_code as parse_jquants_code,
)
from baibai_engine.market.sqlite import store_jquants_daily_bars, store_jquants_market_calendar
from baibai_engine.market.store import (
    count_daily_bars,
    daily_bars_covered,
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_daily_bars,
    read_market_calendar,
)


class _JQuantsTransport:
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


class JQuantsProvider(_JQuantsTransport):
    """Cache-first adapter for jquantsapi.ClientV2: price/calendar + fundamentals."""

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        if self._sqlite_path is not None:
            # Imported lazily to avoid a circular import: sqlite_reader pulls in
            # this module's schema dataclasses to materialise rows.
            from baibai_engine.market.sqlite.reader import read_eq_master_exact

            cached = read_eq_master_exact(self._sqlite_path, requested_asof)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_master_snapshots", requested_asof.isoformat())
        records = self._load_or_fetch(
            "get_eq_master",
            store_params={"requested_asof": requested_asof},
            date=requested_asof.isoformat(),
        )
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_eq_master_exact

            stored = read_eq_master_exact(self._sqlite_path, requested_asof)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_master_snapshots "
                    f"for {requested_asof.isoformat()}"
                )
            return stored
        validated = validate_master_snapshot(records, requested_asof)
        return [
            SecurityMaster(
                ticker=ticker,
                name=name,
                market_segment=market,
                sector_33=sector,
                is_common_stock=bool(is_common),
            )
            for _, ticker, name, market, sector, is_common in validated.rows
        ]

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        """Fetch every ticker's margin balance for one weekly balance date.

        The endpoint answers per balance date, and not every week has one: the
        exchange skips weeks it does not publish. The coverage row records an
        empty attempt either way; the bootstrap planner uses its fetch time and
        the publication calendar to distinguish a premature empty response from
        a final skipped week.
        """
        from baibai_engine.market.margin_publication import require_legacy_weekly_balance_date

        require_legacy_weekly_balance_date(week_end)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_weekly_margin

            cached = read_weekly_margin(self._sqlite_path, week_end)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_weekly_margin", week_end.isoformat())
        records = self._load_or_fetch(
            "get_mkt_margin_interest",
            store_params={"week_end": week_end},
            date_yyyymmdd=week_end.strftime("%Y%m%d"),
        )
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_weekly_margin

            stored = read_weekly_margin(self._sqlite_path, week_end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_weekly_margin "
                    f"for {week_end.isoformat()}"
                )
            return stored
        return [
            margin
            for record in records
            if (margin := normalize_weekly_margin(record, week_end)) is not None
        ]

    def refresh_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        """Re-read one legacy week even when an earlier empty response was cached."""
        from baibai_engine.market.margin_publication import require_legacy_weekly_balance_date

        require_legacy_weekly_balance_date(week_end)
        self._raise_if_cache_only("jquants_weekly_margin", week_end.isoformat())
        records = self._load_or_fetch(
            "get_mkt_margin_interest",
            store_params={"week_end": week_end},
            date_yyyymmdd=week_end.strftime("%Y%m%d"),
        )
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_weekly_margin

            stored = read_weekly_margin(self._sqlite_path, week_end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after refreshing jquants_weekly_margin "
                    f"for {week_end.isoformat()}"
                )
            return stored
        return [
            margin
            for record in records
            if (margin := normalize_weekly_margin(record, week_end)) is not None
        ]

    def get_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]:
        """Return the daily-publication designated-issue dataset for a date range."""
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_margin_alerts

            cached = read_margin_alerts(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        return self._fetch_mkt_margin_alert_range(start, end)

    def refresh_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]:
        """Re-read a bounded publication-date overlap for late rows or corrections."""
        return self._fetch_mkt_margin_alert_range(start, end)

    def _fetch_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]:
        self._raise_if_cache_only(
            "jquants_margin_alerts", f"{start.isoformat()}..{end.isoformat()}"
        )
        records = self._load_or_fetch_range("get_mkt_margin_alert_range", start, end)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_margin_alerts

            stored = read_margin_alerts(self._sqlite_path, start, end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_margin_alerts "
                    f"for {start.isoformat()}..{end.isoformat()}"
                )
            return stored
        return [
            alert
            for record in records
            if (alert := normalize_margin_alert(record)) is not None
            and start <= alert.publication_date <= end
        ]

    def get_mkt_all_issues_daily_margin(
        self, balance_date: date
    ) -> list[JQuantsAllIssuesDailyMargin]:
        """Fetch the all-issues daily replacement without touching weekly storage."""
        from baibai_engine.market.margin_publication import require_all_issues_daily_balance_date

        require_all_issues_daily_balance_date(balance_date)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_all_issues_daily_margin

            cached = read_all_issues_daily_margin(self._sqlite_path, balance_date)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_all_issues_daily_margin", balance_date.isoformat())
        records = self._load_or_fetch(
            "get_mkt_margin_interest",
            store_params={"all_issues_daily_balance_date": balance_date},
            date_yyyymmdd=balance_date.strftime("%Y%m%d"),
        )
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_all_issues_daily_margin

            stored = read_all_issues_daily_margin(self._sqlite_path, balance_date)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching "
                    "jquants_all_issues_daily_margin for "
                    f"{balance_date.isoformat()}"
                )
            return stored
        return [
            margin
            for record in records
            if (margin := normalize_all_issues_daily_margin(record)) is not None
            and margin.balance_date == balance_date
        ]

    def get_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]:
        """Return reports by disclosure date, preserving empty-range coverage."""
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_short_sale_reports

            cached = read_short_sale_reports(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_short_sale_reports", f"{start.isoformat()}..{end.isoformat()}"
        )
        missing: tuple[tuple[date, date], ...] = ((start, end),)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite import connect_current, missing_intervals

            conn = connect_current(self._sqlite_path)
            if conn is not None:
                try:
                    missing = missing_intervals(conn, "jquants_short_sale_reports", start, end)
                finally:
                    conn.close()
        records: list[dict[str, Any]] = []
        for missing_start, missing_end in missing:
            cursor = missing_start
            while cursor <= missing_end:
                records.extend(
                    self._load_or_fetch(
                        "get_mkt_short_sale_report",
                        store_params={"start_dt": cursor, "end_dt": cursor},
                        disclosed_date=cursor.isoformat(),
                    )
                )
                cursor += timedelta(days=1)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_short_sale_reports

            stored = read_short_sale_reports(self._sqlite_path, start, end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching "
                    "jquants_short_sale_reports for "
                    f"{start.isoformat()}..{end.isoformat()}"
                )
            return stored
        return [
            report
            for record in records
            if (report := normalize_short_sale_report(record)) is not None
        ]

    def refresh_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]:
        """Re-read the correction overlap and repair gaps after an unattended outage."""

        self._raise_if_cache_only(
            "jquants_short_sale_reports", f"{start.isoformat()}..{end.isoformat()}"
        )
        planned: tuple[tuple[date, date], ...] = ((start, end),)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite import (
                connect_current,
                merge_date_ranges,
                missing_intervals,
            )
            from baibai_engine.market.sqlite.ingest.jquants import SHORT_SALE_REPORT_SOURCE

            conn = connect_current(self._sqlite_path)
            if conn is not None:
                try:
                    row = conn.execute(
                        "SELECT MIN(coverage_start) FROM source_coverage "
                        "WHERE source = ? AND status = 'ok'",
                        (SHORT_SALE_REPORT_SOURCE,),
                    ).fetchone()
                    coverage_start = (
                        date.fromisoformat(str(row[0])) if row is not None and row[0] else None
                    )
                    repairs = (
                        missing_intervals(
                            conn,
                            "jquants_short_sale_reports",
                            coverage_start,
                            end,
                        )
                        if coverage_start is not None
                        else ()
                    )
                    planned = merge_date_ranges([*repairs, (start, end)])
                finally:
                    conn.close()
        records: list[dict[str, Any]] = []
        for range_start, range_end in planned:
            cursor = range_start
            while cursor <= range_end:
                records.extend(
                    self._load_or_fetch(
                        "get_mkt_short_sale_report",
                        store_params={"start_dt": cursor, "end_dt": cursor},
                        disclosed_date=cursor.isoformat(),
                    )
                )
                cursor += timedelta(days=1)
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_short_sale_reports

            stored = read_short_sale_reports(self._sqlite_path, start, end)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after refreshing "
                    "jquants_short_sale_reports for "
                    f"{start.isoformat()}..{end.isoformat()}"
                )
            return stored
        return [
            report
            for record in records
            if (report := normalize_short_sale_report(record)) is not None
            and start <= report.disclosed_at <= end
        ]

    def get_adjustment_factor_bars_range(
        self, start: date, end: date
    ) -> list[JQuantsAdjustmentFactorEvent]:
        """Return only split events while still proving the full bar range is covered."""
        if self._sqlite_path is not None:
            from baibai_engine.market.store import read_adjustment_factor_bars

            cached = read_adjustment_factor_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jquants_daily_bars", f"{start.isoformat()}..{end.isoformat()}")
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_eq_bars_daily_range", start, end)
            cached = read_adjustment_factor_bars(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching split-normalization bars "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_eq_bars_daily_range", start, end)
        return [
            event
            for record in records
            if (event := normalize_adjustment_factor_event(record)) is not None
        ]

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_fin_summaries

            cached = read_fin_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_fin_summaries", f"{start.isoformat()}..{end.isoformat()}"
        )
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_fin_summary_range", start, end)
            cached = read_fin_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_fin_summaries "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_fin_summary_range", start, end)
        return [
            summary
            for record in records
            if (summary := normalize_financial_summary(record)) is not None
        ]

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        """Return only FY rows while still proving the full summary range is covered."""
        if self._sqlite_path is not None:
            from baibai_engine.market.sqlite.reader import read_fy_summaries

            cached = read_fy_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_fin_summaries", f"{start.isoformat()}..{end.isoformat()}"
        )
        if self._sqlite_path is not None:
            self._fetch_missing_range_chunks("get_fin_summary_range", start, end)
            cached = read_fy_summaries(self._sqlite_path, start, end)
            if cached is not None:
                return cached
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching normalized-profit summaries "
                f"for {start.isoformat()}..{end.isoformat()}"
            )
        records = self._load_or_fetch_range("get_fin_summary_range", start, end)
        return [
            summary
            for record in records
            if (summary := normalize_financial_summary(record)) is not None
            and summary.fiscal_period == "FY"
        ]

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        repair_ranges: Sequence[tuple[date, date]] = (),
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int:
        """Bring the summaries store current for `[start, end]` and count what it holds.

        Two things are fetched: what the coverage says is missing, and the trailing
        ``revision_overlap_days`` regardless of coverage. The overlap is the only
        way a filing the provider published late — for a date already fetched — ever
        lands, and today that protection exists only as an accident of the sliding
        chunk grid, which re-reads between 1 and 31 days depending on the as-of.

        The two are merged before anything is requested, so a stop longer than the
        overlap costs the outage and not the outage plus a week. Asking once, here,
        is also what keeps the 730-day and 2,200-day windows of the same source from
        each paying for the same trailing week.
        """
        window = f"{start.isoformat()}..{end.isoformat()}"
        self._raise_if_cache_only("jquants_fin_summaries", window)
        if self._sqlite_path is None:
            return len(self._load_or_fetch_range("get_fin_summary_range", start, end))
        from baibai_engine.market.sqlite import merge_date_ranges
        from baibai_engine.market.sqlite.reader import count_fin_summaries, fin_summaries_covered

        overlap_start = max(start, end - timedelta(days=revision_overlap_days))
        planned = merge_date_ranges(
            [
                *self._missing_subranges("get_fin_summary_range", start, end),
                (overlap_start, end),
                *repair_ranges,
            ]
        )
        self._fetch_ranges_paced(
            "get_fin_summary_range",
            planned,
            skip_cached_chunks=False,
            progress=progress,
        )
        if not fin_summaries_covered(self._sqlite_path, start, end):
            raise JQuantsProviderError(
                "SQLite cache remained incomplete after fetching jquants_fin_summaries "
                f"for {window}"
            )
        return count_fin_summaries(self._sqlite_path, start, end)

    def _range_chunk_is_cached(self, method: str, start: date, end: date) -> bool:
        if self._sqlite_path is not None and method == "get_fin_summary_range":
            from baibai_engine.market.sqlite.reader import fin_summaries_covered

            return fin_summaries_covered(self._sqlite_path, start, end)
        return super()._range_chunk_is_cached(method, start, end)

    def _missing_subranges(
        self, method: str, start: date, end: date
    ) -> tuple[tuple[date, date], ...]:
        """Ask the summaries store what it lacks instead of walking the request.

        The request window is ``asof - N days``, so its chunk grid shifts by a day
        every run and the chunk holding the as-of covers up to 31 days that are
        already stored. J-Quants expands a range into per-day calls, so that grid
        costs a month of calls to add one day. Reading the coverage complement
        instead makes the fetch as wide as the gap: one day on a daily run, exactly
        the outage after a stop, and the full window on an empty store.
        """
        if self._sqlite_path is None or method != "get_fin_summary_range":
            return super()._missing_subranges(method, start, end)
        from baibai_engine.market.sqlite import connect_current, missing_intervals

        conn = connect_current(self._sqlite_path)
        if conn is None:
            return super()._missing_subranges(method, start, end)
        try:
            return missing_intervals(conn, "jquants_fin_summaries", start, end)
        finally:
            conn.close()

    def _store_records(
        self,
        method: str,
        records: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> None:
        if self._sqlite_path is None:
            return
        if method == "get_eq_master":
            from baibai_engine.market.sqlite.ingest import store_jquants_master

            requested_asof = params.get("requested_asof")
            if not isinstance(requested_asof, date):
                raise JQuantsProviderError("get_eq_master store requires requested_asof")
            store_jquants_master(self._sqlite_path, records, requested_asof=requested_asof)
            return
        if method == "get_fin_summary_range":
            from baibai_engine.market.sqlite.ingest import store_jquants_fin_summaries

            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                return
            store_jquants_fin_summaries(
                self._sqlite_path,
                records,
                requested_start=start,
                requested_end=end,
            )
            return
        if method == "get_mkt_margin_interest":
            from baibai_engine.market.sqlite.ingest import (
                store_jquants_all_issues_daily_margin,
                store_jquants_weekly_margin,
            )

            daily_balance_date = params.get("all_issues_daily_balance_date")
            if isinstance(daily_balance_date, date):
                store_jquants_all_issues_daily_margin(
                    self._sqlite_path, records, balance_date=daily_balance_date
                )
                return
            week_end = params.get("week_end")
            if not isinstance(week_end, date):
                raise JQuantsProviderError("get_mkt_margin_interest store requires week_end")
            store_jquants_weekly_margin(self._sqlite_path, records, week_end=week_end)
            return
        if method == "get_mkt_margin_alert_range":
            from baibai_engine.market.sqlite.ingest import store_jquants_margin_alerts

            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                raise JQuantsProviderError(
                    "get_mkt_margin_alert_range store requires start_dt and end_dt"
                )
            store_jquants_margin_alerts(
                self._sqlite_path,
                records,
                requested_start=start,
                requested_end=end,
            )
            return
        if method == "get_mkt_short_sale_report":
            from baibai_engine.market.sqlite.ingest import store_jquants_short_sale_reports

            start = params.get("start_dt")
            end = params.get("end_dt")
            if not isinstance(start, date) or not isinstance(end, date):
                raise JQuantsProviderError(
                    "get_mkt_short_sale_report store requires start_dt and end_dt"
                )
            store_jquants_short_sale_reports(
                self._sqlite_path,
                records,
                requested_start=start,
                requested_end=end,
            )
            return
        super()._store_records(method, records, params)


def normalize_weekly_margin(
    record: Mapping[str, Any], week_end: date
) -> JQuantsWeeklyMargin | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # `coalesce_field` throughout: a zero balance is an observation, and for a
    # 信用銘柄 the zero short balance is the normal state rather than a gap.
    return JQuantsWeeklyMargin(
        ticker=ticker,
        week_end=week_end,
        long_vol=to_float(coalesce_field(record, "LongVol", "long_vol")),
        short_vol=to_float(coalesce_field(record, "ShrtVol", "shrt_vol")),
        long_std_vol=to_float(coalesce_field(record, "LongStdVol", "long_std_vol")),
        long_neg_vol=to_float(coalesce_field(record, "LongNegVol", "long_neg_vol")),
        short_std_vol=to_float(coalesce_field(record, "ShrtStdVol", "shrt_std_vol")),
        short_neg_vol=to_float(coalesce_field(record, "ShrtNegVol", "shrt_neg_vol")),
        issue_type=_issue_type(coalesce_field(record, "IssType", "iss_type")),
    )


def normalize_all_issues_daily_margin(
    record: Mapping[str, Any],
) -> JQuantsAllIssuesDailyMargin | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    return JQuantsAllIssuesDailyMargin(
        ticker=ticker,
        balance_date=parse_date(first_value(record, "Date", "balance_date")),
        long_vol=to_float(coalesce_field(record, "LongVol", "long_vol")),
        short_vol=to_float(coalesce_field(record, "ShrtVol", "shrt_vol")),
        long_std_vol=to_float(coalesce_field(record, "LongStdVol", "long_std_vol")),
        long_neg_vol=to_float(coalesce_field(record, "LongNegVol", "long_neg_vol")),
        short_std_vol=to_float(coalesce_field(record, "ShrtStdVol", "shrt_std_vol")),
        short_neg_vol=to_float(coalesce_field(record, "ShrtNegVol", "shrt_neg_vol")),
        issue_type=_issue_type(coalesce_field(record, "IssType", "iss_type")),
    )


def normalize_margin_alert(record: Mapping[str, Any]) -> JQuantsMarginAlert | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    return JQuantsMarginAlert(
        publication_date=parse_date(
            first_value(record, "PubDate", "PublicationDate", "publication_date")
        ),
        ticker=ticker,
        applied_date=parse_optional_date(
            coalesce_field(record, "AppDate", "ApplicationDate", "applied_date")
        ),
        publication_reason=_optional_text(coalesce_field(record, "PubReason", "publication_reason"))
        or None,
        short_outstanding=to_float(coalesce_field(record, "ShrtOut", "short_outstanding")),
        short_change=to_float(coalesce_field(record, "ShrtOutChg", "short_change")),
        short_ratio=to_float(coalesce_field(record, "ShrtOutRatio", "short_ratio")),
        long_outstanding=to_float(coalesce_field(record, "LongOut", "long_outstanding")),
        long_change=to_float(coalesce_field(record, "LongOutChg", "long_change")),
        long_ratio=to_float(coalesce_field(record, "LongOutRatio", "long_ratio")),
        short_long_ratio=to_float(coalesce_field(record, "SLRatio", "short_long_ratio")),
        short_negotiable_outstanding=to_float(
            coalesce_field(record, "ShrtNegOut", "short_negotiable_outstanding")
        ),
        short_negotiable_change=to_float(
            coalesce_field(record, "ShrtNegOutChg", "short_negotiable_change")
        ),
        short_standard_outstanding=to_float(
            coalesce_field(record, "ShrtStdOut", "short_standard_outstanding")
        ),
        short_standard_change=to_float(
            coalesce_field(record, "ShrtStdOutChg", "short_standard_change")
        ),
        long_negotiable_outstanding=to_float(
            coalesce_field(record, "LongNegOut", "long_negotiable_outstanding")
        ),
        long_negotiable_change=to_float(
            coalesce_field(record, "LongNegOutChg", "long_negotiable_change")
        ),
        long_standard_outstanding=to_float(
            coalesce_field(record, "LongStdOut", "long_standard_outstanding")
        ),
        long_standard_change=to_float(
            coalesce_field(record, "LongStdOutChg", "long_standard_change")
        ),
        tse_margin_regulation_classification=_optional_text(
            coalesce_field(
                record,
                "TSEMrgnRegCls",
                "tse_margin_regulation_classification",
            )
        )
        or None,
    )


def normalize_short_sale_report(
    record: Mapping[str, Any],
) -> JQuantsShortSaleReport | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    names = (
        _optional_text(coalesce_field(record, "ShortSellerName", "SSName", "short_seller_name")),
        _optional_text(
            coalesce_field(
                record,
                "DiscretionaryInvestmentContractorName",
                "DICName",
                "discretionary_investment_contractor_name",
            )
        ),
        _optional_text(
            coalesce_field(record, "InvestmentFundName", "FundName", "investment_fund_name") or ""
        ),
    )
    ratio = to_float(
        coalesce_field(
            record,
            "ShortPositionsToSharesOutstandingRatio",
            "ShortPosRatio",
            "ShrtPosToSO",
            "short_ratio",
        )
    )
    notes_value = coalesce_field(record, "Notes", "notes")
    notes = _optional_text(notes_value) or None
    is_cancellation = ratio is None and bool(notes)
    if not any(names) or (ratio is None and not is_cancellation) or (ratio or 0.0) < 0.0:
        return None
    return JQuantsShortSaleReport(
        disclosed_at=parse_date(first_value(record, "DiscDate", "DisclosedDate", "disclosed_at")),
        calculated_at=parse_date(
            first_value(record, "CalcDate", "CalculatedDate", "calculated_at")
        ),
        ticker=ticker,
        short_seller_name=names[0],
        discretionary_investment_contractor_name=names[1],
        investment_fund_name=names[2],
        short_ratio=ratio,
        short_shares=_optional_int(
            coalesce_field(
                record,
                "ShortPositionsInSharesNumber",
                "ShortPosShares",
                "ShrtPosShares",
                "short_shares",
            )
        ),
        short_trading_units=_optional_int(
            coalesce_field(
                record,
                "ShortPositionsInTradingUnitsNumber",
                "ShortPosTradingUnits",
                "ShrtPosUnits",
                "short_trading_units",
            )
        ),
        previous_reported_at=parse_optional_date(
            coalesce_field(
                record,
                "PrevRptDate",
                "CalculationInPreviousReportingDate",
                "previous_reported_at",
            )
        ),
        previous_short_ratio=to_float(
            coalesce_field(
                record,
                "ShortPositionsInPreviousReportingRatio",
                "PrevShortPosRatio",
                "PrevRptRatio",
                "previous_short_ratio",
            )
        ),
        is_cancellation=is_cancellation,
        notes=notes,
    )


def _optional_int(value: Any) -> int | None:
    parsed = to_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _optional_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    text = str(value).strip()
    return "" if text in {"NaT", "nan"} else text


def _issue_type(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def normalize_financial_summary(record: Mapping[str, Any]) -> JQuantsFinancialSummary | None:
    ticker, common_code = parse_jquants_code_parts(first_value(record, "Code", "code"))
    if not common_code:
        return None
    # 会社予想の純利益/経常のペアは forecast_eps と同一予想期から採る。当期予想 EPS
    # (FEPS) が埋まっていれば当期予想の FNP/FOdP、本決算開示で FEPS が空なら翌期
    # ガイダンスの NxFNp/NxFOdP を採る (forecast_eps の FEPS→NxFEPS と同じ期選択)。
    # 期をまたいだ比較 (当期 EPS 期 と翌期利益の突合) は一時益 flag の誤検出になるので混ぜない。
    if has_period_slot(record, "FEPS"):
        forecast_profit = to_float(coalesce_field(record, "FNP"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "FOdP"))
    else:
        forecast_profit = to_float(coalesce_field(record, "NxFNp"))
        forecast_ordinary_profit = to_float(coalesce_field(record, "NxFOdP"))
    # すべて `coalesce_field` 経由にして 0.0 の数値フィールドを欠損と誤判定しないようにする
    # (EPS=0 の赤字転換点、Sales=0 の新規事業初期、OP=0 の損益分岐点ちょうど、など)。
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=parse_date(
            first_value(
                record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date", "date"
            )
        ),
        # ClientV2 の fin-summary は短縮キーを返す。FEPS=当期予想 EPS は本決算(FY)
        # 開示で空になり、翌期ガイダンスは NxFEPS に入る。FEPS→NxFEPS の順で各時点の
        # 最良 forward EPS(per_forward の基)を埋める。
        forecast_eps=first_period_float(record, "FEPS", "NxFEPS"),
        eps_ttm=to_float(
            coalesce_field(
                record,
                "EpsTtm",
                "eps_ttm",
                "EarningsPerShare",
                "earnings_per_share",
                "EPS",
                "eps",
            )
        ),
        bps=to_float(
            coalesce_field(record, "BPS", "bps", "BookValuePerShare", "book_value_per_share")
        ),
        shares_outstanding=to_float(
            coalesce_field(
                record,
                "SharesOutstanding",
                "shares_outstanding",
                "IssuedShareEquityQuote",
                "issued_share_equity_quote",
                "ShOutFY",
            )
        ),
        sales=to_float(coalesce_field(record, "NetSales", "net_sales", "Sales", "sales")),
        cfo=to_float(
            coalesce_field(
                record,
                "CashFlowsFromOperatingActivities",
                "cash_flows_from_operating_activities",
                "OperatingCashFlow",
                "operating_cash_flow",
                "CFO",
                "cfo",
            )
        ),
        cash_eq=to_float(
            coalesce_field(
                record,
                "CashAndEquivalents",
                "cash_and_equivalents",
                "CashEq",
                "cash_eq",
            )
        ),
        total_assets=to_float(coalesce_field(record, "TotalAssets", "total_assets", "TA", "ta")),
        equity=to_float(coalesce_field(record, "Equity", "equity", "Eq", "eq")),
        operating_profit=to_float(
            coalesce_field(record, "OperatingProfit", "operating_profit", "OP")
        ),
        ordinary_profit=to_float(
            coalesce_field(record, "OrdinaryProfit", "ordinary_profit", "OdP")
        ),
        profit=to_float(coalesce_field(record, "Profit", "profit", "NP")),
        forecast_profit=forecast_profit,
        forecast_ordinary_profit=forecast_ordinary_profit,
        fiscal_period=to_period(
            coalesce_field(
                record,
                "TypeOfCurrentPeriod",
                "type_of_current_period",
                "CurPerType",
            )
        ),
        fiscal_year_end=parse_optional_date(
            coalesce_field(
                record,
                "CurrentFiscalYearEndDate",
                "current_fiscal_year_end_date",
                "CurFYEn",
            )
        ),
        period_start=parse_optional_date(
            coalesce_field(
                record,
                "CurrentPeriodStartDate",
                "current_period_start_date",
                "CurPerSt",
            )
        ),
        period_end=parse_optional_date(
            coalesce_field(
                record,
                "CurrentPeriodEndDate",
                "current_period_end_date",
                "CurPerEn",
            )
        ),
        # DivAnn=実績年間 DPS。予想年間は FDivAnn (四半期) → NxFDivAnn (本決算の
        # 進行期ガイダンス) の順で埋める (FEPS→NxFEPS と同型)。
        dps_actual_annual=to_float(coalesce_field(record, "DivAnn")),
        dps_forecast_annual=first_period_float(record, "FDivAnn", "NxFDivAnn"),
        # TrShFY = 期末自己株式数、EqAR = 開示された自己資本比率。ShOutFY (発行済・自己株
        # 込み) と Eq (純資産) だけでは時価総額も自己資本比率も正しい分母で作れない。
        treasury_shares=to_float(
            coalesce_field(record, "TrShFY", "treasury_shares", "TreasuryStock")
        ),
        equity_to_asset_ratio=to_float(
            coalesce_field(record, "EqAR", "equity_to_asset_ratio", "EquityToAssetRatio")
        ),
        # 支払ごとの 1 株当たり配当。各支払の基準日 (四半期末) より後の調整だけを掛ければ
        # as-of 基準へ寄せられる。DivTotalAnn は円なので株式基準を持たず、自己株式を除いた
        # 株式数で割った値がその換算の独立した照合になる。AvgSh はその株数の健全性を同じ
        # 行の中で確かめるためのアンカー。
        dividend_q1=to_float(coalesce_field(record, "Div1Q")),
        dividend_interim=to_float(coalesce_field(record, "Div2Q")),
        dividend_q3=to_float(coalesce_field(record, "Div3Q")),
        dividend_year_end=to_float(coalesce_field(record, "DivFY")),
        dividend_total_annual=to_float(coalesce_field(record, "DivTotalAnn")),
        average_shares=to_float(coalesce_field(record, "AvgSh")),
    )
