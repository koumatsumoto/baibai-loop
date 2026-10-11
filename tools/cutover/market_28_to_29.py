"""Temporary attended schema-28 cutover; remove after Issue1407 delivery completes.

Only the two retail observation tables/indexes and user_version change. No network,
runtime migration, original-store write, or existing-output replacement is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.market.retail_dynamics.contract import (
    ANALYST_RATING_DDL,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_DDL,
    MODEL_VALUATION_TABLE,
)
from baibai_engine.market.sqlite import schema

SOURCE_VERSION = 28
TARGET_VERSION = 29
_NEW_TABLES = (MODEL_VALUATION_TABLE, ANALYST_RATING_TABLE)
type Catalog = dict[tuple[str, str], tuple[str, str | None]]


@dataclass(frozen=True)
class TableEvidence:
    rows: int
    sha256: str


@dataclass(frozen=True)
class CutoverResult:
    source_version: int
    output_version: int
    old_schema_sha256: str
    preserved_tables: dict[str, TableEvidence]
    all_old_rows_equal: bool
    all_old_schema_equal: bool
    integrity_ok: bool
    foreign_keys_ok: bool


def _quote(name: str) -> str:
    # Identifiers come only from the exact validated schema catalog or constants;
    # no source row values or CLI text is interpolated into these statements.
    return '"' + name.replace('"', '""') + '"'


def _catalog(connection: sqlite3.Connection) -> Catalog:
    return {
        (str(kind), str(name)): (str(table), str(sql) if sql is not None else None)
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        )
    }


def _source28_catalog() -> Catalog:
    # This PR adds only these tables/indexes to schema 28. Avoid embedding a second
    # copy of the unchanged 22-table DDL in a tool deleted after attended cutover.
    if schema.SQLITE_SCHEMA_VERSION != TARGET_VERSION:
        raise schema.SQLiteSchemaError("temporary cutover requires code schema 29")
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(schema._SCHEMA_SQL)
        for table in _NEW_TABLES:
            expected.execute(f"DROP TABLE {_quote(table)}")
        return _catalog(expected)
    finally:
        expected.close()


def _schema_tokens(catalog: Catalog) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    # Old attended cutovers/ALTERs retain different SQL indentation and comments.
    # Preserve every SQL token (including quoted literal contents), ignoring only
    # whitespace/comments so changed CHECK/default/key/index definitions still fail.
    token = re.compile(
        r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*[\s\S]*?\*/"
        r"|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|[^\s]"
    )
    normalized: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    for key, (table, sql) in catalog.items():
        pieces = [piece for piece in token.findall(sql or "") if not piece.startswith(("--", "/*"))]
        # SQLite ALTER TABLE can quote the declared object name. Only this known
        # declaration position is equivalent; quoted expression contents stay exact.
        if len(pieces) > 2 and pieces[2] == f'"{key[1]}"':
            pieces[2] = key[1]
        normalized[key] = (
            table,
            tuple(piece if piece.startswith(("'", '"')) else piece.casefold() for piece in pieces),
        )
    return normalized


def _check_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise schema.SQLiteSchemaError("market SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise schema.SQLiteSchemaError("market SQLite foreign key check failed")


def _table_evidence(connection: sqlite3.Connection, table: str) -> TableEvidence:
    columns = connection.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    ordered = ",".join(_quote(str(row[0])) for row in columns)
    digest, rows = hashlib.sha256(), 0
    for row in connection.execute(
        f"SELECT * FROM {_quote(table)} ORDER BY {ordered}"  # nosec B608
    ):
        # SQLite's value types have deterministic repr. This handles BLOB as well
        # as numeric/text/null without exposing source values in the report.
        digest.update((repr(tuple(row)) + "\n").encode())
        rows += 1
    return TableEvidence(rows=rows, sha256=digest.hexdigest())


def _require_absent_output(output: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(output) + suffix)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        raise FileExistsError(f"cutover output prefix already exists: {candidate}")


def migrate_market_28_to_29(source: Path, output: Path) -> CutoverResult:
    """Validate/convert privately, then install a standalone file without overwrite."""
    source = source.resolve()
    if source == output.resolve():
        raise ValueError("source and output must be separate files")
    _require_absent_output(output)
    output = output.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source market store does not exist: {source}")
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        original.execute("PRAGMA query_only = ON")
        original.execute("BEGIN")
        version = int(original.execute("PRAGMA user_version").fetchone()[0])
        if version != SOURCE_VERSION:
            raise schema.SQLiteSchemaError("cutover source must be exactly schema 28")
        old_catalog = _catalog(original)
        if _schema_tokens(old_catalog) != _schema_tokens(_source28_catalog()):
            raise schema.SQLiteSchemaError("cutover source differs from exact schema 28")
        _check_integrity(original)
        old_tables = sorted(name for kind, name in old_catalog if kind == "table")
        before = {table: _table_evidence(original, table) for table in old_tables}
        # SQLite never opens the final prefix. It can only create/delete sidecars
        # in this owned directory; failures clean only that private directory.
        with TemporaryDirectory(prefix=".market-28-to-29-", dir=output.parent) as temporary:
            staged = Path(temporary) / "market.sqlite"
            descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(staged.as_uri() + "?mode=rw", uri=True)
            try:
                original.backup(destination)
                # Seal a single portable main file, including any committed WAL
                # content from the source. This changes only the copy's journal mode.
                if destination.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise schema.SQLiteSchemaError("cutover could not seal a standalone copy")
                destination.executescript(
                    "BEGIN IMMEDIATE;"
                    + MODEL_VALUATION_DDL
                    + ANALYST_RATING_DDL
                    + f"PRAGMA user_version = {TARGET_VERSION};COMMIT;"
                )
                schema.validate_current_schema(destination)
                _check_integrity(destination)
                after_catalog = {
                    key: value
                    for key, value in _catalog(destination).items()
                    if value[0] not in _NEW_TABLES
                }
                after = {table: _table_evidence(destination, table) for table in old_tables}
                if after_catalog != old_catalog or after != before:
                    raise schema.SQLiteSchemaError("cutover changed retained market schema or rows")
                for table in _NEW_TABLES:
                    if (
                        destination.execute(
                            f"SELECT COUNT(*) FROM {_quote(table)}"  # nosec B608
                        ).fetchone()[0]
                        != 0
                    ):
                        raise schema.SQLiteSchemaError("cutover retail tables must start empty")
                result = CutoverResult(
                    source_version=version,
                    output_version=int(destination.execute("PRAGMA user_version").fetchone()[0]),
                    old_schema_sha256=hashlib.sha256(
                        repr(sorted(old_catalog.items())).encode()
                    ).hexdigest(),
                    preserved_tables=before,
                    all_old_rows_equal=True,
                    all_old_schema_equal=True,
                    integrity_ok=True,
                    foreign_keys_ok=True,
                )
            finally:
                destination.close()
            _require_absent_output(output)
            # Same filesystem: hardlink installation is atomic and refuses an
            # existing/dangling-symlink destination even if created after the check.
            os.link(staged, output)
    finally:
        original.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="一時的なmarket SQLite 28→29隔離cutover")
    parser.add_argument("--source", type=Path, required=True, help="schema 28の取得済み作業コピー")
    parser.add_argument("--output", type=Path, required=True, help="存在しない別出力ファイル")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = migrate_market_28_to_29(args.source, args.output)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
