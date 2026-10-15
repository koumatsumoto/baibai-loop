"""Read the stored earnings schedule without reaching into the screening layer.

Tasks depend only on the application database and the foundation, so the schedule
arrives as two columns read directly rather than as a screening domain object. The
table is a published contract of the market store; taking only the ticker and the
date keeps this from carrying any of screening's meaning across the boundary.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import connect_read_only, read_store_if_written

_SCHEDULE_QUERY = (
    "SELECT ticker, MIN(announcement_date) FROM jpx_earnings_calendar "
    "WHERE announcement_date >= ? GROUP BY ticker"
)


def read_published_earnings_dates(sqlite_path: Path, today: date) -> dict[str, date] | None:
    """Read the next stored date per ticker; None means absent or unwritten storage.

    An empty published schedule returns {}. Corrupt files, missing columns and
    partially initialized schemas remain errors rather than being called unpublished.
    """

    def read() -> dict[str, date]:
        with closing(connect_read_only(sqlite_path)) as conn:
            rows = conn.execute(_SCHEDULE_QUERY, (today.isoformat(),)).fetchall()
        published: dict[str, date] = {}
        for ticker, announcement_date in rows:
            try:
                published[str(ticker)] = date.fromisoformat(str(announcement_date))
            except ValueError as error:
                raise ValueError(
                    f"invalid earnings date for {ticker}: {announcement_date}"
                ) from error
        return published

    return read_store_if_written(sqlite_path, read)
