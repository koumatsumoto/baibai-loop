"""Merge one market store into another, keeping every row both sides hold.

The market store has two writers. The daily batch extends it forward in the cloud, and
an operator extends it backward locally — a decade of bars is hours of provider calls,
so the deep history is fetched once on a machine that can take the time. Each side
therefore holds rows the other has never seen, and publishing the local store means
merging the cloud copy into it first and proving that nothing cloud-side is dropped.

Every table here is a fact table with a key, including ``source_coverage``: its key is a
source and a single day, so two stores that both fetched a day agree on the row rather
than describing overlapping ranges that would have to be reconciled.

Both stores must carry the current schema. An older cloud copy is not migrated here —
the cloud raises its own schema by opening the store, and doing it from this side would
publish a shape the cloud has never written.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from baibai_batch.storage.store_merge import (
    MergeError,
    MergeReport,
    count,
    merge_fact_tables,
    schema_name,
)
from baibai_engine.batch_api import (
    MARKET_SCHEMA_VERSION as SQLITE_SCHEMA_VERSION,
)
from baibai_engine.batch_api import (
    MarketSchemaError as SQLiteSchemaError,
)
from baibai_engine.batch_api import (
    validate_market_schema as validate_current_schema,
)

# Every table in the store, with the key that decides whether a row is the same row.
# Listed rather than read from the schema: a new table has to be classified by someone
# who knows whether its rows accumulate, and an unlisted one would otherwise be dropped
# from the merge in silence. `test_every_market_table_is_merged` holds the two together.
FACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "edinet_buyback_reports": ("ticker", "report_month_end"),
    "edinet_document_lists": ("doc_date",),
    "edinet_documents": ("doc_date", "sequence_number"),
    "edinet_metrics": ("asof_date", "ticker"),
    "jpx_regulation_flags": ("asof_date", "source_name", "ticker", "flag"),
    "jpx_regulation_sources": ("asof_date", "source_name"),
    "jquants_daily_bars": ("ticker", "traded_at"),
    "jquants_earnings_calendar": ("announcement_date", "ticker"),
    "jquants_fin_summaries": ("ticker", "disclosed_at"),
    "jquants_market_calendar": ("day",),
    "jquants_master_snapshots": ("snapshot_date", "ticker"),
    "jquants_weekly_margin": ("week_end", "ticker"),
    "source_coverage": ("source", "coverage_key"),
}

# Columns that record how and when a store read the source, not what the source said.
# Two stores that read the same record at different moments differ on these by
# construction — measured on the real stores, 218 of 220 disagreements were nothing but
# `fetched_at_utc` — so comparing them would refuse every merge. EDINET is the reason the
# other two are here: it revises a document's edit status and finalises a day's list after
# first publishing them, so the later read carries a different marker for the same
# document. The insert leaves the target's reading in place. Everything the source
# actually asserts — prices, financials, holdings, coverage extents and counts — stays
# compared, and on the real stores those disagreed nowhere.
UNCOMPARED: Mapping[str, tuple[str, ...]] = {
    "edinet_document_lists": ("process_datetime", "fetched_at_utc", "is_final"),
    "edinet_documents": ("doc_info_edit_status",),
    "jpx_regulation_flags": ("fetched_at_utc",),
    "jpx_regulation_sources": ("fetched_at_utc",),
    "source_coverage": ("fetched_at_utc",),
}


def merge_stores(source: Path, target: Path) -> MergeReport:
    """Insert every source row the target lacks, and verify none is left behind."""

    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"market store does not exist: {path}")
    _require_schema(source)
    _require_schema(target)
    uri = f"{target.resolve().as_uri()}"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        connection.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
        try:
            _require_attached_version(connection, path=source, schema="source")
            # No separate column comparison: the shape validator above pins every column
            # of every table on both stores, so two stores that reach here cannot differ.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                tables = merge_fact_tables(connection, FACT_KEYS, uncompared=UNCOMPARED)
                connection.commit()
                return MergeReport(tables=tables)
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _require_schema(path: Path) -> None:
    """Open the store on its own to check its shape.

    The market schema validator reads the connection's own tables, so it cannot speak
    about an attached database. Checking each store before the attach keeps the contract
    exact instead of settling for the version number alone.
    """

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        version = count(connection, "PRAGMA user_version")
        if version != SQLITE_SCHEMA_VERSION:
            raise MergeError(
                f"market store schema is {version} but this code expects "
                f"{SQLITE_SCHEMA_VERSION}: {path}"
            )
        try:
            validate_current_schema(connection)
        except SQLiteSchemaError as error:
            raise MergeError(f"market store schema contract is invalid: {path}: {error}") from error


def _require_attached_version(connection: sqlite3.Connection, *, path: Path, schema: str) -> None:
    """Re-read the version through the attachment the merge will actually write from.

    The shape was checked on a separate connection, so this is what ties that check to
    this one: a path that resolved to a different file between the two opens shows up
    here as a version that no longer matches.
    """

    version = count(connection, f"PRAGMA {schema_name(schema)}.user_version")
    if version != SQLITE_SCHEMA_VERSION:
        raise MergeError(
            f"market store schema changed between opening and attaching: {path} is {version}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="store to merge from")
    parser.add_argument("--target", type=Path, required=True, help="store to merge into")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = merge_stores(args.source, args.target)
    except MergeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report.render())
    print(f"merged {report.inserted} rows into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
