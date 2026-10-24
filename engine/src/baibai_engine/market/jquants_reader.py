"""Offline J-Quants read models; acquisition is an explicit separate operation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.market import master_snapshot as master_contract
from baibai_engine.market.calendar import JQuantsMarketCalendarDay
from baibai_engine.market.financial_documents import (
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.jquants_models import (
    JQuantsAllIssuesDailyMargin,
    JQuantsFinancialSummary,
    JQuantsMarginAlert,
    JQuantsShortSaleReport,
    JQuantsWeeklyMargin,
)
from baibai_engine.market.margin_publication import (
    require_all_issues_daily_balance_date,
    require_legacy_weekly_balance_date,
)
from baibai_engine.market.margin_reader import (
    read_all_issues_daily_margin,
    read_margin_alerts,
    read_short_sale_reports,
    read_weekly_margin,
)
from baibai_engine.market.master_snapshot import masters_from_payloads
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market.price_reader import read_adjustment_factor_bars, read_daily_bars
from baibai_engine.storage.observations.coverage import covered_intervals, range_covered
from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import connect_current

if TYPE_CHECKING:
    import duckdb


def _required[T](value: T | None, source: str) -> T:
    if value is None:
        raise JQuantsProviderError(
            f"Stored J-Quants input is incomplete: {source}; "
            "explicitly acquire and project it before reading"
        )
    return value


class JQuantsReader:
    """Reads never obtain credentials, call a provider, or change stored data."""

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def get_mkt_calendar(self, start: date, end: date) -> list[JQuantsMarketCalendarDay]:
        return _required(read_market_calendar(self._sqlite_path, start, end), "market_calendar")

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        return _required(read_eq_master_exact(self._sqlite_path, requested_asof), "master")

    def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
        return _required(read_daily_bars(self._sqlite_path, start, end), "daily_bars")

    def get_adjustment_factor_bars_range(
        self, start: date, end: date
    ) -> list[JQuantsAdjustmentFactorEvent]:
        return _required(
            read_adjustment_factor_bars(self._sqlite_path, start, end), "adjustment_factors"
        )

    def get_fin_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        return _required(read_fin_summaries(self._sqlite_path, start, end), "financial_summaries")

    def get_fy_summary_range(self, start: date, end: date) -> list[JQuantsFinancialSummary]:
        return [row for row in self.get_fin_summary_range(start, end) if row.fiscal_period == "FY"]

    def get_mkt_margin_interest_week(self, week_end: date) -> list[JQuantsWeeklyMargin]:
        require_legacy_weekly_balance_date(week_end)
        return _required(read_weekly_margin(self._sqlite_path, week_end), "weekly_margin")

    def get_mkt_all_issues_daily_margin(
        self, balance_date: date
    ) -> list[JQuantsAllIssuesDailyMargin]:
        require_all_issues_daily_balance_date(balance_date)
        return _required(
            read_all_issues_daily_margin(self._sqlite_path, balance_date), "all_issues_daily_margin"
        )

    def get_mkt_margin_alert_range(self, start: date, end: date) -> list[JQuantsMarginAlert]:
        return _required(read_margin_alerts(self._sqlite_path, start, end), "margin_alerts")

    def get_mkt_short_sale_report_range(
        self, start: date, end: date
    ) -> list[JQuantsShortSaleReport]:
        return _required(
            read_short_sale_reports(self._sqlite_path, start, end), "short_sale_reports"
        )


@dataclass(frozen=True, slots=True)
class MasterSnapshotRead:
    """A point-in-time security-master read with its source snapshot status."""

    masters: tuple[SecurityMaster, ...]
    snapshot_date: date | None
    status: str


def read_eq_master_asof(sqlite_path: Path, asof: date) -> MasterSnapshotRead:
    """Read the newest master snapshot at or before ``asof``.

    Snapshot collection starts at the first locally stored date, so cohorts
    before it can never have a point-in-time snapshot. For those dates this
    falls back to the earliest stored snapshot and labels the read
    ``future_snapshot``: the approximation keeps historical cohorts measurable
    for diagnostics, while the calibration authority contract excludes every
    non-``exact_date`` read from production evidence.
    """
    if not sqlite_path.exists():
        return MasterSnapshotRead((), None, "unavailable")
    conn = connect_current(sqlite_path)
    if conn is None:
        return MasterSnapshotRead((), None, "unavailable")
    try:
        conn.execute("BEGIN")
        status = "prior_snapshot"
        snapshot = conn.execute(
            "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date <= ?",
            (asof.isoformat(),),
        ).fetchone()[0]
        if snapshot is None:
            status = "future_snapshot"
            snapshot = conn.execute(
                "SELECT MIN(snapshot_date) FROM jquants_master_snapshots"
            ).fetchone()[0]
        if snapshot is None:
            return MasterSnapshotRead((), None, "unavailable")
        rows = conn.execute(
            "SELECT record_json "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY security_code",
            (str(snapshot),),
        ).fetchall()
    finally:
        conn.close()
    snapshot_date = date.fromisoformat(str(snapshot))
    return MasterSnapshotRead(
        tuple(master_contract.masters_from_payloads([row[0] for row in rows])),
        snapshot_date,
        "exact_date" if snapshot_date == asof else status,
    )


def read_eq_master_exact(sqlite_path: Path, asof: date) -> list[SecurityMaster] | None:
    """Return an exact snapshot only when its canonical coverage also agrees."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    iso = asof.isoformat()
    try:
        conn.execute("BEGIN")
        coverage_rows = conn.execute(
            "SELECT coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = ? AND coverage_key = ?",
            (master_contract.MASTER_SOURCE, master_contract.master_coverage_key(asof)),
        ).fetchall()
        if len(coverage_rows) != 1:
            return None
        coverage_start, coverage_end, record_count, status, error = coverage_rows[0]
        if coverage_start != iso or coverage_end != iso or status != "ok" or error is not None:
            return None
        expected_count = master_contract.master_coverage_count(record_count)
        if expected_count is None:
            return None
        rows = conn.execute(
            "SELECT record_json "
            "FROM jquants_master_snapshots WHERE snapshot_date = ? ORDER BY security_code",
            (iso,),
        ).fetchall()
        if len(rows) != expected_count or expected_count <= 0:
            return None
    finally:
        conn.close()
    masters = master_contract.masters_from_payloads([row[0] for row in rows])
    if (
        sum(security.is_common_stock for security in masters)
        < master_contract.MIN_COMMON_STOCK_MASTER_ROWS
    ):
        return None
    return masters


