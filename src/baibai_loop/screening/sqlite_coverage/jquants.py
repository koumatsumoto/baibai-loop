"""J-Quants coverage checks: master rows and bar / fin-summary density."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .shared import CacheCoverageIssue

_MIN_COMMON_STOCK_MASTER_ROWS = 2500
_DENSITY_BUCKET_DAYS = 120


def _append_master_common_stock_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    common_count = _latest_common_stock_count(conn)
    if common_count < _MIN_COMMON_STOCK_MASTER_ROWS:
        issues.append(
            CacheCoverageIssue(
                source="jquants_master_snapshots",
                requirement=asof_date.isoformat(),
                reason=(
                    f"latest master common-stock row count ({common_count}) is below "
                    f"minimum {_MIN_COMMON_STOCK_MASTER_ROWS}; repair SQLite"
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


def _append_daily_history_density_issue(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    start: date,
    end: date,
) -> None:
    expected = _latest_common_stock_count(conn)
    if expected < _MIN_COMMON_STOCK_MASTER_ROWS:
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
            "SELECT COUNT(*) FROM ("
            "SELECT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? AND close IS NOT NULL "
            "GROUP BY traded_at HAVING COUNT(DISTINCT ticker) >= ?"
            ")",
            (bucket_start.isoformat(), bucket_end.isoformat(), minimum_tickers),
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
    expected = _latest_common_stock_count(conn)
    if expected < _MIN_COMMON_STOCK_MASTER_ROWS:
        return
    minimum_tickers = max(1, expected // 2)
    recent_start = max(start, end - timedelta(days=450))
    row = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM jquants_fin_summaries "
        "WHERE disclosed_at BETWEEN ? AND ?",
        (recent_start.isoformat(), end.isoformat()),
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


def _latest_common_stock_count(conn: sqlite3.Connection) -> int:
    master_row = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM jquants_master_snapshots "
        "WHERE is_common_stock = 1 AND snapshot_date = ("
        "SELECT MAX(snapshot_date) FROM jquants_master_snapshots WHERE snapshot_date != 'unknown'"
        ")"
    ).fetchone()
    return int(master_row[0] or 0)
