"""Read-only preconditions used before publishing Web projections."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.appdb.read import connect_read_only as connect_application_read_only
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.rules import ReadingRulesError, load_reading_rules
from baibai_engine.market.sqlite.read import connect_read_only as connect_market_read_only
from baibai_engine.market.sqlite.schema import SQLiteSchemaError

from .sqlite import is_missing_table_error


class MaterializationPreconditionError(RuntimeError):
    """A source cannot produce a complete, current Web projection."""


def validate_application_store_schema(path: Path) -> None:
    """Require a present application store to use the schema this code reads."""

    if not path.is_file():
        return
    try:
        with closing(connect_application_read_only(path)):
            pass
    except RuntimeError as error:
        raise MaterializationPreconditionError(str(error)) from error


def validate_market_store_schema(path: Path) -> None:
    """Require a present market store to match the complete current read contract."""

    if not path.is_file():
        return
    try:
        with closing(connect_market_read_only(path)):
            pass
    except SQLiteSchemaError as error:
        raise MaterializationPreconditionError(str(error)) from error


def validate_market_store_hydration(path: Path) -> None:
    """Require every lake-owned table to hold rows once its fetch ledger claims some.

    The market store published to the object store is a copy whose lake-owned tables
    were emptied: the rows live in the L1 release and a reader fills them back before
    reading it. Emptying keeps the tables, so every query still answers — with nothing —
    and `source_coverage`, which the lake does not own, travels with the copy still
    claiming the fetches. An export from that copy therefore writes valuations, daily
    deltas and security views that carry no market price at all and read as complete,
    while the ledger-observed fallbacks make them look merely a few days stale.

    The comparison is the merge's, one-directional (see `batch/OPERATIONS.md`,
    "merge が検査するもの"): a claim is only ever raised to the rows a store holds and
    never lowered, so an empty table under a positive claim is the unfilled state, while
    a table holding more than its windows claim is ordinary. Totals are deliberately not
    compared — coverage windows overlap, and on the working store the daily-bars claim
    exceeds the rows by 4.9M for that reason alone.
    """

    if not path.is_file():
        return
    # Imported here rather than at module scope: the lake package pulls Arrow and DuckDB
    # in, and `read_api` is imported by every read-only consumer including the local API
    # server, while this validator runs once per export.
    from baibai_engine.market.lake.datasets import LAKE_DATASETS

    uri = f"{path.resolve().as_uri()}?mode=ro"
    unfilled: list[str] = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        try:
            claims = {
                str(source): int(total or 0)
                for source, total in connection.execute(
                    "SELECT source, SUM(record_count) FROM source_coverage "
                    "WHERE status = 'ok' GROUP BY source"
                )
            }
        except sqlite3.OperationalError as error:
            # A store with no fetch ledger has claimed nothing, so there is nothing this
            # check can compare. Views over an unwritten store render empty. Anything else — a
            # renamed column, a malformed query — keeps raising rather than reading as
            # "nothing claimed", which is the shape a hole in this check would take.
            if not is_missing_table_error(error):
                raise
            return
        for name, dataset in sorted(LAKE_DATASETS.items()):
            claimed = claims.get(dataset.coverage_source_name, 0)
            if claimed <= 0:
                continue
            try:
                held = bool(
                    connection.execute(
                        f"SELECT EXISTS(SELECT 1 FROM {dataset.sqlite_table})"  # nosec B608
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError as error:
                if not is_missing_table_error(error):
                    raise
                unfilled.append(f"{name} claims {claimed} row(s) and carries no table")
                continue
            if not held:
                unfilled.append(f"{name} claims {claimed} row(s) and holds none")
    if unfilled:
        raise MaterializationPreconditionError(
            "market store is not hydrated: " + "; ".join(unfilled) + f" ({path}). "
            "Fill it from the current L1 release before exporting "
            "(batch/scripts/r2_transfer.sh hydrate-market)"
        )


def validate_macro_reading_rules(path: Path) -> None:
    """Require the trusted reading rules to resolve every registered series."""

    try:
        rules = load_reading_rules(path)
        for definition in load_definitions().series:
            rules.resolve(series_id=definition.series_id, frequency=definition.frequency)
    except ReadingRulesError as error:
        raise MaterializationPreconditionError(str(error)) from error


__all__ = [
    "MaterializationPreconditionError",
    "validate_application_store_schema",
    "validate_macro_reading_rules",
    "validate_market_store_hydration",
    "validate_market_store_schema",
]
