"""ECB FX acquisition coverage without copying provider-native receipt bodies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.handoff import export_receipts

SOURCE = "ecb-fx"
TABLE = "economic_ecb_input_days"
_METADATA_TABLES = ("economic_source_runs", *store.CONFIRMATION_TABLES)


def export_ecb_input(source: Path, output: Path) -> None:
    """Retain exact run/confirmation metadata and every covered native day.

    The single ZIP request never uses previous values. Keep covered empty days
    as well as nonempty periods; neither is replaced by a synthetic receipt.
    """
    export_receipts(source, output, source_ids=(), since=datetime.min.replace(tzinfo=UTC))
    with (
        closing(store.open_read_only_connection(source)) as received,
        closing(sqlite3.connect(output)) as target,
        target,
    ):
        target.execute(f"CREATE TABLE {TABLE}(observed_at TEXT NOT NULL PRIMARY KEY)")  # nosec B608
        target.executemany(
            f"INSERT INTO {TABLE} VALUES (?)",  # nosec B608
            received.execute(
                "SELECT DISTINCT observed_at FROM economic_source_records "
                "WHERE source_id=? ORDER BY observed_at",
                (SOURCE,),
            ),
        )
        for table in _METADATA_TABLES:
            rows = received.execute(f"SELECT * FROM {table} WHERE source_id=?", (SOURCE,))  # nosec B608
            placeholders = ",".join("?" for _ in rows.description or ())
            target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)  # nosec B608


def covered_input_days(connection: sqlite3.Connection, first: date, last: date) -> Iterator[date]:
    """Add fixed-seed coverage to the real receipts acquired in this execution."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone():
        for (day,) in connection.execute(
            f"SELECT observed_at FROM {TABLE} WHERE observed_at BETWEEN ? AND ?",  # nosec B608
            (first.isoformat(), last.isoformat()),
        ):
            yield date.fromisoformat(day)


def validate_ecb_input(path: Path) -> None:
    with closing(store.open_read_only_connection(path)) as connection:
        store.validate_current_schema(connection)
        info = connection.execute(f"PRAGMA table_info({TABLE})").fetchall()  # nosec B608
        if len(info) != 1 or info[0][1] != "observed_at" or info[0][5] != 1:
            raise ValueError("invalid ECB FX acquisition coverage table")
        for table in ("economic_source_records", "economic_source_documents"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():  # nosec B608
                raise ValueError("ECB FX acquisition input must not contain original bodies")
        for table in _METADATA_TABLES:
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE source_id!=? LIMIT 1",  # nosec B608
                (SOURCE,),
            ).fetchone():
                raise ValueError("ECB FX acquisition input contains another source")
