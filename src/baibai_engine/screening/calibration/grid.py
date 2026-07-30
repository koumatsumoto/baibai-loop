"""月次 asof グリッド: cache 内の bars から月末営業日を決定論的に導出する。

market calendar cache は直近しか無いため、グリッドは bars の実在 (当日の
cross-section 銘柄数) から導く。閾値未満の日 (休日・部分データ日) は営業日と
みなさない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta
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
    """Return the last full trading day of each **complete** month in ``[start, end]``.

    月の完全性は「翌月の営業日が data に存在するか」で判定する。窓を end+40 日まで
    引いて真の月末を確認し、(a) end が月の途中で切れた月と (b) cache 最終月
    (完全性を確認できない) を cohort から落とす。月中日を「月末営業日」と誤認した
    panel が永続化されると、forward 窓が ~96% 重複する重複 cohort として集計を
    二重計上するため。
    """
    # Read-only so that a mistyped cache path fails instead of creating an empty
    # store that the next writer would migrate into a schema-valid empty cache.
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? GROUP BY traded_at ORDER BY traded_at",
            (start.isoformat(), (end + timedelta(days=40)).isoformat()),
        ).fetchall()
    finally:
        conn.close()
    day_counts = [(date.fromisoformat(str(day)), int(count)) for day, count in rows]
    return complete_month_end_dates(day_counts, end=end, min_tickers=min_tickers)


def days_with_bars(
    sqlite_path: Path, days: Sequence[date], *, min_tickers: int = DEFAULT_MIN_TICKERS
) -> set[date]:
    """Return the requested days the bar store shows as trading days.

    The market calendar is fetched around recent as-of dates only, so it cannot
    answer for historical dates. The bar store can: a day the whole market
    priced carries thousands of rows, so the same ticker floor the cohort grid
    uses separates a trading day from a holiday or a partially ingested day.
    """
    if not days or not sqlite_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(days))
        rows = conn.execute(
            "SELECT traded_at, COUNT(*) FROM jquants_daily_bars "
            f"WHERE traded_at IN ({placeholders}) GROUP BY traded_at",
            [day.isoformat() for day in days],
        ).fetchall()
    finally:
        conn.close()
    return {date.fromisoformat(str(day)) for day, count in rows if int(count) >= min_tickers}


def complete_month_end_dates(
    day_counts: Sequence[tuple[date, int]],
    *,
    end: date,
    min_tickers: int = DEFAULT_MIN_TICKERS,
) -> list[date]:
    """Pure selection: 各 (year, month) の最終 qualifying 営業日のうち、
    end 以前かつ「data 上の最終月でない」ものだけを返す。"""
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
