"""Rebuild source-scoped acquisition inputs from the common canonical integration.

These are disposable read inputs with the existing market schema, not full lake
stores. They deliberately have no lake_store_origin and can never be passed to
publish_market_lake. History and matching coverage stay together.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    observation_calendar_context as calendar_context,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import master_coverage_key
from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)
from baibai_jobs.calendar_cutover import calendar_native_enabled
from baibai_jobs.master_cutover import master_native_enabled

_TABLES = {
    "tradingview": (
        "jquants_market_calendar",
        "jquants_master_snapshots",
        "tradingview_forecast_snapshots",
        "tradingview_source_receipts",
    ),
}


SOURCE_INPUT_GROUPS = tuple(_TABLES)


def export_source_input(source: Path, output: Path, *, group: str, asof: date) -> dict[str, object]:
    """Keep TradingView planning state with explicit calendar/master dependencies."""
    tables: tuple[str, ...] = _TABLES[group]
    if group == "tradingview" and master_native_enabled():
        tables = tuple(table for table in tables if table != "jquants_master_snapshots")
    if group == "tradingview" and calendar_native_enabled():
        tables = tuple(table for table in tables if table != "jquants_market_calendar")
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
        basis = (
            calendar_context(raw, asof)[0].isoformat()
            if group == "tradingview" and "jquants_master_snapshots" in tables
            else None
        )
        for table in tables:
            condition = "1=1"
            values: tuple[object, ...] = ()
            if group == "tradingview" and table == "jquants_master_snapshots":
                condition, values = "snapshot_date = ?", (basis,)
            elif group == "tradingview" and table in {
                "tradingview_forecast_snapshots",
                "tradingview_source_receipts",
            }:
                condition, values = "snapshot_date >= ?", (asof.isoformat(),)
            cursor = raw.execute(f"SELECT * FROM {table} WHERE {condition}", values)  # nosec B608
            placeholders = ",".join("?" for _ in cursor.description or ())
            before = target.total_changes
            target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", cursor)  # nosec B608
            rows_by_table[table] = target.total_changes - before
        master_key = master_coverage_key(date.fromisoformat(basis)) if basis is not None else None
        coverage = tuple(
            table
            for table in tables
            if table
            not in {
                "tradingview_forecast_snapshots",
                "tradingview_source_receipts",
            }
        )
        for name in coverage:
            rows = raw.execute(
                "SELECT * FROM source_coverage WHERE source = ? "
                "AND (? != 'tradingview' OR source != 'jquants_master_snapshots' "
                "OR coverage_key = ?)",
                (name, group, master_key),
            )
            target.executemany("INSERT INTO source_coverage VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return {
        "group": group,
        "asof": asof.isoformat(),
        "rows": rows_by_table,
    }
