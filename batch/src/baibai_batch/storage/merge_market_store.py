"""Merge one market store into another, keeping every row both sides hold.

The market store has two writers. The daily batch extends it forward in the cloud, and
an operator extends it backward locally — a decade of bars is hours of provider calls,
so the deep history is fetched once on a machine that can take the time. Each side
therefore holds rows the other has never seen, and publishing the local store means
merging the cloud copy into it first and proving that nothing cloud-side is dropped.

Every table here is a fact table with a key. ``source_coverage`` carries both single-day
keys and range claims. A clean financial-summary range is valid only while its stored
count matches the rows in that store, so input claims are recounted before the union and
target claims are regenerated from the resulting rows. Failure provenance remains an
exact fact row and is preserved without treating a failed fetch as a completeness claim;
unknown or hybrid status/error states are rejected rather than falling between them.

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
# document. `extractor_revision` identifies the local reader implementation rather than
# an assertion in that document. The insert leaves the target's reading metadata in
# place. Everything the source actually asserts — prices, financials, holdings, coverage
# extents and counts — stays compared.
UNCOMPARED: Mapping[str, tuple[str, ...]] = {
    "edinet_document_lists": ("process_datetime", "fetched_at_utc", "is_final"),
    "edinet_documents": ("doc_info_edit_status",),
    "edinet_metrics": ("extractor_revision",),
    "jpx_regulation_flags": ("fetched_at_utc",),
    "jpx_regulation_sources": ("fetched_at_utc",),
    "source_coverage": ("fetched_at_utc",),
}

# The cloud copy can carry the current columns without having fetched their values yet.
# A fully rebuilt local target may enrich only these fields. The directional comparison
# still refuses a populated source against a missing target and any two populated values
# that disagree, so a local cache that is not fully rebuilt cannot claim completeness.
SOURCE_MISSING_ALLOWED: Mapping[str, tuple[str, ...]] = {
    "jquants_fin_summaries": (
        "forecast_profit",
        "forecast_ordinary_profit",
        "treasury_shares",
        "equity_to_asset_ratio",
    ),
}

# `record_count` receives a table-aware comparison below. It proves every clean
# financial-summary range against the corresponding rows before and after the union,
# while other sources and non-clean shared rows retain exact payload agreement.
_CUSTOM_COMPARED: Mapping[str, tuple[str, ...]] = {
    "source_coverage": ("record_count",),
}
_MERGE_EXEMPTIONS: Mapping[str, tuple[str, ...]] = {
    table: (*UNCOMPARED.get(table, ()), *_CUSTOM_COMPARED.get(table, ()))
    for table in FACT_KEYS
    if table in UNCOMPARED or table in _CUSTOM_COMPARED
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
                _require_compatible_source_coverage_counts(connection)
                tables = merge_fact_tables(
                    connection,
                    FACT_KEYS,
                    uncompared=_MERGE_EXEMPTIONS,
                    source_missing_allowed=SOURCE_MISSING_ALLOWED,
                )
                _reconcile_fin_summary_coverage_counts(connection)
                _require_compatible_source_coverage_counts(connection)
                connection.commit()
                return MergeReport(tables=tables)
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _require_compatible_source_coverage_counts(connection: sqlite3.Connection) -> None:
    """Allow differing clean counts only when both inputs prove their range claims."""

    _require_fin_summary_coverage_states(connection, schema="source")
    _require_fin_summary_coverage_states(connection, schema="main")
    _require_fin_summary_coverage_counts(connection, schema="source")
    _require_fin_summary_coverage_counts(connection, schema="main")
    rows = connection.execute(
        "SELECT s.source, s.coverage_key, s.coverage_start, t.coverage_start, "
        "s.coverage_end, t.coverage_end, s.record_count, t.record_count, "
        "s.status, t.status, s.error, t.error "
        "FROM source.source_coverage s "
        "JOIN main.source_coverage t USING (source, coverage_key) "
        "WHERE s.record_count IS NOT t.record_count "
        "ORDER BY s.source, s.coverage_key"
    ).fetchall()
    for row in rows:
        (
            source,
            coverage_key,
            source_start,
            target_start,
            source_end,
            target_end,
            source_count,
            target_count,
            source_status,
            target_status,
            source_error,
            target_error,
        ) = row
        key = f"{source!r}, {coverage_key!r}"
        if (
            source != "jquants_fin_summaries"
            or source_start != target_start
            or source_end != target_end
            or coverage_key != f"get_fin_summary_range:{source_start}..{source_end}"
            or source_status != "ok"
            or target_status != "ok"
            or source_error is not None
            or target_error is not None
            or not isinstance(source_count, int)
            or not isinstance(target_count, int)
            or source_count < 0
            or target_count < 0
            or source_start is None
            or source_end is None
        ):
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual_source = _fin_summary_range_count(
            connection, schema="source", start=str(source_start), end=str(source_end)
        )
        actual_target = _fin_summary_range_count(
            connection, schema="main", start=str(target_start), end=str(target_end)
        )
        if actual_source != source_count or actual_target != target_count:
            raise MergeError(
                "jquants_fin_summaries coverage count does not match stored rows for " + key
            )


def _require_fin_summary_coverage_states(connection: sqlite3.Connection, *, schema: str) -> None:
    """Classify every financial-summary claim so no hybrid state bypasses validation."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count, "  # nosec B608
        f'status, error FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end, record_count, status, error in rows:
        key = f"'jquants_fin_summaries', {coverage_key!r}"
        common_valid = (
            start is not None
            and end is not None
            and coverage_key == f"get_fin_summary_range:{start}..{end}"
            and isinstance(record_count, int)
            and record_count >= 0
        )
        state_valid = (status == "ok" and error is None) or (
            status in {"partial", "failed"} and isinstance(error, str) and bool(error.strip())
        )
        if not common_valid or not state_valid:
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")


