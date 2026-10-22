"""One-shot lossless 47 -> 48 copy; never called by readers or daily jobs.

Remove after the attended production cutover. The source remains read-only;
publication uses the existing lease, backup and conditional machine-store push.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.external_assessment.observations.contract import ANALYST_FORECAST_DDL
from baibai_engine.storage.observations import schema


def prior_schema_sql() -> str:
    if schema.SQLITE_SCHEMA_VERSION != 48:
        raise ValueError("this one-shot tool only targets market schema 48")
    return schema._SCHEMA_SQL.replace(ANALYST_FORECAST_DDL, "").replace(
        "'monex', 'rakuten', 'kabumap', 'minkabu'", "'monex', 'rakuten', 'kabumap'"
    )


def _objects(connection: sqlite3.Connection, database: str) -> list[tuple[object, ...]]:
    if database not in {"main", "prior"}:
        raise ValueError("unexpected database")
    return connection.execute(
        f"SELECT type, name, tbl_name, sql FROM {database}.sqlite_master "  # nosec B608
        "WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()


def upgrade(source: Path, output: Path) -> dict[str, int]:
    if not source.is_file():
        raise FileNotFoundError(source)
    # Exclusive creation prevents a failed retry from replacing a previous candidate.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(":memory:")) as expected:
            expected.executescript(prior_schema_sql())
            old_objects = _objects(expected, "main")
        schema.open_connection(output).close()
        with closing(sqlite3.connect(output, uri=True)) as connection:
            connection.execute(
                "ATTACH DATABASE ? AS prior", (source.resolve().as_uri() + "?mode=ro",)
            )
            connection.execute("BEGIN")
            if connection.execute("PRAGMA prior.user_version").fetchone() != (47,):
                raise ValueError(
                    "source must be market schema 47; do not retry a completed cutover"
                )
            if _objects(connection, "prior") != old_objects:
                raise ValueError("source schema differs from the reviewed schema 47")
            if connection.execute("PRAGMA prior.quick_check").fetchone() != ("ok",):
                raise ValueError("source integrity check failed")
            counts: dict[str, int] = {}
            for kind, raw_name, _, _ in old_objects:
                if kind != "table":
                    continue
                # Names come from the exact reviewed schema, never from user SQL.
                name = str(raw_name)
                columns = [
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')
                ]
                names = ",".join(f'"{column}"' for column in columns)
                connection.execute(
                    f'INSERT INTO main."{name}" ({names}) SELECT {names} FROM prior."{name}"'  # nosec B608
                )
                source_count = connection.execute(
                    f'SELECT COUNT(*) FROM prior."{name}"'  # nosec B608
                ).fetchone()[0]  # nosec B608
                target_count = connection.execute(
                    f'SELECT COUNT(*) FROM main."{name}"'  # nosec B608
                ).fetchone()[0]
                for first, second in (("main", "prior"), ("prior", "main")):
                    difference = connection.execute(
                        f'SELECT {names} FROM {first}."{name}" EXCEPT '  # nosec B608
                        f'SELECT {names} FROM {second}."{name}" LIMIT 1'  # nosec B608
                    ).fetchone()
                    if difference is not None or source_count != target_count:
                        raise ValueError(f"copied row mismatch: {name}")
                counts[name] = int(target_count)
            if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
                raise ValueError("copied foreign keys are inconsistent")
            if connection.execute("PRAGMA main.quick_check").fetchone() != ("ok",):
                raise ValueError("candidate integrity check failed")
            schema.validate_current_schema(connection)
            connection.commit()
        return counts
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counts = upgrade(args.source, args.output)
    print(
        json.dumps({"from_schema": 47, "to_schema": 48, "preserved_rows": counts}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
