"""Read acquisition-only metadata without treating it as a received original."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.documents import (
    PMI_INPUT_COLUMNS,
    PMI_INPUT_TABLE,
)

_DAY_INPUTS = {
    "economic-umich_sca": ("umich-sca-ics", "economic_umich_input_days"),
    "economic-ecb_fx": ("ecb-fx", "economic_ecb_input_days"),
}


def covered_input_days(
    connection: sqlite3.Connection, source_id: str, first: date, last: date
) -> Iterator[date]:
    """Fixed-seed days supplement this execution's real, possibly empty receipts."""
    for source, table in _DAY_INPUTS.values():
        if source != source_id:
            continue
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            for (day,) in connection.execute(
                f"SELECT observed_at FROM {table} WHERE observed_at BETWEEN ? AND ?",  # nosec B608
                (first.isoformat(), last.isoformat()),
            ):
                yield date.fromisoformat(day)


def validate_planning_input(path: Path, group: str) -> None:
    """Validate the existing v1 planning formats at their download boundary."""
    with closing(store.open_read_only_connection(path)) as connection:
        store.validate_current_schema(connection)
        if group == "economic-spglobal_pmi":
            info = connection.execute(f"PRAGMA table_info({PMI_INPUT_TABLE})").fetchall()  # nosec B608
            if tuple(row[1] for row in info) != PMI_INPUT_COLUMNS or tuple(
                row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]
            ) != ("source_id", "request_key"):
                raise ValueError("invalid PMI acquisition input metadata table")
            if connection.execute("SELECT 1 FROM economic_source_documents LIMIT 1").fetchone():
                raise ValueError("PMI acquisition input must not contain document receipt bodies")
            return
        source, table = _DAY_INPUTS[group]
        info = connection.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
        if len(info) != 1 or info[0][1] != "observed_at" or info[0][5] != 1:
            raise ValueError("invalid acquisition coverage table")
        for original in ("economic_source_records", "economic_source_documents"):
            if connection.execute(f"SELECT 1 FROM {original} LIMIT 1").fetchone():  # nosec B608
                raise ValueError("acquisition planning input must not contain original bodies")
        for metadata in ("economic_source_runs", *store.CONFIRMATION_TABLES):
            if connection.execute(
                f"SELECT 1 FROM {metadata} WHERE source_id!=? LIMIT 1",  # nosec B608
                (source,),
            ).fetchone():
                raise ValueError("acquisition planning input contains another source")
