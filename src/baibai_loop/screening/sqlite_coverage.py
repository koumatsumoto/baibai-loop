from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .sqlite_reader import (
    _has_any_import,
    _parse_chunk_window,
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


@dataclass(frozen=True, slots=True)
class CacheCoverageIssue:
    source: str
    requirement: str
    reason: str


def verify_screening_sqlite_coverage(
    sqlite_path: Path,
    asof_date: date,
    *,
    require_edinet_metrics: bool = False,
    required_jpx_sources: Iterable[str] = (),
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
    jpx_raw_import_covers_asof = False
    try:
        conn = sqlite3.connect(sqlite_path)
        try:
            if not _has_any_import(conn, "jquants_master_snapshots"):
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_master_snapshots",
                        requirement="latest imported master snapshot",
                        reason="no imported master snapshot in SQLite",
                    )
                )
            else:
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
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_daily_bars",
                        requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                        reason="daily bars request window is not fully covered in SQLite",
                    )
                )
            else:
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
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_fin_summaries",
                        requirement=f"{fin_start.isoformat()}..{asof_date.isoformat()}",
                        reason="financial summary request window is not fully covered in SQLite",
                    )
                )
            else:
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
            if not _has_any_import(conn, "jquants_earnings_calendar"):
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_earnings_calendar",
                        requirement="latest imported earnings calendar payload",
                        reason="earnings calendar payload is not imported in SQLite",
                    )
                )
            else:
                _append_table_consistency_issues(
                    conn,
                    issues,
                    source="jquants_earnings_calendar",
                    table="jquants_earnings_calendar",
                    requirement="latest imported earnings calendar payload",
                    require_rows=False,
                    enforce_record_count=True,
                )
            if not _range_covered(conn, "jquants_market_calendar", asof_date, asof_date):
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_market_calendar",
                        requirement=asof_date.isoformat(),
                        reason="market calendar asof date is not covered in SQLite",
                    )
                )
            else:
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
            jpx_raw_import_covers_asof = _raw_import_covers_date(
                conn, "jpx_regulation_flags", asof_date
            )
            if jpx_raw_import_covers_asof:
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

    if not jpx_raw_import_covers_asof:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation raw import is not covered in SQLite",
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
    imported_count = _raw_import_record_count(conn, source)
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
                    f"table_count ({recorded_count}); rebuild SQLite from raw JSON"
                ),
            )
        )
        return
    if require_rows and imported_count <= 0:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="raw_imports records zero imported rows for required source",
            )
        )
        return
    if enforce_record_count and table_count < imported_count:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    f"{table} row count ({table_count}) is smaller than raw_imports "
                    f"record_count ({imported_count}); rebuild SQLite from raw JSON"
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


def _raw_import_record_count(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(record_count), 0) FROM raw_imports WHERE source = ?",
        (source,),
    ).fetchone()
    return int(row[0] or 0)


def _raw_import_covers_date(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_imports WHERE source = ? AND min_date <= ? AND max_date >= ? LIMIT 1",
        (source, on_date.isoformat(), on_date.isoformat()),
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
        "SELECT path, min_date, max_date FROM raw_imports WHERE source = ?",
        (source,),
    ).fetchall()
    intervals: list[tuple[date, date]] = []
    for path_text, min_date, max_date in rows:
        window = _parse_chunk_window(path_text)
        if window is not None:
            start_text, end_text = window
        elif min_date and max_date:
            start_text, end_text = str(min_date), str(max_date)
        else:
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
