"""ローカルschema 50 copyから新しいschema 51 copyを作る。一回限りの明示移行。

原本・本番・remoteは更新しない。承認済みcutoverの完了後、このtoolと専用testを撤去する。
旧release用のQUANTS_SCORE_SCHEMA50_COLUMNSは別の読取契約として保持する。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_DDL,
    QUANTS_SCORE_SCHEMA50_COLUMNS,
    QUANTS_SCORE_TABLE,
    _ddl,
)
from baibai_engine.storage.sqlite.source_cache import schema


def _schema50_sql() -> str:
    if schema.SQLITE_SCHEMA_VERSION != 51 or schema._SCHEMA_SQL.count(QUANTS_SCORE_DDL) != 1:
        raise ValueError("this one-time cutover is only for market schema 50 to 51")
    return schema._SCHEMA_SQL.replace(
        QUANTS_SCORE_DDL, _ddl(QUANTS_SCORE_TABLE, QUANTS_SCORE_SCHEMA50_COLUMNS), 1
    )


def _validate_schema50(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != 50:
        raise ValueError("source must have market schema 50")
    expected_tables, expected_indexes = schema._expected_schema_shape(_schema50_sql())
    for table, columns in expected_tables.items():
        if schema._table_info(conn, table) != columns:
            raise ValueError(f"source schema-50 table shape mismatch: {table}")
    for name, expected_index in expected_indexes.items():
        if schema._index_info(conn, expected_index[0], name) != expected_index:
            raise ValueError(f"source schema-50 index shape mismatch: {name}")
    # Rebuilding must not silently discard an unexpected CHECK/default/foreign key.
    with closing(sqlite3.connect(":memory:")) as expected_schema:
        expected_schema.executescript(_schema50_sql())
        query = "SELECT sql FROM sqlite_schema WHERE name=?"
        for name in (QUANTS_SCORE_TABLE, *expected_indexes):
            if (
                conn.execute(query, (name,)).fetchone()
                != expected_schema.execute(query, (name,)).fetchone()
            ):
                raise ValueError(f"source schema-50 definition mismatch: {name}")
    _validate_integrity(conn)


def _validate_integrity(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("market SQLite integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("market SQLite foreign key check failed")


def _rebuild_quants(conn: sqlite3.Connection) -> None:
    replacement = f"{QUANTS_SCORE_TABLE}_schema51"
    dependent_sql = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name=? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name",
        (QUANTS_SCORE_TABLE,),
    ).fetchall()
    # Do not rename the old table: SQLite would rewrite views/foreign keys pointing
    # to it. Legacy rename also leaves dependent views valid across the brief gap.
    create_table = _ddl(replacement, QUANTS_SCORE_COLUMNS).split(";", 1)[0]
    conn.execute(create_table.replace("IF NOT EXISTS ", "", 1))
    old_names = ",".join(column[0] for column in QUANTS_SCORE_SCHEMA50_COLUMNS)
    conn.execute(
        f"INSERT INTO {replacement}(rowid,{old_names},availability) "  # nosec B608
        f"SELECT rowid,{old_names},'available' FROM {QUANTS_SCORE_TABLE}"
    )
    for left, right in ((QUANTS_SCORE_TABLE, replacement), (replacement, QUANTS_SCORE_TABLE)):
        if (
            conn.execute(
                f"SELECT rowid,{old_names} FROM {left} "  # nosec B608
                f"EXCEPT SELECT rowid,{old_names} FROM {right}"
            ).fetchone()
            is not None
        ):
            raise ValueError("quants rows changed during copy")
    conn.execute(f"DROP TABLE {QUANTS_SCORE_TABLE}")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO {QUANTS_SCORE_TABLE}")
    for (statement,) in dependent_sql:
        conn.execute(statement)


def copy_with_kabumap_uncomputed(source: Path, destination: Path) -> None:
    """Preserve the source; publish only a fully validated, new local destination."""
    _schema50_sql()
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing local SQLite file")
    destination = destination.parent.resolve(strict=True) / destination.name
    if source == destination:
        raise ValueError("source and destination must differ")
    for path in (
        destination,
        *(Path(str(destination) + suffix) for suffix in ("-journal", "-wal", "-shm")),
    ):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"destination or SQLite sidecar already exists: {path}")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("BEGIN")
        _validate_schema50(original)
        with TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as work:
            temporary = Path(work) / "market.sqlite"
            with closing(sqlite3.connect(temporary)) as target:
                original.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
                target.execute("PRAGMA foreign_keys=OFF")
                target.execute("PRAGMA legacy_alter_table=ON")
                with target:
                    target.execute("BEGIN IMMEDIATE")
                    _rebuild_quants(target)
                    target.execute("PRAGMA user_version=51")
                    _validate_integrity(target)
                    schema.validate_current_schema(target)
                target.execute("PRAGMA legacy_alter_table=OFF")
            # Atomic no-clobber publication on the same filesystem. A failed check
            # leaves only the disposable private copy; even a racing output wins.
            with temporary.open("rb") as complete:
                os.fsync(complete.fileno())
            os.link(temporary, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    copy_with_kabumap_uncomputed(args.source, args.destination)


if __name__ == "__main__":
    main()