def fin_summaries_covered(sqlite_path: Path, start: date, end: date) -> bool:
    """Answer whether the cache can serve `[start, end]` without building the rows.

    Deliberately the same predicate `read_fin_summaries` gates on, asked without
    the materialisation: a chunk-skip test and a fetch planner need the boolean,
    not the summaries. Sharing `range_covered` is what keeps them agreeing with
    the reader. A successful empty financial window is readable as an empty list,
    whereas an unexamined or partial window still needs a fetch. Required history
    and usable financial inputs are checked separately by the analysis consumers.
    """
    if not sqlite_path.exists():
        return False
    conn = connect_current(sqlite_path)
    if conn is None:
        return False
    try:
        return range_covered(conn, "jquants_fin_summaries", start, end)
    finally:
        conn.close()


def fin_summaries_readable_from(sqlite_path: Path, asof: date) -> date | None:
    """Where the store's **continuous** history ending at `asof` begins.

    The rows the store holds and the range it may serve are two different facts, and
    the subscription window moves. A filing fetched while the window still reached
    that far back keeps its row after the window passes it, so the oldest row can sit
    outside the coverage. A caller that takes its floor from the oldest row then asks
    for a range `read_fin_summaries` refuses, and a handful of rows at the far edge of
    the history takes every read down.

    Answering only for a continuous history is what keeps that relief from covering a
    real hole. A store that simply does not reach further back has one window and
    gets its floor; a store missing months in the middle has an older window too, and
    gets None so the caller keeps its own floor and the read reports the range it
    cannot serve. Silently starting after a gap would turn an outage into a quietly
    shorter history, which no diagnostic distinguishes from a young store.

    None also covers "no window holds `asof`" -- the same outage seen from the other
    end.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        intervals = covered_intervals(conn, "jquants_fin_summaries")
    finally:
        conn.close()
    for index, (interval_start, interval_end) in enumerate(intervals):
        if interval_start <= asof <= interval_end:
            return interval_start if index == 0 else None
    return None


def count_fin_summaries(sqlite_path: Path, start: date, end: date) -> int:
    """Count stored summary rows in `[start, end]`; 0 when the store cannot be read."""
    if not sqlite_path.exists():
        return 0
    conn = connect_current(sqlite_path)
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row is not None else 0


def read_fin_summaries(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsFinancialSummary] | None:
    """Read covered source documents; project forecasts under the same pure contract."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not range_covered(conn, "jquants_fin_summaries", start, end):
            return None
        return read_financial_documents(conn, start=start, end=end)
    finally:
        conn.close()


