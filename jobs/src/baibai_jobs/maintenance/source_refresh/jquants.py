"""Explicit J-Quants maintenance: acquire L1, then project and verify L2.

This command-side composition owns fetch planning. The reader has no reference
back to it, and Collection never imports these market coverage rules.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP, JQuantsSource
from baibai_engine.data_collection.providers.jquants.receipts import acquire_request
from baibai_engine.primitives.time import JST
from baibai_engine.machine_analysis.observations.trading_activity.jquants_contract import (
    ALL_ISSUES_DAILY_MARGIN_SOURCE,
    SHORT_SALE_REPORT_SOURCE,
    WEEKLY_MARGIN_SOURCE,
    all_issues_daily_margin_coverage_key,
    weekly_margin_coverage_key,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_projection import project_request
from baibai_jobs.maintenance.jquants_inputs import (
    JQuantsReader,
)
from baibai_engine.machine_analysis.observations.financials.reader import (
    count_fin_summaries,
    fin_summaries_covered,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_exact,
)
from baibai_engine.machine_analysis.observations.prices.calendar_reader import (
    read_market_calendar,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_publication import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    MARGIN_PUBLICATION_TRADING_DAYS,
    _publication_trading_day,
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import read_all_issues_daily_margin, read_weekly_margin
from baibai_engine.machine_analysis.observations.prices.price_reader import (
    count_daily_bars,
    daily_bars_covered,
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
)
from baibai_engine.storage.sqlite.source_cache.coverage import merge_date_ranges, missing_intervals
from baibai_engine.storage.sqlite.source_cache.schema import connect_current, open_connection

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


def final_legacy_week_requires_refresh(sqlite_path: Path) -> bool:
    """Whether the final weekly balance still lacks a non-empty clean snapshot."""
    if not sqlite_path.exists():
        return True
    conn = connect_current(sqlite_path)
    if conn is None:
        return True
    try:
        row = conn.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source = ? AND coverage_key = ?",
            (
                WEEKLY_MARGIN_SOURCE,
                weekly_margin_coverage_key(LEGACY_WEEKLY_LAST_BALANCE_DATE),
            ),
        ).fetchone()
    finally:
        conn.close()
    return row is None or row[0] != "ok" or int(row[1] or 0) == 0


def weekly_margin_empty_requires_refresh(
    sqlite_path: Path,
    week_end: date,
    *,
    asof: date,
) -> bool:
    """Whether an empty weekly snapshot was fetched before publication was due.

    Empty is a valid final answer for a week the exchange skipped, but only after
    the second-trading-day publication point.  Older code could ask sooner and
    persist that temporary empty response as clean coverage.  The fetch timestamp
    distinguishes that state from an empty response observed after publication.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        coverage = conn.execute(
            "SELECT status, record_count, fetched_at_utc FROM source_coverage "
            "WHERE source = ? AND coverage_key = ?",
            (WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key(week_end)),
        ).fetchone()
        if coverage is None or coverage[0] != "ok" or int(coverage[1] or 0) != 0:
            return False
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at > ? AND traded_at <= ? ORDER BY traded_at",
                (week_end.isoformat(), asof.isoformat()),
            )
        ]
    finally:
        conn.close()
    publication_day = _publication_trading_day(
        trading_days,
        week_end,
        trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS,
    )
    if publication_day is None or publication_day > asof:
        return False
    try:
        fetched_at = datetime.fromisoformat(str(coverage[2]))
    except (TypeError, ValueError):
        return True
    if fetched_at.tzinfo is None:
        return True
    return fetched_at.astimezone(JST).date() <= publication_day


def weekly_margin_candidate_dates(
    sqlite_path: Path,
    start: date,
    end: date,
    *,
    publication_asof: date | None = None,
) -> list[date]:
    """The last stored trading day of each week in `[start, end]`, ascending.

    The exchange's balance date is that day in most weeks and an earlier one when
    the week's later days were closed — and some weeks have no balance date at all,
    even weeks the market traded. Rather than encode that calendar, this proposes
    one candidate per week and lets the fetch record an empty answer as the week's
    fact. When `publication_asof` is supplied, candidates whose second-trading-day
    publication is still in the future are withheld from the daily bootstrap.
    """
    balance_end = min(end, LEGACY_WEEKLY_LAST_BALANCE_DATE)
    if not sqlite_path.exists() or start > balance_end:
        return []
    calendar_end = max(balance_end, publication_asof or balance_end)
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (start.isoformat(), calendar_end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    trading_days: list[date] = []
    for (value,) in rows:
        try:
            day = date.fromisoformat(str(value))
        except ValueError:
            continue
        trading_days.append(day)
    last_of_week: dict[tuple[int, int], date] = {}
    for day in (candidate for candidate in trading_days if candidate <= balance_end):
        year, week, _ = day.isocalendar()
        last_of_week[(year, week)] = day
    candidates = sorted(last_of_week.values())
    if publication_asof is None:
        # A bounded historical store can end on a long-published balance date.
        # Its cache frontier is not the clock of an in-progress trading week.
        return candidates
    # Daily callers supply their publication cutoff. The latest in-progress
    # week's candidate has no two later trading days by that cutoff and waits.
    return [
        candidate
        for candidate in candidates
        if (
            publication_day := _publication_trading_day(
                trading_days,
                candidate,
                trading_day_lag=MARGIN_PUBLICATION_TRADING_DAYS,
            )
        )
        is not None
        and publication_day <= publication_asof
    ]


def all_issues_daily_margin_candidate_dates(
    sqlite_path: Path,
    start: date,
    asof: date,
) -> list[date]:
    """Unfetched daily balance dates that have a later trading-day publication."""
    start = max(start, ALL_ISSUES_DAILY_FIRST_BALANCE_DATE)
    if not sqlite_path.exists() or start >= asof:
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
                (start.isoformat(), asof.isoformat()),
            )
        ]
        covered = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (ALL_ISSUES_DAILY_MARGIN_SOURCE,),
            )
        }
    finally:
        conn.close()
    # Publication is on the following business day, so the newest trading day has
    # not yet become available. An empty all-issues response cannot describe a
    # complete business-day snapshot and remains a retry target.
    return [
        day for day in trading_days[:-1] if all_issues_daily_margin_coverage_key(day) not in covered
    ]


def all_issues_daily_margin_backfill_candidate_dates(
    sqlite_path: Path,
    start: date,
    end: date,
) -> list[date]:
    """Every uncovered balance date in an inclusive historical backfill window."""
    start = max(start, ALL_ISSUES_DAILY_FIRST_BALANCE_DATE)
    if not sqlite_path.exists() or start > end:
        return []
    conn = connect_current(sqlite_path)
    if conn is None:
        return []
    try:
        trading_days = [
            date.fromisoformat(str(row[0]))
            for row in conn.execute(
                "SELECT DISTINCT traded_at FROM jquants_daily_bars "
                "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
                (start.isoformat(), end.isoformat()),
            )
        ]
        covered = {
            str(row[0])
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage "
                "WHERE source = ? AND status = 'ok' AND record_count > 0",
                (ALL_ISSUES_DAILY_MARGIN_SOURCE,),
            )
        }
    finally:
        conn.close()
    return [day for day in trading_days if all_issues_daily_margin_coverage_key(day) not in covered]
