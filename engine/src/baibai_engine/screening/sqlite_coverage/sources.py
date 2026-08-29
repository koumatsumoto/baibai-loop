"""source_coverage bookkeeping checks and table consistency queries."""

from __future__ import annotations

import sqlite3
from datetime import date

from .shared import CacheCoverageIssue

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
    "jpx_earnings_calendar": "SELECT COUNT(*) FROM jpx_earnings_calendar",
    "jquants_market_calendar": "SELECT COUNT(*) FROM jquants_market_calendar",
    "jpx_regulation_flags": "SELECT COUNT(*) FROM jpx_regulation_flags",
}
_WINDOW_COUNT_SOURCES = {
    "jquants_daily_bars": ("jquants_daily_bars", "traded_at"),
    "jquants_fin_summaries": ("jquants_fin_summaries", "disclosed_at"),
}
_SINGLE_SNAPSHOT_SOURCES = frozenset({"jpx_earnings_calendar"})


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
    if require_rows and imported_count <= 0:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="source_coverage records zero imported rows for required source",
            )
        )
        return
    if enforce_record_count:
        window_mismatches = _source_window_count_mismatches(conn, source)
        if window_mismatches:
            for coverage_key, window_count, expected_count in window_mismatches:
                issues.append(
                    CacheCoverageIssue(
                        source=source,
                        requirement=str(coverage_key),
                        reason=(
                            f"{table} row count in source_coverage window ({window_count}) "
                            f"does not match source_coverage record_count ({expected_count}); "
                            "repair SQLite"
                        ),
                    )
                )
    if (
        enforce_record_count
        and source not in _WINDOW_COUNT_SOURCES
        and table_count != imported_count
    ):
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    f"{table} row count ({table_count}) does not match source_coverage "
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


def _source_window_count_mismatches(
    conn: sqlite3.Connection, source: str
) -> tuple[tuple[str, int, int], ...]:
    table_date_column = _WINDOW_COUNT_SOURCES.get(source)
    if table_date_column is None:
        return ()
    table, date_column = table_date_column
    rows = conn.execute(
        "SELECT coverage_key, coverage_start, coverage_end, record_count "
        "FROM source_coverage WHERE source = ? AND status = 'ok' "
        "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL",
        (source,),
    ).fetchall()
    mismatches: list[tuple[str, int, int]] = []
    for coverage_key, coverage_start, coverage_end, record_count in rows:
        count_row = conn.execute(
            _REQUIRED_DATE_ROWS_SQL[(table, date_column)],
            (str(coverage_start), str(coverage_end)),
        ).fetchone()
        window_count = int(count_row[0] or 0)
        expected_count = int(record_count or 0)
        if window_count != expected_count:
            mismatches.append((str(coverage_key), window_count, expected_count))
    return tuple(mismatches)


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


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(_TABLE_COUNT_SQL[table]).fetchone()
    return int(row[0] or 0)