def read_financial_documents(
    conn: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    start: date,
    end: date,
    ticker: str | None = None,
) -> list[JQuantsFinancialSummary]:
    """Read stored documents without asserting provider acquisition coverage."""
    cursor = conn.execute(
        "SELECT document_payload FROM jquants_fin_summaries "
        "WHERE disclosed_at BETWEEN ? AND ? AND (? IS NULL OR ticker = ?) "
        "ORDER BY ticker, disclosed_at, disclosure_number",
        (start.isoformat(), end.isoformat(), ticker, ticker),
    )
    summaries: list[JQuantsFinancialSummary] = []
    for (payload,) in cursor.fetchall():
        try:
            record = json.loads(payload)
            if not isinstance(record, dict):
                raise ValueError("financial document must be a provider record")
            summary = normalize_financial_summary(record)
            if summary is None:
                raise ValueError("financial document is not an eligible security")
            summaries.append(summary)
        except (TypeError, ValueError) as exc:
            raise JQuantsProviderError(f"corrupt financial document: {exc}") from exc
    return project_financial_documents(summaries, end)


@dataclass(frozen=True, slots=True)
class MasterSnapshot:
    as_of: date
    securities: dict[str, dict[str, object]]


def read_master_snapshot(path: Path, *, as_of: date) -> MasterSnapshot | None:
    rows = read_rows(
        path,
        "SELECT snapshot_date, record_json "
        "FROM jquants_master_snapshots WHERE snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots "
        "WHERE snapshot_date <= ?) ORDER BY security_code",
        (as_of.isoformat(),),
        connector=connect_read_only,
    )
    if not rows:
        return None
    return MasterSnapshot(
        date.fromisoformat(str(rows[0]["snapshot_date"])),
        {
            security.ticker: {
                "name": security.name,
                "market_segment": security.market_segment,
                "sector_33": security.sector_33,
                "is_common_stock": security.is_common_stock,
                "snapshot_date": str(rows[0]["snapshot_date"]),
            }
            for security in masters_from_payloads([row["record_json"] for row in rows])
        },
    )


def read_market_calendar(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    """Return market calendar days for `[start, end]`, or `None` if the
    cache cannot serve the range.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        return _read_calendar(conn, start, end)
    finally:
        conn.close()


def _read_calendar(
    conn: sqlite3.Connection, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    if not range_covered(conn, "jquants_market_calendar", start, end):
        return None
    rows = conn.execute(
        "SELECT day, holiday_division IN ('1', '2') FROM jquants_market_calendar "
        "WHERE day BETWEEN ? AND ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        JQuantsMarketCalendarDay(day=date.fromisoformat(day), is_business_day=bool(flag))
        for day, flag in rows
    ]
