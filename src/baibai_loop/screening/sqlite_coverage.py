from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .date_utils import weekday_distance
from .render import JST
from .sqlite_cache import ensure_sqlite_schema
from .sqlite_reader import (
    _has_any_import,
    _minmax_covered,
    _range_covered,
    read_edinet_metrics,
    read_jpx_regulations,
)

_REQUIRED_DATE_ROWS_SQL = {
    ("jquants_daily_bars", "traded_at"): (
        "SELECT COUNT(*) FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?"
    ),
    ("jquants_fin_summaries", "disclosed_at"): (
        "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?"
    ),
    ("jquants_market_calendar", "day"): (
        "SELECT COUNT(*) FROM jquants_market_calendar WHERE day BETWEEN ? AND ?"
    ),
}

_TABLE_COUNT_SQL = {
    "jquants_daily_bars": "SELECT COUNT(*) FROM jquants_daily_bars",
    "jquants_fin_summaries": "SELECT COUNT(*) FROM jquants_fin_summaries",
    "jquants_master_snapshots": "SELECT COUNT(*) FROM jquants_master_snapshots",
    "jquants_earnings_calendar": "SELECT COUNT(*) FROM jquants_earnings_calendar",
    "jquants_market_calendar": "SELECT COUNT(*) FROM jquants_market_calendar",
    "jpx_regulation_flags": "SELECT COUNT(*) FROM jpx_regulation_flags",
}
_SINGLE_SNAPSHOT_SOURCES = frozenset({"jquants_master_snapshots", "jquants_earnings_calendar"})
_JPX_MAX_FETCH_AGE_BUSINESS_DAYS = 7


@dataclass(frozen=True, slots=True)
class CacheCoverageIssue:
    source: str
    requirement: str
    reason: str


