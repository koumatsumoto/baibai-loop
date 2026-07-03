"""月次 asof グリッド: cache 内の bars から月末営業日を決定論的に導出する。

market calendar cache は直近しか無いため、グリッドは bars の実在 (当日の
cross-section 銘柄数) から導く。閾値未満の日 (休日・部分データ日) は営業日と
みなさない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path

# 東証の全市場営業日は ~3,700-4,900 銘柄が取引される。部分データ日 (取込途中断面)
# を月末営業日と誤認しないための下限。
DEFAULT_MIN_TICKERS = 2000


def month_end_asof_grid(
    sqlite_path: Path,
    *,
    start: date,
    end: date,
    min_tickers: int = DEFAULT_MIN_TICKERS,
) -> list[date]:
    """Return the last full trading day of each month in ``[start, end]``."""
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? GROUP BY traded_at ORDER BY traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    day_counts = [(date.fromisoformat(str(day)), int(count)) for day, count in rows]
    return month_end_dates(day_counts, min_tickers=min_tickers)


def month_end_dates(
    day_counts: Sequence[tuple[date, int]],
    *,
    min_tickers: int = DEFAULT_MIN_TICKERS,
) -> list[date]:
    """Pure selection of the last qualifying trading day per (year, month)."""
    latest_by_month: dict[tuple[int, int], date] = {}
    for day, count in day_counts:
        if count < min_tickers:
            continue
        key = (day.year, day.month)
        current = latest_by_month.get(key)
        if current is None or day > current:
            latest_by_month[key] = day
    return [latest_by_month[key] for key in sorted(latest_by_month)]