def _require_fin_summary_coverage_counts(connection: sqlite3.Connection, *, schema: str) -> None:
    """Prove every clean range claim against the facts held by that store."""

    rows = connection.execute(
        f"SELECT coverage_key, coverage_start, coverage_end, record_count "  # nosec B608
        f'FROM {schema_name(schema)}."source_coverage" '
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end, record_count in rows:
        key = f"'jquants_fin_summaries', {coverage_key!r}"
        if (
            start is None
            or end is None
            or coverage_key != f"get_fin_summary_range:{start}..{end}"
            or not isinstance(record_count, int)
            or record_count < 0
        ):
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual = _fin_summary_range_count(connection, schema=schema, start=str(start), end=str(end))
        if actual != record_count:
            raise MergeError(
                "jquants_fin_summaries coverage count does not match stored rows for " + key
            )


def _reconcile_fin_summary_coverage_counts(connection: sqlite3.Connection) -> None:
    """Make clean target claims describe the fact union produced by this transaction."""

    rows = connection.execute(
        "SELECT coverage_key, coverage_start, coverage_end "
        "FROM main.source_coverage "
        "WHERE source = 'jquants_fin_summaries' AND status = 'ok' AND error IS NULL "
        "ORDER BY coverage_key"
    ).fetchall()
    for coverage_key, start, end in rows:
        if start is None or end is None:
            key = f"'jquants_fin_summaries', {coverage_key!r}"
            raise MergeError(f"source_coverage payload disagrees for shared key: {key}")
        actual = _fin_summary_range_count(connection, schema="main", start=str(start), end=str(end))
        connection.execute(
            "UPDATE main.source_coverage SET record_count = ? "
            "WHERE source = 'jquants_fin_summaries' AND coverage_key = ?",
            (actual, coverage_key),
        )


def _fin_summary_range_count(
    connection: sqlite3.Connection, *, schema: str, start: str, end: str
) -> int:
    row = connection.execute(
        f'SELECT count(*) FROM {schema_name(schema)}."jquants_fin_summaries" '  # nosec B608
        "WHERE disclosed_at BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    if row is None:
        raise MergeError("financial-summary coverage count returned no row")
    return int(row[0])


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
