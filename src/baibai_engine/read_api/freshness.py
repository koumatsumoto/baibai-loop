"""Query-only store freshness lookups for the meta view.

Freshness derives exclusively from timestamps recorded inside each store, never
from file mtime, so the values stay correct after the stores are copied to a
remote object store that does not preserve filesystem metadata.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.macro.indicators.definitions import load_definitions

from .sqlite import read_rows

# Judgment-layer stores are all JST-domain records. Date-only columns
# (task dates, holding-review as-of) and any timezone-naive value are read at JST
# so they compare with the timezone-aware timestamps in the same max().
_JST = ZoneInfo("Asia/Tokyo")


def screening_latest_asof(path: Path) -> date | None:
    """Return the as-of date of the newest screening run, or None without runs."""

    rows = read_rows(path, "SELECT max(asof_date) FROM screening_run")
    if not rows or rows[0][0] is None:
        return None
    return date.fromisoformat(str(rows[0][0]))


def macro_latest_observed_at(path: Path) -> date | None:
    """Return the newest successful observation among currently registered series."""

    if not path.is_file():
        return None
    registered = {series.series_id for series in load_definitions().series}
    if not registered:
        return None
    rows = read_rows(
        path,
        (
            # A retracted date is not an observation any consumer reads, so it must not
            # be what the freshness badge dates the store by. Asking whether a newer
            # retraction exists costs a third of resolving the newest vintage outright,
            # and retractions are rare enough that the check almost always short-circuits.
            "SELECT o.series_id, max(o.observed_at) FROM observations o "
            "WHERE o.fetch_status = 'ok' AND NOT EXISTS ("
            "SELECT 1 FROM observations r "
            "WHERE r.series_id = o.series_id AND r.observed_at = o.observed_at "
            "AND r.fetch_status = 'retracted' AND r.vintage_at > o.vintage_at"
            ") GROUP BY o.series_id"
        ),
    )
    latest = max(
        (str(row[1]) for row in rows if str(row[0]) in registered and row[1] is not None),
        default=None,
    )
    return None if latest is None else date.fromisoformat(latest)


def application_db_updated_at(path: Path) -> datetime | None:
    """Return the newest write instant recorded inside the application database.

    The value is the max over every judgment-layer write timestamp: ledger events,
    research theses and reviews, holding reviews, macro context revisions, reviewed
    shortlists, proposals (created and decided), tasks (created and closed), and
    operation sessions (started and completed). Nullable decision timestamps are
    excluded until set. Values are normalized to timezone-aware JST before the max
    so timezone-aware timestamps and date-only columns compare in one pass.
    """

    rows = read_rows(
        path,
        """
            SELECT occurred_at FROM ledger_event
            UNION ALL
            SELECT published_at FROM thesis
            UNION ALL
            SELECT reviewed_at FROM thesis_review
            UNION ALL
            SELECT as_of FROM holding_review
            UNION ALL
            SELECT published_at FROM macro_context
            UNION ALL
            SELECT published_at FROM shortlist
            UNION ALL
            SELECT created_at FROM proposal
            UNION ALL
            SELECT decided_at FROM proposal WHERE decided_at IS NOT NULL
            UNION ALL
            SELECT created_at FROM task
            UNION ALL
            SELECT closed_at FROM task WHERE closed_at IS NOT NULL
            UNION ALL
            SELECT started_at FROM operation_session
            UNION ALL
            SELECT completed_at FROM operation_session WHERE completed_at IS NOT NULL
            """,
    )
    return max((_as_jst_instant(str(row[0])) for row in rows), default=None)


def _as_jst_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_JST)


__all__ = [
    "application_db_updated_at",
    "macro_latest_observed_at",
    "screening_latest_asof",
]
