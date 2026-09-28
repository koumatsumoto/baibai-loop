"""日付snapshotの取得完了判定。

EDINET書類の訂正・撤回を見落とさないためextractor revisionの対象とする。"""

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
