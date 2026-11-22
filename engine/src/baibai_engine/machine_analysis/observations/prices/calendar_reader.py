"""保存済み観測の責務別読取。取得・公開は行わない。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.observations.prices.calendar import JQuantsMarketCalendarDay
from baibai_engine.primitives.jquants_calendar import cash_equity_open
from baibai_engine.storage.sqlite.source_cache.coverage import range_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


def read_market_calendar(
    sqlite_path: Path, start: date, end: date, *, connection: sqlite3.Connection | None = None
) -> list[JQuantsMarketCalendarDay] | None:
    """Return every calendar day in `[start, end]`, including closed days.

    An incomplete range or unknown classification is unavailable, not a sparse
    session calendar that consumers may compress into the next observed day.
    A supplied connection and its transaction stay with the caller.
    """
    owns_connection = connection is None
    if connection is None:
        if not sqlite_path.exists():
            return None
        connection = connect_current(sqlite_path)
        if connection is None:
            return None
    try:
        if owns_connection:
            connection.execute("BEGIN")
        if not range_covered(connection, "jquants_market_calendar", start, end):
            return None
        rows = connection.execute(
            "SELECT day, holiday_division FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()

    # An acquisition coverage marker does not certify that all stored rows survived.
    if len(rows) != (end - start).days + 1:
        return None
    calendar: list[JQuantsMarketCalendarDay] = []
    for index, (day, division) in enumerate(rows):
        expected = start + timedelta(days=index)
        is_business_day = cash_equity_open(division)
        if day != expected.isoformat() or is_business_day is None:
            return None
        calendar.append(JQuantsMarketCalendarDay(day=expected, is_business_day=is_business_day))
    return calendar
