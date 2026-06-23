"""Coverage verification entry point and schema-level checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.market.sqlite import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    _daily_bars_covered_by_data,
    _range_covered,
    validate_current_schema,
)
from baibai_loop.screening.sqlite_reader import (
    _has_any_import,
    _minmax_covered,
    _minmax_horizon_covered,
)

from .edinet import _append_edinet_metrics_coverage_issues
from .jpx import _append_jpx_freshness_issues, _jpx_source_names
from .jquants import (
    _append_asof_bar_density_issue,
    _append_daily_history_density_issue,
    _append_fin_summary_density_issue,
    _append_master_common_stock_issue,
    _append_recent_bar_density_issue,
)
from .shared import CacheCoverageIssue
from .sources import (
    _append_required_date_rows_issue,
    _append_source_coverage_quality_issues,
    _append_table_consistency_issues,
    _has_source_coverage,
    _source_coverage_covers_date,
)


def verify_screening_sqlite_coverage(
    sqlite_path: Path,
    asof_date: date,
    *,
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
    jpx_source_names: set[str] = set()
    try:
        conn = _connect_readonly(sqlite_path)
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
            schema_issue = _schema_version_issue(conn, sqlite_path)
            if schema_issue is not None:
                return (schema_issue,)
            schema_shape_issue = _schema_shape_issue(conn, sqlite_path)
            if schema_shape_issue is not None:
                return (schema_shape_issue,)
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
                    _append_master_common_stock_issue(conn, issues, asof_date=asof_date)
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
                _append_master_common_stock_issue(conn, issues, asof_date=asof_date)
            # daily_bars completeness is derived from the actual rows. The
            # quality / density checks below run regardless of the coverage gate
            # so a grossly incomplete cache reports both the missing window and
            # the specific density problem, rather than only the first failure.
            bars_window_covered = _daily_bars_covered_by_data(conn, bars_start, asof_date)
            _append_source_coverage_quality_issues(
                conn,
                issues,
                source="jquants_daily_bars",
                start=bars_start,
                end=asof_date,
            )
            if not bars_window_covered:
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_daily_bars",
                        requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                        reason="daily bars request window is not fully covered in SQLite",
                    )
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
            _append_daily_history_density_issue(
                conn,
                issues,
                start=bars_start,
                end=asof_date,
            )
            _append_table_consistency_issues(
                conn,
                issues,
                source="jquants_daily_bars",
                table="jquants_daily_bars",
                requirement=f"{bars_start.isoformat()}..{asof_date.isoformat()}",
                require_rows=True,
                enforce_record_count=True,
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
                    enforce_record_count=True,
                )
                _append_fin_summary_density_issue(
                    conn,
                    issues,
                    start=fin_start,
                    end=asof_date,
                )
            earnings_end = asof_date + timedelta(days=90)
            earnings_exact = _minmax_covered(
                conn, "jquants_earnings_calendar", asof_date, earnings_end
            )
            # For historical replay (allow_stale_jpx), fall back to end-only coverage:
            # the earnings calendar is a live-only endpoint so past asof dates can never
            # satisfy coverage_start <= asof_date. Accept any fetch whose horizon covers
            # the 90-day window, even if that fetch post-dates asof.
            earnings_covered = earnings_exact or (
                allow_stale_jpx
                and _minmax_horizon_covered(conn, "jquants_earnings_calendar", earnings_end)
            )
            if not earnings_covered:
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
                jpx_source_names = _jpx_source_names(conn, asof_date)
            _append_source_coverage_quality_issues(
                conn,
                issues,
                source="edinet_metrics",
                start=asof_date,
                end=asof_date,
            )
            if not _source_coverage_covers_date(conn, "edinet_metrics", asof_date):
                issues.append(
                    CacheCoverageIssue(
                        source="edinet_metrics",
                        requirement=asof_date.isoformat(),
                        reason="EDINET metrics source coverage is not covered in SQLite",
                    )
                )
            _append_edinet_metrics_coverage_issues(conn, issues, asof_date=asof_date)
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
        missing_jpx_sources = tuple(
            source for source in sorted(required_jpx_sources) if source not in jpx_source_names
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
    return tuple(issues)


def _connect_readonly(sqlite_path: Path) -> sqlite3.Connection:
    uri = f"file:{sqlite_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _schema_version_issue(conn: sqlite3.Connection, sqlite_path: Path) -> CacheCoverageIssue | None:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        return CacheCoverageIssue(
            source="sqlite",
            requirement=sqlite_path.as_posix(),
            reason=f"SQLite user_version is unavailable: {type(exc).__name__}: {exc}",
        )
    found_version = int(row[0] or 0) if row is not None else 0
    if found_version != SQLITE_SCHEMA_VERSION:
        found = "<missing>" if row is None else str(row[0])
        return CacheCoverageIssue(
            source="sqlite",
            requirement=sqlite_path.as_posix(),
            reason=f"SQLite user_version is {found}; expected {SQLITE_SCHEMA_VERSION}",
        )
    return None


def _schema_shape_issue(conn: sqlite3.Connection, sqlite_path: Path) -> CacheCoverageIssue | None:
    try:
        validate_current_schema(conn)
    except SQLiteSchemaError as exc:
        return CacheCoverageIssue(
            source="sqlite",
            requirement=sqlite_path.as_posix(),
            reason=str(exc),
        )
    except sqlite3.Error as exc:
        return CacheCoverageIssue(
            source="sqlite",
            requirement=sqlite_path.as_posix(),
            reason=f"SQLite schema validation failed: {type(exc).__name__}: {exc}",
        )
    return None
