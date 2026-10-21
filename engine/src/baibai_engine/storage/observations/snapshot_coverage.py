"""取得済みsnapshotの日付被覆。未取得とDB照会失敗を区別する。"""

from __future__ import annotations

import sqlite3
from datetime import date


def date_covered(conn: sqlite3.Connection, source: str, on_date: date) -> bool:
    """True when a successful `source_coverage` row spans `on_date`."""
    iso = on_date.isoformat()
    cursor = conn.execute(
        "SELECT 1 FROM source_coverage WHERE source = ? "
        "AND coverage_start <= ? AND coverage_end >= ? AND status = 'ok' LIMIT 1",
        (source, iso, iso),
    )
    return cursor.fetchone() is not None
