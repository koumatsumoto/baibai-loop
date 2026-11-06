"""月次asof標本を、各月の銘柄数条件を満たす最終観測日から選ぶ。

公式の月末営業日や全銘柄の取得完了を証明するカレンダーではない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

# 月次標本に採用する観測日の最低銘柄数。営業日や取得完了の公式判定ではない。
DEFAULT_MIN_TICKERS = 2000


def month_end_asof_grid(
    sqlite_path: Path,
    *,
    start: date,
    end: date,
    min_tickers: int = DEFAULT_MIN_TICKERS,
) -> list[date]:
    """Select the last qualifying observed date of each nonfinal data month.

    Read through end+40 days to find later qualifying observations. A later date
    in the same month prevents selecting an earlier one merely because end cuts
    the month. If later days are missing or below the floor, the chosen date can
    still be mid-month. end limits the selected date, not calendar-month closure.
    The final observed data month is omitted; neither rule proves full ingestion.
    """
    # Read-only so that a mistyped cache path fails instead of creating an empty
    # store that the next writer would migrate into a schema-valid empty cache.
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? GROUP BY traded_at ORDER BY traded_at",
            (start.isoformat(), (end + timedelta(days=40)).isoformat()),
        ).fetchall()
    finally:
        conn.close()
    day_counts = [(date.fromisoformat(str(day)), int(count)) for day, count in rows]
    return observed_month_end_dates(day_counts, end=end, min_tickers=min_tickers)


def days_with_bars(
    sqlite_path: Path, days: Sequence[date], *, min_tickers: int = DEFAULT_MIN_TICKERS
) -> set[date]:
    """Return requested dates whose stored bar count meets the cohort floor.

    This path does not consult the trading calendar. Passing the count floor is
    an observed-data selection rule, not proof of complete ingestion.
    """
    if not days or not sqlite_path.exists():
        return set()
    requested = set(days)
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? GROUP BY traded_at",
            (min(requested).isoformat(), max(requested).isoformat()),
        ).fetchall()
    finally:
        conn.close()
    return {
        day
        for day, count in ((date.fromisoformat(str(raw)), int(count)) for raw, count in rows)
        if day in requested and count >= min_tickers
    }


def observed_month_end_dates(
    day_counts: Sequence[tuple[date, int]],
    *,
    end: date,
    min_tickers: int = DEFAULT_MIN_TICKERS,
) -> list[date]:
    """Return each nonfinal data month's last qualifying observed date <= end."""
    latest_by_month: dict[tuple[int, int], date] = {}
    for day, count in day_counts:
        if count < min_tickers:
            continue
        key = (day.year, day.month)
        current = latest_by_month.get(key)
        if current is None or day > current:
            latest_by_month[key] = day
    if not latest_by_month:
        return []
    last_data_month = max(latest_by_month)
    return [
        latest_by_month[key]
        for key in sorted(latest_by_month)
        if key != last_data_month and latest_by_month[key] <= end
    ]
