"""One-time replacement of the rebuildable screening run store with schema v4.

The run store is cloud-owned machine output. Its v3 rows encode the retired
selection DAG and audit-only columns, and cannot be projected into the single
ranked-set contract without keeping compatibility semantics. This cutover therefore
verifies the source, discards only rebuildable run/selection rows, and creates an
empty current store.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL

SOURCE_SCHEMA_VERSION = 3
_SOURCE_TABLES = (
    "selection_entry",
    "screening_selection",
    "screening_candidate",
    "screening_run",
)
_CURRENT_TABLES = ("screening_selection", "screening_candidate", "screening_run")


class RunStoreCutoverError(RuntimeError):
    """The cloud-owned cache is not the exact obsolete store this tool replaces."""


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def cutover(path: Path) -> dict[str, int]:
    """Replace an exact v3 run cache in place and return the discarded row counts."""
    if not path.is_file():
        raise FileNotFoundError(f"screening run store does not exist: {path}")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SOURCE_SCHEMA_VERSION:
            raise RunStoreCutoverError(
                f"run-store cutover requires schema {SOURCE_SCHEMA_VERSION}, found {version}"
            )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"source integrity_check failed: {integrity}")
        tables = _table_names(connection)
        if tables != set(_SOURCE_TABLES):
            raise RunStoreCutoverError(
                f"run-store schema has unexpected tables: {sorted(tables)!r}"
            )
        discarded = {
            # ``table`` comes from the module-owned tuple, not operator input.
            table: int(
                connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # nosec B608
            )
            for table in _SOURCE_TABLES
        }
        script = ["BEGIN IMMEDIATE;"]
        script.extend(f'DROP TABLE "{table}";' for table in _SOURCE_TABLES)
        script.append(SCHEMA_SQL)
        script.append(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION};")
        try:
            connection.executescript("\n".join(script))
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RunStoreCutoverError("current run store failed foreign_key_check")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RunStoreCutoverError("current run store failed integrity_check")
            if _table_names(connection) != set(_CURRENT_TABLES):
                raise RunStoreCutoverError("current run store table set is incomplete")
            if any(
                connection.execute(  # ``table`` is the fixed _TABLES allowlist.
                    f'SELECT 1 FROM "{table}" LIMIT 1'  # nosec B608
                ).fetchone()
                for table in _CURRENT_TABLES
            ):
                raise RunStoreCutoverError("current run store is not empty")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return discarded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    discarded = cutover(args.path)
    print(
        json.dumps(
            {"schema_version": RUN_STORE_SCHEMA_VERSION, "discarded_rows": discarded},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
