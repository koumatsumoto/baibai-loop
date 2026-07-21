"""Query-only store freshness lookups for the meta view.

Freshness derives exclusively from timestamps recorded inside each store, never
from file mtime, so the values stay correct after the stores are copied to a
remote object store that does not preserve filesystem metadata.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .sqlite import connect_read_only


def screening_latest_asof(path: Path) -> date | None:
    """Return the as-of date of the newest screening run, or None without runs."""

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute("SELECT max(asof_date) FROM screening_run").fetchone()
    finally:
        connection.close()
    return None if row[0] is None else date.fromisoformat(str(row[0]))


def macro_latest_observed_at(path: Path) -> date | None:
    """Return the newest successfully fetched macro observation date across all series."""

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            "SELECT max(observed_at) FROM observations WHERE fetch_status = 'ok'"
        ).fetchone()
    finally:
        connection.close()
    return None if row[0] is None else date.fromisoformat(str(row[0]))


def application_db_updated_at(path: Path) -> datetime | None:
    """Return the newest write instant recorded inside the application database.

    The application database only grows through ledger events, research packet
    revisions, and macro context revisions, so the max of their stored timestamps
    is the last judgment-layer update. Timestamps are compared as parsed datetimes
    because stores may mix timezone offsets.
    """

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            """
            SELECT occurred_at FROM ledger_event
            UNION ALL
            SELECT published_at FROM research_packet
            UNION ALL
            SELECT published_at FROM macro_context
            """
        ).fetchall()
    finally:
        connection.close()
    return max((datetime.fromisoformat(str(row[0])) for row in rows), default=None)


__all__ = [
    "application_db_updated_at",
    "macro_latest_observed_at",
    "screening_latest_asof",
]
