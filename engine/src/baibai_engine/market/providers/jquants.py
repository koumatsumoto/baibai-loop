"""J-Quantsのprice/calendar・財務・信用入力を取得する単一adapter。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

from requests.adapters import HTTPAdapter
from urllib3.exceptions import InvalidHeader
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry

from baibai_engine.collection.prices import (
    JQuantsAdjustmentFactorEvent as JQuantsAdjustmentFactorEvent,
)
from baibai_engine.collection.prices import JQuantsDailyBar
from baibai_engine.market.calendar import JQuantsMarketCalendarDay
from baibai_engine.market.config import JQUANTS_CLIENT_V2_METHODS
from baibai_engine.market.financial_documents import (
    normalize_financial_summary as normalize_financial_summary,
)
from baibai_engine.market.financial_documents import project_financial_documents
from baibai_engine.market.jquants_decode import JQuantsProviderError as JQuantsProviderError
from baibai_engine.market.jquants_decode import (
    _parse_yyyymmdd_param,
    _payload_to_records,
    _stringify_dates,
    normalize_daily_bar,
    normalize_market_calendar,
    parse_date,
    parse_jquants_code_parts,
    parse_optional_date,
    to_float,
)
from baibai_engine.market.jquants_decode import (
    normalize_adjustment_factor_event as normalize_adjustment_factor_event,
)
from baibai_engine.market.jquants_decode import parse_jquants_code as parse_jquants_code
from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.master_snapshot import masters_from_payloads, validate_master_snapshot
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.store import (
    count_daily_bars,
    daily_bars_covered,
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_daily_bars,
    read_market_calendar,
)
from baibai_engine.storage.observations.jquants import (
    store_jquants_daily_bars,
    store_jquants_market_calendar,
)

_RATE_LIMIT_BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class _JQuantsReadRetry(Retry):
    """One GET's retry policy; pagination and range helpers must not restart it."""

    def get_backoff_time(self) -> float:
        if not self.history:
            return 0.0
        return float(_RATE_LIMIT_BACKOFF_SECONDS[min(len(self.history), 5) - 1])

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
    _INTER_CHUNK_SLEEP_SECONDS = 3.0

    def __init__(
        self,
        api_key: str | None,
        client: Any | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
    ) -> None:
        self._api_key = api_key
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
                # Reuse a historical window accepted by the coverage policy.
                # Later stored dates do not prove that its edge gap is a holiday.
                # Implicit re-fetching would delete and rewrite
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
        """Fetch as needed under the stored-date coverage policy and count non-null closes.

        Bootstrap and backfill need a count, not one model per stored bar. The
        post-fetch coverage check rejects windows outside that policy, but its
        tolerated gaps do not prove every trading date or instrument is present.
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
        # Fetch every category, including non-business and holiday-trading days.
        # Storage retains HolDiv; this cash-equity projection selects categories 1/2.
        records = self._load_or_fetch(
            "get_mkt_calendar",
            from_yyyymmdd=start.strftime("%Y%m%d"),
            to_yyyymmdd=end.strftime("%Y%m%d"),
        )
        if self._sqlite_path is not None:
            stored = read_market_calendar(self._sqlite_path, start, end)
            if stored is None:
                raise JQuantsProviderError(
                    "stored market calendar remains incomplete after fetching"
                )
            return stored
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
        payload = self._call(method, call, **params)

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

    def _call(self, method: str, call: Any, **params: Any) -> Any:
        try:
            return call(**_stringify_dates(params))
        except Exception as exc:
            # SDK HTTP retries belong to each GET. Retrying the enclosing SDK
            # method would reset that budget and replay earlier successful pages.
            sanitized = self._sanitize_secret(str(exc))
            raise JQuantsProviderError(
                f"failed to fetch J-Quants payload via {method}: {type(exc).__name__}: {sanitized}"
            ) from None

    def _sanitize_secret(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "<redacted>")
        return text

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise JQuantsProviderError("JQUANTS_API_KEY is required for provider fetch")
        try:
            import jquantsapi
        except ModuleNotFoundError as exc:
            raise JQuantsProviderError("jquantsapi is not installed") from exc
        self._client = jquantsapi.ClientV2(api_key=self._api_key)
        # ClientV2 2.7 has no public session/retry constructor setting. Configure
        # its existing HTTPS adapter once, retaining its pool, proxy/TLS and GET
        # timeout. Only this read client's transport changes; no SDK global patch.
        adapter = cast("HTTPAdapter", self._client._request_session().adapters["https://"])
        adapter.max_retries = _JQuantsReadRetry(
            total=5,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 500, 502, 503, 504),
        )
        return self._client


class JQuantsProvider(_JQuantsTransport):
    """Cache-first adapter for jquantsapi.ClientV2: price/calendar + fundamentals."""

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.reader import read_eq_master_exact

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
            from baibai_engine.storage.observations.reader import read_eq_master_exact

            stored = read_eq_master_exact(self._sqlite_path, requested_asof)
            if stored is None:
                raise JQuantsProviderError(
                    "SQLite cache remained incomplete after fetching jquants_master_snapshots "
                    f"for {requested_asof.isoformat()}"
                )
            return stored
        validated = validate_master_snapshot(records, requested_asof)
        return masters_from_payloads([payload for _, _, payload in validated.rows])

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
            from baibai_engine.storage.observations.reader import read_weekly_margin

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
            from baibai_engine.storage.observations.reader import read_weekly_margin

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
            from baibai_engine.storage.observations.reader import read_weekly_margin

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
            from baibai_engine.storage.observations.reader import read_margin_alerts

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
            from baibai_engine.storage.observations.reader import read_margin_alerts

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
            from baibai_engine.storage.observations.reader import read_all_issues_daily_margin

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
            from baibai_engine.storage.observations.reader import read_all_issues_daily_margin

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
            from baibai_engine.storage.observations.reader import read_short_sale_reports

            cached = read_short_sale_reports(self._sqlite_path, start, end)
            if cached is not None:
                return cached
        self._raise_if_cache_only(
            "jquants_short_sale_reports", f"{start.isoformat()}..{end.isoformat()}"
        )
        missing: tuple[tuple[date, date], ...] = ((start, end),)
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.coverage import missing_intervals
            from baibai_engine.storage.observations.schema import connect_current

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
            from baibai_engine.storage.observations.reader import read_short_sale_reports

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
            from baibai_engine.storage.observations.coverage import (
                merge_date_ranges,
                missing_intervals,
            )
            from baibai_engine.storage.observations.jquants_contract import SHORT_SALE_REPORT_SOURCE
            from baibai_engine.storage.observations.schema import connect_current

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
            from baibai_engine.storage.observations.reader import read_short_sale_reports

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
        """Return price-adjustment events under the daily-bar coverage policy."""
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
                "SQLite cache remained incomplete after fetching price-adjustment bars "
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
            from baibai_engine.storage.observations.reader import read_fin_summaries

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
        return project_financial_documents(
            [
                summary
                for record in records
                if (summary := normalize_financial_summary(record)) is not None
            ],
            end,
        )

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        """Retain every FY document, including missing EPS and unresolved revisions."""
        return [
            summary
            for summary in self.get_fin_summary_range(start, end)
            if summary.fiscal_period == "FY"
        ]

    def refresh_fin_summary_range(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Callable[[int, int, date, date], None] | None = None,
    ) -> int:
        """Bring the summaries store current for `[start, end]` and count what it holds.

        Two things are fetched: what the coverage says is missing, and the trailing
        ``revision_overlap_days`` regardless of coverage. The overlap is the only
        way this refresh discovers a late publication for an already covered date
        within that trailing window. Older corrections require refreshing their window;
        acquisition coverage alone does not prove that no later revision exists.

        The two are merged before anything is requested, so a stop longer than the
        overlap costs the outage and not the outage plus a week. Asking once, here,
        is also what keeps the 730-day and 2,200-day windows of the same source from
        each paying for the same trailing week.
        """
        window = f"{start.isoformat()}..{end.isoformat()}"
        self._raise_if_cache_only("jquants_fin_summaries", window)
        if self._sqlite_path is None:
            return len(self._load_or_fetch_range("get_fin_summary_range", start, end))
        from baibai_engine.storage.observations.coverage import merge_date_ranges
        from baibai_engine.storage.observations.reader import (
            count_fin_summaries,
            fin_summaries_covered,
        )

        overlap_start = max(start, end - timedelta(days=revision_overlap_days))
        planned = merge_date_ranges(
            [
                *self._missing_subranges("get_fin_summary_range", start, end),
                (overlap_start, end),
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
            from baibai_engine.storage.observations.reader import fin_summaries_covered

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
        from baibai_engine.storage.observations.coverage import missing_intervals
        from baibai_engine.storage.observations.schema import connect_current

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
            from baibai_engine.storage.observations.ingest.jquants import store_jquants_master

            requested_asof = params.get("requested_asof")
            if not isinstance(requested_asof, date):
                raise JQuantsProviderError("get_eq_master store requires requested_asof")
            store_jquants_master(self._sqlite_path, records, requested_asof=requested_asof)
            return
        if method == "get_fin_summary_range":
            from baibai_engine.storage.observations.ingest.jquants import (
                store_jquants_fin_summaries,
            )

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
            from baibai_engine.storage.observations.ingest.jquants import (
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
            from baibai_engine.storage.observations.ingest.jquants import (
                store_jquants_margin_alerts,
            )

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
            from baibai_engine.storage.observations.ingest.jquants import (
                store_jquants_short_sale_reports,
            )

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
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    if parse_date(record.get("Date")) != week_end:
        raise JQuantsProviderError("weekly margin balance date does not match request")
    # Zero balances remain observations, not missing values.
    return JQuantsWeeklyMargin(
        ticker=ticker,
        week_end=week_end,
        long_vol=to_float(record.get("LongVol")),
        short_vol=to_float(record.get("ShrtVol")),
        long_std_vol=to_float(record.get("LongStdVol")),
        long_neg_vol=to_float(record.get("LongNegVol")),
        short_std_vol=to_float(record.get("ShrtStdVol")),
        short_neg_vol=to_float(record.get("ShrtNegVol")),
        issue_type=_issue_type(record.get("IssType")),
    )


def normalize_all_issues_daily_margin(
    record: Mapping[str, Any],
) -> JQuantsAllIssuesDailyMargin | None:
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    return JQuantsAllIssuesDailyMargin(
        ticker=ticker,
        balance_date=parse_date(record.get("Date")),
        long_vol=to_float(record.get("LongVol")),
        short_vol=to_float(record.get("ShrtVol")),
        long_std_vol=to_float(record.get("LongStdVol")),
        long_neg_vol=to_float(record.get("LongNegVol")),
        short_std_vol=to_float(record.get("ShrtStdVol")),
        short_neg_vol=to_float(record.get("ShrtNegVol")),
        issue_type=_issue_type(record.get("IssType")),
    )


def normalize_margin_alert(record: Mapping[str, Any]) -> JQuantsMarginAlert | None:
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    return JQuantsMarginAlert(
        publication_date=parse_date(record.get("PubDate")),
        ticker=ticker,
        applied_date=parse_optional_date(record.get("AppDate")),
        publication_reason=_optional_text(record.get("PubReason")) or None,
        short_outstanding=to_float(record.get("ShrtOut")),
        short_change=to_float(record.get("ShrtOutChg")),
        short_ratio=to_float(record.get("ShrtOutRatio")),
        long_outstanding=to_float(record.get("LongOut")),
        long_change=to_float(record.get("LongOutChg")),
        long_ratio=to_float(record.get("LongOutRatio")),
        short_long_ratio=to_float(record.get("SLRatio")),
        short_negotiable_outstanding=to_float(record.get("ShrtNegOut")),
        short_negotiable_change=to_float(record.get("ShrtNegOutChg")),
        short_standard_outstanding=to_float(record.get("ShrtStdOut")),
        short_standard_change=to_float(record.get("ShrtStdOutChg")),
        long_negotiable_outstanding=to_float(record.get("LongNegOut")),
        long_negotiable_change=to_float(record.get("LongNegOutChg")),
        long_standard_outstanding=to_float(record.get("LongStdOut")),
        long_standard_change=to_float(record.get("LongStdOutChg")),
        tse_margin_regulation_classification=_optional_text(record.get("TSEMrgnRegCls")) or None,
    )


def normalize_short_sale_report(
    record: Mapping[str, Any],
) -> JQuantsShortSaleReport | None:
    """Read native V2 fields; a missing ratio or a note does not establish cancellation."""
    ticker, common_code = parse_jquants_code_parts(record.get("Code"))
    if not common_code:
        return None
    names = tuple(_optional_text(record.get(key)) for key in ("SSName", "DICName", "FundName"))
    if not any(names):
        return None
    return JQuantsShortSaleReport(
        disclosed_at=parse_date(record.get("DiscDate")),
        calculated_at=parse_date(record.get("CalcDate")),
        ticker=ticker,
        short_seller_name=names[0],
        discretionary_investment_contractor_name=names[1],
        investment_fund_name=names[2],
        short_ratio=to_float(record.get("ShrtPosToSO")),
        short_shares=_optional_int(record.get("ShrtPosShares")),
        short_trading_units=_optional_int(record.get("ShrtPosUnits")),
        previous_reported_at=parse_optional_date(record.get("PrevRptDate")),
        previous_short_ratio=to_float(record.get("PrevRptRatio")),
        notes=_optional_text(record.get("Notes")) or None,
        short_seller_address=_optional_text(record.get("SSAddr")) or None,
        discretionary_investment_contractor_address=_optional_text(record.get("DICAddr")) or None,
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
