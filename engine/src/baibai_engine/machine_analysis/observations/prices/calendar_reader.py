"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.observations.prices.calendar import JQuantsMarketCalendarDay
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_market_calendar(
    sqlite_path: Path, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    """Return market calendar days for `[start, end]`, or `None` if the
    cache cannot serve the range.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        return _read_calendar(conn, start, end)
    finally:
        conn.close()


def _read_calendar(
    conn: sqlite3.Connection, start: date, end: date
) -> list[JQuantsMarketCalendarDay] | None:
    if not range_covered(conn, "jquants_market_calendar", start, end):
        return None
    rows = conn.execute(
        "SELECT day, holiday_division FROM jquants_market_calendar "
        "WHERE day BETWEEN ? AND ? ORDER BY day",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if any(division not in {"0", "1", "2", "3"} for _, division in rows):
        return None
    return [
        JQuantsMarketCalendarDay(
            day=date.fromisoformat(day), is_business_day=division in {"1", "2"}
        )
        for day, division in rows
    ]
