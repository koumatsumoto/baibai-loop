"""Shared market.sqlite fixtures for source readers and model generation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.storage.sqlite.source_cache.coverage import record_range_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def insert_financial_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Insert a constructed provider-native document, without financial projection."""
    conn.execute(
        "INSERT OR REPLACE INTO jquants_fin_summaries "
        "(ticker, disclosed_at, disclosure_number, document_payload) VALUES (?, ?, ?, ?)",
        (
            str(record["Code"])[:4],
            record["DiscDate"],
            str(record["DiscNo"]),
            json.dumps(record, ensure_ascii=False),
        ),
    )


def make_master_records(
    asof: date,
    *,
    count: int = 2500,
    secondary_class_count: int = 0,
) -> list[dict[str, str]]:
    """Return a complete official-key master response for offline tests."""
    records = [
        {
            "Date": asof.isoformat(),
            "Code": f"{1000 + index:04d}0",
            "CoName": f"Company {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
            "ProdCat": "011",
        }
        for index in range(count)
    ]
    records.extend(
        {
            "Date": asof.isoformat(),
            "Code": f"{9000 + index:04d}1",
            "CoName": f"Secondary class {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
            "ProdCat": "011",
        }
        for index in range(secondary_class_count)
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
    """Insert consecutive raw closes; absent turnover remains SQL NULL."""
    start = end_date - timedelta(days=len(closes) - 1)
    rows = [
        (ticker, (start + timedelta(days=index)).isoformat(), close, turnover_value)
        for index, close in enumerate(closes)
    ]
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, turnover_value) VALUES (?, ?, ?, ?)",
            rows,
        )
        # Synthetic closes here represent a completed fetch of their date range.
        # Production coverage comes from acquisition, not observation presence.
        if closes:
            record_range_source_coverage(
                conn,
                source="jquants_daily_bars",
                operation="get_daily_quotes",
                table="jquants_daily_bars",
                date_column="traded_at",
                requested_start=start,
                requested_end=end_date,
                status="ok",
                error=None,
            )
        conn.commit()
    finally:
        conn.close()


def seed_daily_bars(
    sqlite_path: Path,
    rows: list[tuple[str, str, float | None, float | None]],
) -> None:
    """Insert (ticker, traded_at, close, adjustment_factor) rows into the store."""
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
