"""J-Quants coverage checks: master rows and bar / fin-summary density."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import baibai_engine.machine_analysis.observations.stocks.master_snapshot as master_contract

from .common_stock import (
    COMMON_STOCK_SNAPSHOT_SQL,
    scope_parameters,
)
from .shared import CacheCoverageIssue

_DENSITY_BUCKET_DAYS = 120


def _append_master_snapshot_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    iso = asof_date.isoformat()
    row = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN length(security_code) != 5 "
        "OR COALESCE(json_extract(record_json, '$.CoName'), '') = '' "
        "OR COALESCE(json_extract(record_json, '$.MktNm'), '') = '' "
        "OR COALESCE(json_extract(record_json, '$.S33Nm'), '') = '' "
        "OR COALESCE(json_extract(record_json, '$.ProdCat'), '') = '' "
        "THEN 1 ELSE 0 END) "
        "FROM jquants_master_snapshots WHERE snapshot_date = ?",
        (iso,),
    ).fetchone()
    actual_count = int(row[0] or 0)
    invalid_count = int(row[1] or 0)
    common_count = _common_stock_count(conn, asof_date)
    coverage_rows = conn.execute(
        "SELECT coverage_start, coverage_end, record_count, status, error "
        "FROM source_coverage WHERE source = ? AND coverage_key = ?",
        (master_contract.MASTER_SOURCE, master_contract.master_coverage_key(asof_date)),
    ).fetchall()

    if actual_count <= 0:
        issues.append(
            CacheCoverageIssue(
                source=master_contract.MASTER_SOURCE,
                requirement=iso,
                reason="no exact master snapshot rows for requested as-of",
            )
        )
    if len(coverage_rows) != 1:
        issues.append(
            CacheCoverageIssue(
                source=master_contract.MASTER_SOURCE,
                requirement=master_contract.master_coverage_key(asof_date),
                reason="canonical exact-date master coverage row is missing or duplicated",
            )
        )
    else:
        coverage_start, coverage_end, record_count, status, error = coverage_rows[0]
        if coverage_start != iso or coverage_end != iso:
            issues.append(
                CacheCoverageIssue(
                    source=master_contract.MASTER_SOURCE,
                    requirement=master_contract.master_coverage_key(asof_date),
                    reason="master coverage range does not equal requested as-of",
                )
            )
        if status != "ok" or error is not None:
            suffix = f": {error}" if error else ""
            issues.append(
                CacheCoverageIssue(
                    source=master_contract.MASTER_SOURCE,
                    requirement=master_contract.master_coverage_key(asof_date),
                    reason=f"source_coverage status is not ok: {status}{suffix}",
                )
            )
        coverage_count = master_contract.master_coverage_count(record_count)
        if coverage_count is None:
            issues.append(
                CacheCoverageIssue(
                    source=master_contract.MASTER_SOURCE,
                    requirement=iso,
                    reason="master source_coverage record_count is not a non-negative integer",
                )
            )
        if coverage_count is not None and coverage_count != actual_count:
            issues.append(
                CacheCoverageIssue(
                    source=master_contract.MASTER_SOURCE,
                    requirement=iso,
                    reason=(
                        f"exact master row count ({actual_count}) does not match "
                        f"source_coverage record_count ({coverage_count}); repair SQLite"
                    ),
                )
            )
    if invalid_count:
        issues.append(
            CacheCoverageIssue(
                source=master_contract.MASTER_SOURCE,
                requirement=iso,
                reason=(
                    f"exact master snapshot has {invalid_count} row(s) with invalid required data"
                ),
            )
        )
    if common_count < master_contract.MIN_COMMON_STOCK_MASTER_ROWS:
        issues.append(
            CacheCoverageIssue(
                source=master_contract.MASTER_SOURCE,
                requirement=iso,
                reason=(
                    f"exact master common-stock row count ({common_count}) is below "
                    f"minimum {master_contract.MIN_COMMON_STOCK_MASTER_ROWS}; repair SQLite"
                ),
            )
        )


def _append_asof_bar_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    asof_iso = asof_date.isoformat()
    expected = _common_stock_count(conn, asof_date)
    if expected <= 0:
        return
    actual_row = conn.execute(
        f"WITH p AS ({COMMON_STOCK_SNAPSHOT_SQL}) "  # nosec B608
        "SELECT COUNT(DISTINCT b.ticker) FROM jquants_daily_bars b "
        "JOIN p ON p.ticker = b.ticker "
        "WHERE b.traded_at = :traded_at AND b.close IS NOT NULL",
        {**scope_parameters(asof_date), "traded_at": asof_iso},
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


def _append_recent_bar_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    expected = _common_stock_count(conn, asof_date)
    if expected <= 0:
        return
    recent_start = asof_date - timedelta(days=30)
    rows = conn.execute(
        f"WITH p AS ({COMMON_STOCK_SNAPSHOT_SQL}) "  # nosec B608
        "SELECT b.traded_at, COUNT(DISTINCT b.ticker) FROM jquants_daily_bars b "
        "JOIN p ON p.ticker = b.ticker "
        "WHERE b.traded_at BETWEEN :start AND :end AND b.close IS NOT NULL "
        "GROUP BY b.traded_at ORDER BY b.traded_at DESC LIMIT 5",
        {
            **scope_parameters(asof_date),
            "start": recent_start.isoformat(),
            "end": asof_date.isoformat(),
        },
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


def _append_daily_history_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    start: date,
    end: date,
) -> None:
    expected = _common_stock_count(conn, end)
    if expected < master_contract.MIN_COMMON_STOCK_MASTER_ROWS:
        return
    minimum_tickers = max(1, expected // 2)
    weak_buckets: list[str] = []
    bucket_start = start
    while bucket_start <= end:
        bucket_end = min(end, bucket_start + timedelta(days=_DENSITY_BUCKET_DAYS - 1))
        bucket_days = (bucket_end - bucket_start).days + 1
        minimum_floor = 1 if bucket_days < 20 else 5
        minimum_dates = max(minimum_floor, int(bucket_days * 0.25))
        row = conn.execute(
            f"WITH p AS ({COMMON_STOCK_SNAPSHOT_SQL}) "  # nosec B608
            "SELECT COUNT(*) FROM ("
            "SELECT b.traded_at FROM jquants_daily_bars b "
            "JOIN p ON p.ticker = b.ticker "
            "WHERE b.traded_at BETWEEN :start AND :end AND b.close IS NOT NULL "
            "GROUP BY b.traded_at HAVING COUNT(DISTINCT b.ticker) >= :minimum"
            ")",
            {
                **scope_parameters(end),
                "start": bucket_start.isoformat(),
                "end": bucket_end.isoformat(),
                "minimum": minimum_tickers,
            },
        ).fetchone()
        actual_dates = int(row[0] or 0)
        if actual_dates < minimum_dates:
            weak_buckets.append(
                f"{bucket_start.isoformat()}..{bucket_end.isoformat()} "
                f"({actual_dates}/{minimum_dates} usable dates)"
            )
        bucket_start = bucket_end + timedelta(days=1)
    if weak_buckets:
        issues.append(
            CacheCoverageIssue(
                source="jquants_daily_bars",
                requirement=f"{start.isoformat()}..{end.isoformat()}",
                reason=(
                    "daily bars long-history usable date density is too small in buckets: "
                    + "; ".join(weak_buckets)
                ),
            )
        )


def _append_fin_summary_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    start: date,
    end: date,
) -> None:
    expected = _common_stock_count(conn, end)
    if expected < master_contract.MIN_COMMON_STOCK_MASTER_ROWS:
        return
    minimum_tickers = max(1, expected // 2)
    recent_start = max(start, end - timedelta(days=450))
    row = conn.execute(
        f"WITH p AS ({COMMON_STOCK_SNAPSHOT_SQL}) "  # nosec B608
        "SELECT COUNT(DISTINCT s.ticker) FROM jquants_fin_summaries s "
        "JOIN p ON p.ticker = s.ticker "
        "WHERE s.disclosed_at BETWEEN :start AND :end",
        {**scope_parameters(end), "start": recent_start.isoformat(), "end": end.isoformat()},
    ).fetchone()
    actual = int(row[0] or 0)
    if actual < minimum_tickers:
        issues.append(
            CacheCoverageIssue(
                source="jquants_fin_summaries",
                requirement=f"{start.isoformat()}..{end.isoformat()}",
                reason=(
                    f"financial summary usable ticker count in recent window ({actual}) is "
                    f"too small relative to common-stock master rows ({expected}); repair SQLite"
                ),
            )
        )


def _common_stock_count(conn: sqlite3.Connection, snapshot_date: date) -> int:
    master_row = conn.execute(
        f"SELECT COUNT(DISTINCT ticker) FROM ({COMMON_STOCK_SNAPSHOT_SQL})",  # nosec B608
        scope_parameters(snapshot_date),
    ).fetchone()
    return int(master_row[0] or 0)
