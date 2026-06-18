"""Test helpers for the screening market.sqlite cache.

Common fixture builders that previously appeared verbatim across multiple
test modules. Keep these strictly minimal — only the duplications that exist
in real test code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    record_count: int = 1,
    min_date: str | None = None,
    max_date: str | None = None,
    status: str = "ok",
    fetched_at_utc: str | None = None,
) -> None:
    """Insert one ``source_coverage`` row, mirroring the production INSERT shape."""
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, coverage_key, min_date, max_date, fetched_at, record_count, status),
    )
