"""Test helpers for the screening market.sqlite cache.

Common fixture builders that previously appeared verbatim across multiple
test modules. Keep these strictly minimal — only the duplications that exist
in real test code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.screening.sqlite_cache import open_connection


def make_master_records(
    asof: date,
    *,
    count: int = 2500,
    excluded_count: int = 0,
) -> list[dict[str, str]]:
    """Return a complete official-key master response for offline tests."""
    records = [
        {
            "Date": asof.isoformat(),
            "Code": f"{1000 + index:04d}0",
            "CoName": f"Company {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
        }
        for index in range(count)
    ]
    records.extend(
        {
            "Date": asof.isoformat(),
            "Code": f"{9000 + index:04d}1",
            "CoName": f"Excluded {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
        }
        for index in range(excluded_count)
    )
    return records


def add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    record_count: int = 1,
    min_date: str | None = None,
    max_date: str | None = None,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
) -> None:
    """Insert one ``source_coverage`` row, mirroring the production INSERT shape."""
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, "
        "status, error"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, coverage_key, min_date, max_date, fetched_at, record_count, status, error),
    )


def insert_daily_bars_from_closes(
    sqlite_path: Path,
    ticker: str,
    closes: list[float],
    *,
    end_date: date,
    turnover_value: float | None = None,
) -> None:
    """Insert ``len(closes)`` consecutive daily bars ending on ``end_date``.

    ``adjustment_close`` mirrors ``close`` (the test fixtures are unadjusted by
    design). ``turnover_value`` is included only when supplied, so callers that
    do not need a turnover column do not get a non-NULL row.
    """
    start = end_date - timedelta(days=len(closes) - 1)
    if turnover_value is None:
        columns = "ticker, traded_at, close, adjustment_close"
        rows: list[tuple[object, ...]] = [
            (ticker, (start + timedelta(days=index)).isoformat(), close, close)
            for index, close in enumerate(closes)
        ]
        placeholders = "?, ?, ?, ?"
    else:
        columns = "ticker, traded_at, close, adjustment_close, turnover_value"
        rows = [
            (ticker, (start + timedelta(days=index)).isoformat(), close, close, turnover_value)
            for index, close in enumerate(closes)
        ]
        placeholders = "?, ?, ?, ?, ?"
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO jquants_daily_bars({columns}) VALUES ({placeholders})",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
