"""Query-only market price views for read-only application consumers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .sqlite import connect_read_only


def latest_unadjusted_closes(path: Path, tickers: Sequence[str]) -> dict[str, tuple[float, date]]:
    """Return each ticker's most recent non-null unadjusted close and its trade date.

    Reads the licensed daily-bars store read-only. A missing file or an unknown
    ticker yields no entry so callers fall back to the canonical ledger observation.
    Rows whose close is NULL are ignored.
    """

    if not tickers or not path.is_file():
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            f"""
            SELECT ticker, traded_at, close FROM (
                SELECT ticker, traded_at, close,
                       row_number() OVER (
                           PARTITION BY ticker ORDER BY traded_at DESC
                       ) AS rank
                FROM jquants_daily_bars
                WHERE ticker IN ({placeholders}) AND close IS NOT NULL
            )
            WHERE rank = 1
            """,
            unique,
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]): (float(row[2]), date.fromisoformat(str(row[1]))) for row in rows}


__all__ = ["latest_unadjusted_closes"]
