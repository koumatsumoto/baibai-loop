"""Read the stored earnings schedule without reaching into the screening layer.

Tasks depend only on the application database and the foundation, so the schedule
arrives as two columns read directly rather than as a screening domain object. The
table is a published contract of the market store; taking only the ticker and the
date keeps this from carrying any of screening's meaning across the boundary.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

_SCHEDULE_QUERY = (
    "SELECT ticker, MIN(announcement_date) FROM jquants_earnings_calendar "
    "WHERE announcement_date >= ? GROUP BY ticker"
)


def read_published_earnings_dates(sqlite_path: Path, today: date) -> dict[str, date] | None:
    """The soonest announcement at or after `today` per ticker, or None if unreadable.

    None separates "no schedule is stored" from "the schedule reaches no ticker
    yet": the first is a setup problem worth reporting, the second is the normal
    state between publication windows.
    """
    if not sqlite_path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)) as conn:
            rows = conn.execute(_SCHEDULE_QUERY, (today.isoformat(),)).fetchall()
    except sqlite3.Error:
        return None
    published: dict[str, date] = {}
    for ticker, announcement_date in rows:
        try:
            published[str(ticker)] = date.fromisoformat(str(announcement_date))
        except ValueError:
            continue
    return published
