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

from baibai_engine.machine_analysis.observations.prices.calendar_context import observation_calendar_context as calendar_context
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import master_coverage_key
from baibai_engine.storage.sqlite.source_cache.schema import open_connection, validate_current_schema

_MARKET = (
    "jquants_master_snapshots",
    "jquants_daily_bars",
    "jquants_market_calendar",
    "jquants_weekly_margin",
    "jquants_margin_alerts",
    "jquants_all_issues_daily_margin",
    "jquants_short_sale_reports",
)
_LISTS = ("edinet_documents", "edinet_document_lists", "edinet_list_responses")
_TABLES = {
    "jquants-market": _MARKET,
    "jquants-financial": ("jquants_fin_summaries", "jquants_market_calendar"),
    "edinet-list": _LISTS,
    "edinet-metrics": (*_LISTS, "edinet_metrics"),
    "edinet-facts": _LISTS,
    "jpx-earnings": (),
    "jpx-regulations": (),
    "tradingview": (
        "jquants_market_calendar",
        "jquants_master_snapshots",
        "tradingview_forecast_snapshots",
    ),
}


SOURCE_INPUT_GROUPS = tuple(_TABLES)


def export_source_input(source: Path, output: Path, *, group: str, asof: date) -> dict[str, object]:
    """Keep only a source's planning state and explicit calendar/master/list dependencies.

    J-Quants history and its coverage are copied together, including normal empty
    responses. EDINET archive inventories carry identity only; CSV/XBRL extraction
    belongs to common integration and must not download archives merely to parse
    them again in each acquisition runner. JPX acquisitions need no old facts.
    """
    tables = _TABLES[group]
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
        basis = calendar_context(raw, asof)[0].isoformat() if group == "tradingview" else None
        for table in tables:
            condition = "1=1"
            values: tuple[object, ...] = ()
            if group == "tradingview" and table == "jquants_master_snapshots":
                condition, values = "snapshot_date = ?", (basis,)
            elif group == "tradingview" and table == "tradingview_forecast_snapshots":
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
            if table not in {"edinet_list_responses", "tradingview_forecast_snapshots"}
        )
        if group == "edinet-facts":
            coverage += ("edinet_research_facts",)
        for name in coverage:
            rows = raw.execute(
                "SELECT * FROM source_coverage WHERE source = ? "
                "AND (? != 'tradingview' OR source != 'jquants_master_snapshots' "
                "OR coverage_key = ?)",
                (name, group, master_key),
            )
            target.executemany("INSERT INTO source_coverage VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        source_type = 5 if group == "edinet-metrics" else 1
        archives = (
            list(
                raw.execute(
                    "SELECT doc_id, source_type FROM edinet_source_archives "
                    "WHERE source_type = ? ORDER BY doc_id",
                    (source_type,),
                )
            )
            if group in {"edinet-metrics", "edinet-facts"}
            else []
        )
    return {
        "group": group,
        "asof": asof.isoformat(),
        "rows": rows_by_table,
        "archive_keys": archives,
    }
