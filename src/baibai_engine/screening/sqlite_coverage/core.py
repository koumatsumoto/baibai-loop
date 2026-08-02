"""Coverage verification entry point and schema-level checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.sqlite import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    daily_bars_covered_by_data,
    range_covered,
    validate_current_schema,
)

from ..metrics import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)
from ..sqlite_cache.jquants import WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key
from .edinet import _append_edinet_metrics_coverage_issues
from .jpx import (
    _append_jpx_earnings_calendar_issues,
    _append_jpx_freshness_issues,
    _jpx_source_names,
)
from .jquants import (
    _append_asof_bar_density_issue,
    _append_daily_history_density_issue,
    _append_fin_summary_density_issue,
    _append_master_snapshot_issues,
    _append_recent_bar_density_issue,
)
from .shared import CacheCoverageIssue
from .sources import (
    _append_required_date_rows_issue,
    _append_source_coverage_quality_issues,
    _append_table_consistency_issues,
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
    bars_start = asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS)
    fin_start = asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS)
    normalized_start = asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS)
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
            _append_master_snapshot_issues(conn, issues, asof_date=asof_date)
            # daily_bars completeness is derived from the actual rows. The
            # quality / density checks below run regardless of the coverage gate
            # so a grossly incomplete cache reports both the missing window and
            # the specific density problem, rather than only the first failure.
            bars_window_covered = daily_bars_covered_by_data(conn, bars_start, asof_date)
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
                # The split basis for normalized_per_3fy reaches beyond the
                # ordinary metric window. Date continuity alone cannot distinguish
                # a complete cross-section from a range with only one ticker left.
                start=normalized_start,
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
            if not range_covered(conn, "jquants_fin_summaries", fin_start, asof_date):
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
            if not daily_bars_covered_by_data(conn, normalized_start, asof_date):
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_daily_bars",
                        requirement=(
                            "normalized_per_3fy split basis "
                            f"{normalized_start.isoformat()}..{asof_date.isoformat()}"
                        ),
                        reason="split-normalization bar range is not fully covered in SQLite",
                    )
                )
            if not range_covered(conn, "jquants_fin_summaries", normalized_start, asof_date):
                issues.append(
                    CacheCoverageIssue(
                        source="jquants_fin_summaries",
                        requirement=(
                            "normalized_per_3fy FY history "
                            f"{normalized_start.isoformat()}..{asof_date.isoformat()}"
                        ),
                        reason="normalized-profit summary range is not fully covered in SQLite",
                    )
                )
            _append_jpx_earnings_calendar_issues(
                conn,
                issues,
                asof_date=asof_date,
                allow_stale_jpx=allow_stale_jpx,
            )
            if not range_covered(conn, "jquants_market_calendar", asof_date, asof_date):
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
            _append_weekly_margin_issue(conn, issues, asof_date=asof_date)
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


# The supply/demand axes read the newest balance date already published at the
# as-of. Requiring a recent one keeps a store that silently stopped fetching the
# weekly source from producing a screen whose axes are all null without saying so.
# Three weeks of cadence plus a long closure, with room for one skipped week: the
# 2020 Golden Week already produced twenty days between balance dates, so a tighter
# bound would fail the batch on a state the exchange itself created.
_WEEKLY_MARGIN_MAX_STALE_DAYS = 28


def _append_weekly_margin_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    # The join reads a balance date only when its coverage is `ok`, has rows, and is
    # keyed the way the reader looks it up. The gate has to measure that same
    # quantity through the same predicate; reading the table, or matching on a
    # different column, would report fresh while the join comes back blank.
    readable_dates = [
        parsed
        for coverage_start, coverage_key in conn.execute(
            "SELECT coverage_start, coverage_key FROM source_coverage "
            "WHERE source = ? AND status = 'ok' AND record_count > 0 AND coverage_start <= ?",
            (WEEKLY_MARGIN_SOURCE, asof_date.isoformat()),
        )
        if (parsed := _parsed_date(coverage_start)) is not None
        and str(coverage_key) == weekly_margin_coverage_key(parsed)
    ]
    latest = max(readable_dates).isoformat() if readable_dates else None
    if latest is None:
        issues.append(
            CacheCoverageIssue(
                source="jquants_weekly_margin",
                requirement=asof_date.isoformat(),
                reason="no readable weekly margin balance date at or before the as-of",
            )
        )
        return
    try:
        stale_days = (asof_date - date.fromisoformat(latest)).days
    except ValueError:
        issues.append(
            CacheCoverageIssue(
                source="jquants_weekly_margin",
                requirement=asof_date.isoformat(),
                reason=f"stored balance date is not a date: {latest!r}",
            )
        )
        return
    if stale_days > _WEEKLY_MARGIN_MAX_STALE_DAYS:
        issues.append(
            CacheCoverageIssue(
                source="jquants_weekly_margin",
                requirement=asof_date.isoformat(),
                reason=(
                    f"newest balance date {latest} is {stale_days} days before the as-of "
                    f"(limit {_WEEKLY_MARGIN_MAX_STALE_DAYS})"
                ),
            )
        )


def _parsed_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
