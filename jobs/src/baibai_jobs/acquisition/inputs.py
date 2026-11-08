"""Export saved TradingView receipts and snapshots for acquisition planning.

Native master/calendar inputs are fixed separately before provider access. These
scoped inputs have no market facts, source coverage, or lake store origin.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)

_TABLES = {
    "tradingview": (
        "tradingview_forecast_snapshots",
        "tradingview_source_receipts",
    ),
}


SOURCE_INPUT_GROUPS = tuple(_TABLES)


def export_source_input(source: Path, output: Path, *, group: str, asof: date) -> dict[str, object]:
    """Keep TradingView planning state with explicit calendar/master dependencies."""
    tables: tuple[str, ...] = _TABLES[group]
    if output.exists():
        raise FileExistsError(output)
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(output)) as target,
        target,
    ):
        validate_current_schema(raw)
        raw.execute("BEGIN")
        rows_by_table = {}
        for table in tables:
            condition = "1=1"
            values: tuple[object, ...] = ()
            if group == "tradingview" and table in {
                "tradingview_forecast_snapshots",
                "tradingview_source_receipts",
            }:
                condition, values = "snapshot_date >= ?", (asof.isoformat(),)
            cursor = raw.execute(f"SELECT * FROM {table} WHERE {condition}", values)  # nosec B608
            placeholders = ",".join("?" for _ in cursor.description or ())
            before = target.total_changes
            target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", cursor)  # nosec B608
            rows_by_table[table] = target.total_changes - before
    return {
        "group": group,
        "asof": asof.isoformat(),
        "rows": rows_by_table,
    }
