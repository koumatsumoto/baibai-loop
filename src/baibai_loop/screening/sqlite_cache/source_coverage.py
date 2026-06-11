"""source_coverage bookkeeping and date-range row maintenance."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

_DELETE_DATE_RANGE_SQL = {
    ("jquants_daily_bars", "traded_at"): (
        "DELETE FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?"
    ),
    ("jquants_fin_summaries", "disclosed_at"): (
        "DELETE FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?"
    ),
    ("jquants_market_calendar", "day"): (
        "DELETE FROM jquants_market_calendar WHERE day BETWEEN ? AND ?"
    ),
}
_COUNT_DATE_RANGE_SQL = {
    ("jquants_daily_bars", "traded_at"): (
        "SELECT COUNT(*) FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?"
    ),
    ("jquants_fin_summaries", "disclosed_at"): (
        "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?"
    ),
    ("jquants_market_calendar", "day"): (
        "SELECT COUNT(*) FROM jquants_market_calendar WHERE day BETWEEN ? AND ?"
    ),
    ("edinet_documents", "doc_date"): (
        "SELECT COUNT(*) FROM edinet_documents WHERE doc_date BETWEEN ? AND ?"
    ),
    ("edinet_metrics", "asof_date"): (
        "SELECT COUNT(*) FROM edinet_metrics WHERE asof_date BETWEEN ? AND ?"
    ),
    ("jpx_regulation_flags", "asof_date"): (
        "SELECT COUNT(*) FROM jpx_regulation_flags WHERE asof_date BETWEEN ? AND ?"
    ),
}
_COUNT_TABLE_SQL = {
    "jquants_master_snapshots": "SELECT COUNT(*) FROM jquants_master_snapshots",
    "jquants_earnings_calendar": "SELECT COUNT(*) FROM jquants_earnings_calendar",
}


def _delete_source_coverage(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM source_coverage WHERE source = ?", (source,))


def _delete_overlapping_source_coverage(
    conn: sqlite3.Connection,
    source: str,
    start: date,
    end: date,
) -> None:
    conn.execute(
        "DELETE FROM source_coverage WHERE source = ? "
        "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
        "AND coverage_start <= ? AND coverage_end >= ?",
        (source, end.isoformat(), start.isoformat()),
    )


def _record_range_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    operation: str,
    table: str,
    date_column: str,
    requested_start: date,
    requested_end: date,
    status: str,
    error: str | None,
) -> int:
    """Record coverage for a freshly fetched ``[requested_start, requested_end]`` range.

    The fetched range is merged with any overlapping or adjacent ``ok`` coverage
    window into one contiguous window. Range fetches chunk the request and anchor
    chunk boundaries at ``asof - N days``, so a later bootstrap shifts those
    boundaries; a re-fetch whose chunk only partially overlaps an existing window
    must not delete-and-shrink that window and orphan the rest of it (the bug that
    left ``_range_covered`` gaps even though the rows were present). ``record_count``
    is recomputed over the merged window from the rows themselves -- the DB is the
    source of truth, and the merged window keeps rows preserved outside the
    fetched range.

    A non-``ok`` fetch is recorded for its own range without merging, so a fetch
    quality problem stays visible instead of being absorbed into an ``ok`` window.
    """
    merged_start, merged_end = requested_start, requested_end
    if status == "ok":
        # Treat windows touching within one day as adjacent so the merged window is
        # contiguous under `_range_covered` (which tolerates a <=1 day gap).
        window_low = (requested_start - timedelta(days=1)).isoformat()
        window_high = (requested_end + timedelta(days=1)).isoformat()
        # Same bind params and WHERE clause for the select-then-delete pair. The
        # clause is written out literally in each query (not interpolated) so the
        # SQL stays a static string and never builds a query from variables.
        overlap_params = (source, window_high, window_low)
        for coverage_start, coverage_end in conn.execute(
            "SELECT coverage_start, coverage_end FROM source_coverage "
            "WHERE source = ? AND status = 'ok' "
            "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start <= ? AND coverage_end >= ?",
            overlap_params,
        ).fetchall():
            try:
                existing_start = date.fromisoformat(coverage_start)
                existing_end = date.fromisoformat(coverage_end)
            except ValueError:
                continue
            merged_start = min(merged_start, existing_start)
            merged_end = max(merged_end, existing_end)
        conn.execute(
            "DELETE FROM source_coverage "
            "WHERE source = ? AND status = 'ok' "
            "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start <= ? AND coverage_end >= ?",
            overlap_params,
        )
    else:
        _delete_overlapping_source_coverage(conn, source, requested_start, requested_end)
    persisted_count = _date_range_row_count(conn, table, date_column, merged_start, merged_end)
    _record_source_coverage(
        conn,
        source=source,
        coverage_key=_range_coverage_key(operation, merged_start, merged_end),
        coverage_start=merged_start.isoformat(),
        coverage_end=merged_end.isoformat(),
        record_count=persisted_count,
        status=status,
        error=error,
    )
    return persisted_count


def _delete_date_range(
    conn: sqlite3.Connection,
    table: str,
    date_column: str,
    start: date,
    end: date,
) -> None:
    conn.execute(
        _DELETE_DATE_RANGE_SQL[(table, date_column)],
        (start.isoformat(), end.isoformat()),
    )


def _date_range_row_count(
    conn: sqlite3.Connection,
    table: str,
    date_column: str,
    start: date,
    end: date,
) -> int:
    row = conn.execute(
        _COUNT_DATE_RANGE_SQL[(table, date_column)],
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return int(row[0] or 0)


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(_COUNT_TABLE_SQL[table]).fetchone()
    return int(row[0] or 0)


def _record_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    coverage_start: str | None,
    coverage_end: str | None,
    record_count: int,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
    replace: bool = True,
    **_: Any,
) -> None:
    """Record the minimal provider coverage needed for preflight checks."""
    conflict_action = "REPLACE" if replace else "IGNORE"
    conn.execute(
        f"""
        INSERT OR {conflict_action} INTO source_coverage(
          source, coverage_key, coverage_start, coverage_end,
          fetched_at_utc, record_count, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            coverage_key,
            coverage_start,
            coverage_end,
            fetched_at_utc or datetime.now(UTC).isoformat(),
            record_count,
            status,
            error,
        ),
    )


def _range_coverage_key(operation: str, start: date, end: date) -> str:
    return f"{operation}:{start.isoformat()}..{end.isoformat()}"
