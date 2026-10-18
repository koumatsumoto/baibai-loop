"""Explicit J-Quants maintenance: acquire L1, then project and verify L2.

This command-side composition owns fetch planning. The reader has no reference
back to it, and Collection never imports these market coverage rules.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.collection.jquants.http import JQuantsHTTP, JQuantsSource
from baibai_engine.collection.jquants.receipts import acquire_request
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.market.jquants_reader import JQuantsReader
from baibai_engine.market.margin_publication import (
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
from baibai_engine.market.store import (
    count_daily_bars,
    daily_bars_covered,
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_market_calendar,
)
from baibai_engine.storage.observations.coverage import merge_date_ranges, missing_intervals
from baibai_engine.storage.observations.jquants_contract import SHORT_SALE_REPORT_SOURCE
from baibai_engine.storage.observations.reader import (
    count_fin_summaries,
    fin_summaries_covered,
    read_all_issues_daily_margin,
    read_eq_master_exact,
    read_weekly_margin,
)
from baibai_engine.storage.observations.schema import connect_current, open_connection

Progress = Callable[[int, int, date, date], None]


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("request start must not follow end")


class JQuantsRefresh:
    def __init__(
        self,
        sqlite_path: Path,
        api_key: str | None,
        *,
        client: JQuantsHTTP | None = None,
    ) -> None:
        self._path = sqlite_path
        self._api_key = api_key
        self._client = client
        self._reader = JQuantsReader(sqlite_path)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _fetch(self, source: JQuantsSource, params: dict[str, str]) -> None:
        if self._client is None:
            self._client = JQuantsHTTP(self._api_key or "")
        with closing(open_connection(self._path)) as conn:
            request_id = acquire_request(conn, self._client, source, params)
        # Acquisition has committed before any interpretation can fail.
        project_request(self._path, request_id)

    def _fetch_days(
        self,
        source: JQuantsSource,
        ranges: Sequence[tuple[date, date]],
        *,
        progress: Progress | None = None,
    ) -> None:
        planned = merge_date_ranges(ranges)
        total = sum((end - start).days + 1 for start, end in planned)
        index = 0
        for start, end in planned:
            day = start
            while day <= end:
                # Preserve bounded pacing between 31-day blocks. Each individual
                # GET also honors provider Retry-After and transient HTTP retries.
                if index and index % 31 == 0:
                    time.sleep(3.0)
                key = "disc_date" if source == "short_sale_reports" else "date"
                value = (
                    day.strftime("%Y%m%d") if source == "financial_summaries" else day.isoformat()
                )
                self._fetch(source, {key: value})
                index += 1
                if progress is not None:
                    progress(index, total, day, day)
                day += timedelta(days=1)

    def _missing(self, source: str, start: date, end: date) -> tuple[tuple[date, date], ...]:
        _check_range(start, end)
        conn = connect_current(self._path)
        if conn is None:
            return ((start, end),)
        with closing(conn):
            return missing_intervals(conn, source, start, end)

    def ensure_master(self, day: date) -> int:
        cached = read_eq_master_exact(self._path, day)
        if cached is None:
            self._fetch("master", {"date": day.isoformat()})
        return len(self._reader.get_eq_master(day))

    def ensure_market_calendar(self, start: date, end: date) -> int:
        _check_range(start, end)
        if read_market_calendar(self._path, start, end) is None:
            self._fetch(
                "market_calendar",
                {"from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")},
            )
        return len(self._reader.get_mkt_calendar(start, end))

    def ensure_daily_bars(self, start: date, end: date) -> int:
        missing = self._missing("jquants_daily_bars", start, end)
        if not missing:
            latest = latest_daily_bar_date(self._path, start, end)
            stored_latest = latest_stored_daily_bar_date(self._path)
            if (
                latest is None
                or latest >= end
                or (stored_latest is not None and end < stored_latest)
            ):
                return count_daily_bars(self._path, start, end)
            self._fetch_days("daily_bars", ((latest, end),))
        else:
            self._fetch_days("daily_bars", missing)
        if not daily_bars_covered(self._path, start, end):
            raise JQuantsProviderError("daily bar projection remains incomplete after acquisition")
        return count_daily_bars(self._path, start, end)

    def ensure_fin_summaries(self, start: date, end: date) -> int:
        self._fetch_days("financial_summaries", self._missing("jquants_fin_summaries", start, end))
        return self._financial_count(start, end)

    def refresh_fin_summaries(
        self,
        start: date,
        end: date,
        *,
        revision_overlap_days: int,
        progress: Progress | None = None,
    ) -> int:
        _check_range(start, end)
        if revision_overlap_days < 0:
            raise ValueError("revision overlap must not be negative")
        overlap = max(start, end - timedelta(days=revision_overlap_days))
        planned = merge_date_ranges(
            [
                *self._missing("jquants_fin_summaries", start, end),
                (overlap, end),
            ]
        )
        self._fetch_days("financial_summaries", planned, progress=progress)
        return self._financial_count(start, end)

    def _financial_count(self, start: date, end: date) -> int:
        if not fin_summaries_covered(self._path, start, end):
            raise JQuantsProviderError("financial projection remains incomplete after acquisition")
        return count_fin_summaries(self._path, start, end)

    def ensure_weekly_margin(self, day: date) -> int:
        require_legacy_weekly_balance_date(day)
        if read_weekly_margin(self._path, day) is None:
            self._fetch("margin_interest", {"date": day.strftime("%Y%m%d")})
        return len(self._reader.get_mkt_margin_interest_week(day))

    def refresh_weekly_margin(self, day: date) -> int:
        require_legacy_weekly_balance_date(day)
        self._fetch("margin_interest", {"date": day.strftime("%Y%m%d")})
        return len(self._reader.get_mkt_margin_interest_week(day))

    def ensure_all_issues_daily_margin(self, day: date) -> int:
        require_all_issues_daily_balance_date(day)
        if read_all_issues_daily_margin(self._path, day) is None:
            self._fetch("margin_interest", {"date": day.strftime("%Y%m%d")})
        return len(self._reader.get_mkt_all_issues_daily_margin(day))

    def ensure_margin_alerts(self, start: date, end: date) -> int:
        self._fetch_days("margin_alerts", self._missing("jquants_margin_alerts", start, end))
        return len(self._reader.get_mkt_margin_alert_range(start, end))

    def refresh_margin_alerts(self, start: date, end: date) -> int:
        _check_range(start, end)
        self._fetch_days("margin_alerts", ((start, end),))
        return len(self._reader.get_mkt_margin_alert_range(start, end))

    def ensure_short_sale_reports(self, start: date, end: date) -> int:
        self._fetch_days("short_sale_reports", self._missing(SHORT_SALE_REPORT_SOURCE, start, end))
        return len(self._reader.get_mkt_short_sale_report_range(start, end))

    def refresh_short_sale_reports(self, start: date, end: date) -> int:
        _check_range(start, end)
        planned: tuple[tuple[date, date], ...] = ((start, end),)
        conn = connect_current(self._path)
        if conn is not None:
            with closing(conn):
                row = conn.execute(
                    "SELECT MIN(coverage_start) FROM source_coverage "
                    "WHERE source = ? AND status = 'ok'",
                    (SHORT_SALE_REPORT_SOURCE,),
                ).fetchone()
                first = date.fromisoformat(str(row[0])) if row is not None and row[0] else None
                repairs = (
                    missing_intervals(conn, SHORT_SALE_REPORT_SOURCE, first, end) if first else ()
                )
                planned = merge_date_ranges([*repairs, (start, end)])
        self._fetch_days("short_sale_reports", planned)
        return len(self._reader.get_mkt_short_sale_report_range(start, end))