def verify_screening_sqlite_coverage(
    sqlite_path: Path,
    asof_date: date,
    *,
    require_edinet_metrics: bool = True,
    required_jpx_sources: Iterable[str] = (),
    allow_stale_jpx: bool = False,
) -> tuple[CacheCoverageIssue, ...]:
    """Return cache coverage gaps that must block `screening run`.

    This verifier is local-only. It checks the SQLite cache that `run` will
    read, and it never fetches raw JSON or provider APIs. If a required source
    is incomplete, callers must stop and refresh the cache before running the
    screen.
    """
    if not sqlite_path.exists():
        return (
            CacheCoverageIssue(
                source="sqlite",
                requirement=sqlite_path.as_posix(),
                reason="SQLite cache file is missing",
            ),
        )

    issues: list[CacheCoverageIssue] = []
    bars_start = asof_date - timedelta(days=1200)
    fin_start = asof_date - timedelta(days=730)
    jpx_source_coverage_covers_asof = False
    try:
        ensure_sqlite_schema(sqlite_path)
        conn = sqlite3.connect(sqlite_path)
        try:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity_row is None or integrity_row[0] != "ok":
                reason = integrity_row[0] if integrity_row else "<no result>"
                return (
                    CacheCoverageIssue(
                        source="sqlite",
                        requirement=sqlite_path.as_posix(),
                        reason=f"SQLite integrity_check failed: {reason}",
                    ),
                )
            if not _has_any_import(conn, "jquants_master_snapshots"):
                _append_source_coverage_quality_issues(
                    conn, issues, source="jquants_master_snapshots"
                )
                if _has_source_coverage(conn, "jquants_master_snapshots"):
                    _append_table_consistency_issues(
                        conn,
                        issues,
                        source="jquants_master_snapshots",
                        table="jquants_master_snapshots",
                        requirement="latest imported master snapshot",
                        require_rows=True,
                        enforce_record_count=True,
                    )
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_master_snapshots",
                        requirement="latest imported master snapshot",
                        reason="no imported master snapshot in SQLite",
                    )
                )
            else:
                _append_source_coverage_quality_issues(
                    conn, issues, source="jquants_master_snapshots"
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_master_snapshots",
                    table="jquants_master_snapshots",
                    requirement="latest imported master snapshot",
                    require_rows=True,
                    enforce_record_count=True,
                )
            if not _range_covered(conn, "jquants_daily_bars", bars_start, asof_date):
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_daily_bars",
                    start=bars_start,
                    end=asof_date,
                )
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_daily_bars",
                        requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                        reason="daily bars request window is not fully covered in SQLite",
                    )
                )
            else:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_daily_bars",
                    start=bars_start,
                    end=asof_date,
                )
                _append_required_date_rows_issue(
                    conn,
                    issues,
                    source="jquants_daily_bars",
                    table="jquants_daily_bars",
                    date_column="traded_at",
                    start=bars_start,
                    end=asof_date,
                    requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                )
                _append_asof_bar_density_issue(conn, issues, asof_date=asof_date)
                _append_recent_bar_density_issue(conn, issues, asof_date=asof_date)
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_daily_bars",
                    table="jquants_daily_bars",
                    requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                    require_rows=True,
                    enforce_record_count=_raw_import_windows_are_non_overlapping(
                        conn, "jquants_daily_bars"
                    ),
                )
            if not _range_covered(conn, "jquants_fin_summaries", fin_start, asof_date):
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_fin_summaries",
                    start=fin_start,
                    end=asof_date,
                )
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_fin_summaries",
                        requirement=f"{fin_start.isoformat()}..{asof_date.isoformat()}",
                        reason="financial summary request window is not fully covered in SQLite",
                    )
                )
            else:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_fin_summaries",
                    start=fin_start,
                    end=asof_date,
                )
                _append_required_date_rows_issue(
                    conn,
                    issues,
                    source="jquants_fin_summaries",
                    table="jquants_fin_summaries",
                    date_column="disclosed_at",
                    start=fin_start,
                    end=asof_date,
                    requirement=f"{fin_start.isoformat()}..{asof_date.isoformat()}",
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_fin_summaries",
                    table="jquants_fin_summaries",
                    requirement=f"{fin_start.isoformat()}..{asof_date.isoformat()}",
                    require_rows=True,
                    enforce_record_count=_raw_import_windows_are_non_overlapping(
                        conn, "jquants_fin_summaries"
                    ),
                )
            earnings_end = asof_date + timedelta(days=90)
            if not _minmax_covered(conn, "jquants_earnings_calendar", asof_date, earnings_end):
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_earnings_calendar",
                    start=asof_date,
                    end=earnings_end,
                )
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_earnings_calendar",
                        requirement=f"{asof_date.isoformat()}..{earnings_end.isoformat()}",
                        reason="earnings calendar horizon is not covered in SQLite",
                    )
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_earnings_calendar",
                    table="jquants_earnings_calendar",
                    requirement=f"{asof_date.isoformat()}..{earnings_end.isoformat()}",
                    require_rows=True,
                    enforce_record_count=True,
                )
            else:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_earnings_calendar",
                    start=asof_date,
                    end=earnings_end,
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_earnings_calendar",
                    table="jquants_earnings_calendar",
                    requirement=f"{asof_date.isoformat()}..{earnings_end.isoformat()}",
                    require_rows=True,
                    enforce_record_count=True,
                )
            if not _range_covered(conn, "jquants_market_calendar", asof_date, asof_date):
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_market_calendar",
                    start=asof_date,
                    end=asof_date,
                )
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_market_calendar",
                        requirement=asof_date.isoformat(),
                        reason="market calendar asof date is not covered in SQLite",
                    )
                )
            else:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jquants_market_calendar",
                    start=asof_date,
                    end=asof_date,
                )
                _append_required_date_rows_issue(
                    conn,
                    issues,
                    source="jquants_market_calendar",
                    table="jquants_market_calendar",
                    date_column="day",
                    start=asof_date,
                    end=asof_date,
                    requirement=asof_date.isoformat(),
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_market_calendar",
                    table="jquants_market_calendar",
                    requirement=asof_date.isoformat(),
                    require_rows=True,
                    enforce_record_count=False,
                )
            jpx_source_coverage_covers_asof = _source_coverage_covers_date(
                conn, "jpx_regulation_flags", asof_date
            )
            if jpx_source_coverage_covers_asof:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="jpx_regulation_flags",
                    start=asof_date,
                    end=asof_date,
                )
                _append_jpx_freshness_issues(
                    conn,
                    issues,
                    asof_date=asof_date,
                    allow_stale_jpx=allow_stale_jpx,
                )
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jpx_regulation_flags",
                    table="jpx_regulation_flags",
                    requirement=asof_date.isoformat(),
                    require_rows=False,
                    enforce_record_count=True,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return (
            CacheCoverageIssue(
                source="sqlite",
                requirement=sqlite_path.as_posix(),
                reason=f"SQLite coverage query failed: {type(exc).__name__}: {exc}",
            ),
        )

    if not jpx_source_coverage_covers_asof:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation source coverage is not covered in SQLite",
            )
        )
    else:
        snapshot = read_jpx_regulations(sqlite_path, asof_date)
        source_names = set(snapshot.source_names if snapshot is not None else ())
        missing_jpx_sources = tuple(
            source for source in sorted(required_jpx_sources) if source not in source_names
        )
        if missing_jpx_sources:
            issues.append(
                CacheCoverageIssue(
                    source="jpx_regulation_flags",
                    requirement=asof_date.isoformat(),
                    reason="missing required JPX regulation sources: "
                    + ", ".join(missing_jpx_sources),
                )
            )
    if require_edinet_metrics:
        try:
            conn = sqlite3.connect(sqlite_path)
            try:
                _append_source_coverage_quality_issues(
                    conn,
                    issues,
                    source="edinet_metrics",
                    start=asof_date,
                    end=asof_date,
                )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            issues.append(
                CacheCoverageIssue(
                    source="edinet_metrics",
                    requirement=asof_date.isoformat(),
                    reason=f"EDINET metrics quality query failed: {type(exc).__name__}: {exc}",
                )
            )
        try:
            edinet_metrics = read_edinet_metrics(sqlite_path, asof_date)
        except Exception as exc:
            issues.append(
                CacheCoverageIssue(
                    source="edinet_metrics",
                    requirement=asof_date.isoformat(),
                    reason=f"EDINET metrics coverage query failed: {type(exc).__name__}: {exc}",
                )
            )
        else:
            if not edinet_metrics:
                issues.append(
                    CacheCoverageIssue(
                        source="edinet_metrics",
                        requirement=asof_date.isoformat(),
                        reason="EDINET metrics are required but not covered in SQLite",
                    )
                )
    return tuple(issues)


