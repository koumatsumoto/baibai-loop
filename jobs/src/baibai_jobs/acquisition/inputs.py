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

from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)

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
        for table in tables:
            cursor = raw.execute(f"SELECT * FROM {table}")  # nosec B608
            placeholders = ",".join("?" for _ in cursor.description or ())
            before = target.total_changes
            target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", cursor)  # nosec B608
            rows_by_table[table] = target.total_changes - before
        coverage = tuple(table for table in tables if table != "edinet_list_responses")
        if group == "edinet-facts":
            coverage += ("edinet_research_facts",)
        for name in coverage:
            rows = raw.execute(
                "SELECT * FROM source_coverage WHERE source = ?",
                (name,),
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
