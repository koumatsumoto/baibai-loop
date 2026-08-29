"""One-time semantic cutover from the market v24 store to the v25 baseline.

The removed EDINET buyback-report cache had no consumer in ranking, FV, E[r], or
research. This operator tool drops only that table, preserves every retained row, and
then advances the store version. It is intentionally outside runtime store opening.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION, validate_current_schema

SOURCE_SCHEMA_VERSION = 24
REMOVED_TABLE = "edinet_buyback_reports"
SOURCE_EARNINGS_TABLE = "jquants_earnings_calendar"
CURRENT_EARNINGS_TABLE = "jpx_earnings_calendar"


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _row_counts(connection: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])  # nosec B608
        for table in sorted(tables)
    }


def cutover(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise FileNotFoundError(f"market SQLite file does not exist: {path}")
    with closing(sqlite3.connect(path)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == SQLITE_SCHEMA_VERSION:
            validate_current_schema(connection)
            return _row_counts(connection, _tables(connection))
        if version != SOURCE_SCHEMA_VERSION:
            raise ValueError(
                f"market cutover requires schema {SOURCE_SCHEMA_VERSION}, found {version}"
            )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"source integrity_check failed: {integrity}")
        tables = _tables(connection)
        if REMOVED_TABLE not in tables:
            raise ValueError(f"schema {SOURCE_SCHEMA_VERSION} is missing {REMOVED_TABLE}")
        if SOURCE_EARNINGS_TABLE not in tables:
            raise ValueError(f"schema {SOURCE_SCHEMA_VERSION} is missing {SOURCE_EARNINGS_TABLE}")
        retained = tables - {REMOVED_TABLE, SOURCE_EARNINGS_TABLE}
        before = _row_counts(connection, retained)
        earnings_count = int(
            connection.execute(
                f'SELECT count(*) FROM "{SOURCE_EARNINGS_TABLE}"'  # nosec B608
            ).fetchone()[0]
        )

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(f'DROP TABLE "{REMOVED_TABLE}"')
            connection.execute(
                f'ALTER TABLE "{SOURCE_EARNINGS_TABLE}" RENAME TO "{CURRENT_EARNINGS_TABLE}"'
            )
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
            validate_current_schema(connection)
            after = _row_counts(connection, retained)
            if after != before:
                raise RuntimeError("retained market row counts changed during cutover")
            current_earnings_count = int(
                connection.execute(
                    f'SELECT count(*) FROM "{CURRENT_EARNINGS_TABLE}"'  # nosec B608
                ).fetchone()[0]
            )
            if current_earnings_count != earnings_count:
                raise RuntimeError("earnings-calendar row count changed during cutover")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {**before, CURRENT_EARNINGS_TABLE: earnings_count}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    counts = cutover(args.path)
    print(
        f"market schema is current at {SQLITE_SCHEMA_VERSION}: "
        f"{args.path} ({sum(counts.values())} retained rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