def _append_required_date_rows_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    source: str,
    table: str,
    date_column: str,
    start: date,
    end: date,
    requirement: str,
) -> None:
    row = conn.execute(
        _REQUIRED_DATE_ROWS_SQL[(table, date_column)],
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    if int(row[0] or 0) == 0:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=f"{table} has no rows in the required date window",
            )
        )


def _append_asof_bar_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    asof_iso = asof_date.isoformat()
    expected = _latest_common_stock_count(conn)
    if expected <= 0:
        return
    actual_row = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM jquants_daily_bars "
        "WHERE traded_at = ? AND close IS NOT NULL",
        (asof_iso,),
    ).fetchone()
    actual = int(actual_row[0] or 0)
    minimum = max(1, expected // 2)
    if actual < minimum:
        issues.append(
            CacheCoverageIssue(
                source="jquants_daily_bars",
                requirement=asof_iso,
                reason=(
                    f"daily bars usable ticker count on asof ({actual}) is too small "
                    f"relative to common-stock master rows ({expected}); repair SQLite"
                ),
            )
        )


def _latest_common_stock_count(conn: sqlite3.Connection) -> int:
    master_row = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM jquants_master_snapshots "
        "WHERE is_common_stock = 1 AND snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date != 'unknown'"
        ")"
    ).fetchone()
    return int(master_row[0] or 0)


def _append_recent_bar_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    expected = _latest_common_stock_count(conn)
    if expected <= 0:
        return
    recent_start = asof_date - timedelta(days=30)
    rows = conn.execute(
        "SELECT traded_at, COUNT(DISTINCT ticker) FROM jquants_daily_bars "
        "WHERE traded_at BETWEEN ? AND ? AND close IS NOT NULL "
        "GROUP BY traded_at ORDER BY traded_at DESC LIMIT 5",
        (recent_start.isoformat(), asof_date.isoformat()),
    ).fetchall()
    minimum = max(1, expected // 2)
    if len(rows) < 5:
        issues.append(
            CacheCoverageIssue(
                source="jquants_daily_bars",
                requirement=f"{recent_start.isoformat()}..{asof_date.isoformat()}",
                reason=(
                    "daily bars have fewer than 5 usable traded dates in the recent "
                    "30-day window; repair SQLite"
                ),
            )
        )
        return
    weak_dates = [str(traded_at) for traded_at, count in rows if int(count or 0) < minimum]
    if weak_dates:
        issues.append(
            CacheCoverageIssue(
                source="jquants_daily_bars",
                requirement=f"{recent_start.isoformat()}..{asof_date.isoformat()}",
                reason=(
                    "daily bars recent usable ticker count is too small on: "
                    + ", ".join(weak_dates)
                ),
            )
        )


def _append_source_coverage_quality_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    source: str,
    start: date | None = None,
    end: date | None = None,
) -> None:
    if start is not None and end is not None:
        rows = conn.execute(
            "SELECT coverage_key, status, error FROM source_coverage WHERE source = ? "
            "AND (coverage_start IS NULL OR coverage_end IS NULL "
            "OR NOT (coverage_end < ? OR coverage_start > ?))",
            (source, start.isoformat(), end.isoformat()),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT coverage_key, status, error FROM source_coverage WHERE source = ?",
            (source,),
        ).fetchall()
    for coverage_key, status, error in rows:
        if status != "ok":
            suffix = f": {error}" if error else ""
            issues.append(
                CacheCoverageIssue(
                    source=source,
                    requirement=str(coverage_key),
                    reason=f"source_coverage status is not ok: {status}{suffix}",
                )
            )


