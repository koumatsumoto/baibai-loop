"""Merge source receipts without importing L2 definitions or computed histories."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from baibai_batch.storage.store_merge import (
    MergeError,
    MergeReport,
    merge_fact_tables,
    require_identical_columns,
)
from baibai_engine.batch_api import EconomicSchemaError
from baibai_engine.batch_api import validate_economic_schema as validate_current_schema

FACT_KEYS = {
    "economic_source_records": ("source_id", "observed_at", "fetched_at"),
    "economic_source_documents": ("source_id", "request_key", "fetched_at"),
    "economic_source_runs": ("run_id",),
}


def merge_stores(source: Path, target: Path) -> MergeReport:
    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"economic source store does not exist: {path}")
    with closing(
        sqlite3.connect(target.resolve().as_uri(), uri=True, isolation_level=None)
    ) as connection:
        try:
            validate_current_schema(connection)
            connection.execute(
                "ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",)
            )
            try:
                validate_current_schema(connection, schema="source")
                require_identical_columns(connection, tuple(FACT_KEYS))
                connection.execute("BEGIN IMMEDIATE")
                try:
                    tables = merge_fact_tables(connection, FACT_KEYS)
                    connection.commit()
                    return MergeReport(tables=tables)
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.execute("DETACH DATABASE source")
        except EconomicSchemaError as error:
            raise MergeError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
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
