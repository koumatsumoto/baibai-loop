"""Whether a per-date snapshot source has been imported for one date.

This sits in `baibai_engine.screening` rather than beside `range_covered` in the market
coverage kernel because it is on the EDINET extraction's value path, and the extractor's
revision manifest only tracks this package. `read_edinet_documents` uses it to decide
cache-versus-API for a day's document list, and that list carries the correction and
withdrawal events that quarantine a metric row. A predicate that wrongly served a
truncated day would hide a withdrawal, raise no quarantine event, leave
`source_document_revision` matching, and let the stale row be reused — so an edit to it
has to move the revision.

`range_covered` answers a different question: it stitches abutting coverage intervals to
ask whether a *range* is served. The callers here are per-date snapshots that are either
imported for that date or not.
"""

from __future__ import annotations

import sqlite3
from datetime import date


def date_covered(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    """True when a successful `source_coverage` row spans `on_date`."""
    iso = on_date.isoformat()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM source_coverage WHERE source = ? "
            "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
            (source, iso, iso),
        )
    except sqlite3.OperationalError:
        return False
    return cursor.fetchone() is not None