def _append_jpx_freshness_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
    allow_stale_jpx: bool,
) -> None:
    if allow_stale_jpx:
        return
    rows = conn.execute(
        "SELECT source_name, fetched_at_utc FROM jpx_regulation_sources WHERE asof_date = ?",
        (asof_date.isoformat(),),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT DISTINCT flag, fetched_at_utc FROM jpx_regulation_flags "
            "WHERE asof_date = ? AND fetched_at_utc IS NOT NULL",
            (asof_date.isoformat(),),
        ).fetchall()
    if not rows:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has no fetched_at_utc; refetch SQLite",
            )
        )
        return
    stale_values: list[str] = []
    invalid_values: list[str] = []
    missing_values: list[str] = []
    for source_name, fetched_at_text in rows:
        if fetched_at_text is None:
            missing_values.append(str(source_name))
            continue
        try:
            fetched_at = datetime.fromisoformat(str(fetched_at_text).replace("Z", "+00:00"))
        except ValueError:
            invalid_values.append(str(fetched_at_text))
            continue
        fetched_date = fetched_at.astimezone(JST).date()
        if weekday_distance(asof_date, fetched_date) > _JPX_MAX_FETCH_AGE_BUSINESS_DAYS:
            stale_values.append(str(fetched_at_text))
    if missing_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has source rows without fetched_at_utc: "
                + ", ".join(sorted(missing_values)),
            )
        )
    if invalid_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has invalid fetched_at_utc: "
                + ", ".join(sorted(invalid_values)),
            )
        )
    if stale_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason=(
                    "JPX regulation snapshot fetched_at_utc is stale for asof; "
                    f"refetch SQLite or pass --allow-stale-jpx: {', '.join(sorted(stale_values))}"
                ),
            )
        )


def _append_table_consistency_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    source: str,
    table: str,
    requirement: str,
    require_rows: bool,
    enforce_record_count: bool,
) -> None:
    imported_count = _source_coverage_record_count(conn, source)
    table_count = _table_row_count(conn, table)
    recorded_count = _recorded_table_row_count(conn, table)
    if recorded_count is None:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=f"cache_metadata table_count for {table} is missing; rebuild SQLite",
            )
        )
        return
    if table_count != recorded_count:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    f"{table} row count ({table_count}) differs from recorded cache_metadata "
                    f"table_count ({recorded_count}); repair SQLite"
                ),
            )
        )
        return
    if require_rows and imported_count <= 0:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="source_coverage records zero imported rows for required source",
            )
        )
        return
    if enforce_record_count and table_count < imported_count:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    f"{table} row count ({table_count}) is smaller than source_coverage "
                    f"record_count ({imported_count}); repair SQLite"
                ),
            )
        )
        return
    if require_rows and table_count <= 0:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=f"{table} has no usable rows for required source",
            )
        )
        return


def _source_coverage_record_count(conn: sqlite3.Connection, source: str) -> int:
    if source in _SINGLE_SNAPSHOT_SOURCES:
        row = conn.execute(
            "SELECT COALESCE(MAX(record_count), 0) FROM source_coverage "
            "WHERE source = ? AND status = 'ok'",
            (source,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(record_count), 0) FROM source_coverage "
            "WHERE source = ? AND status = 'ok'",
            (source,),
        ).fetchone()
    return int(row[0] or 0)


def _source_coverage_covers_date(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM source_coverage "
        "WHERE source = ? AND coverage_start <= ? AND coverage_end >= ? "
        "AND status = 'ok' LIMIT 1",
        (source, on_date.isoformat(), on_date.isoformat()),
    ).fetchone()
    return row is not None


def _has_source_coverage(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM source_coverage WHERE source = ? LIMIT 1",
        (source,),
    ).fetchone()
    return row is not None


def _recorded_table_row_count(conn: sqlite3.Connection, table: str) -> int | None:
    row = conn.execute(
        "SELECT value FROM cache_metadata WHERE key = ?",
        (f"table_count.{table}",),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _raw_import_windows_are_non_overlapping(conn: sqlite3.Connection, source: str) -> bool:
    rows = conn.execute(
        "SELECT coverage_start, coverage_end FROM source_coverage "
        "WHERE source = ? AND status = 'ok'",
        (source,),
    ).fetchall()
    intervals: list[tuple[date, date]] = []
    for start_text, end_text in rows:
        if not start_text or not end_text:
            return False
        try:
            intervals.append((date.fromisoformat(start_text), date.fromisoformat(end_text)))
        except ValueError:
            return False
    intervals.sort()
    previous_end: date | None = None
    for start, end in intervals:
        if previous_end is not None and start <= previous_end:
            return False
        previous_end = end
    return True


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(_TABLE_COUNT_SQL[table]).fetchone()
    return int(row[0] or 0)
