"""Shared read-only access to the application DB for every read_api query."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Protocol

from baibai_engine.appdb.read import connect_read_only as connect_application_read_only

__all__ = ["is_unwritten_store", "read_rows"]


class _Connector(Protocol):
    def __call__(self, path: Path) -> sqlite3.Connection: ...


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an arbitrary SQLite store query-only; domain owners add schema checks."""

    resolved = path.resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def is_unwritten_store(error: sqlite3.OperationalError) -> bool:
    """Tell a missing-table error from another SQLite query failure.

    The caller separately proves that the whole database is unwritten. A missing table
    in a populated database is an incomplete current schema and must keep raising.
    """

    return "no such table" in str(error)


def _database_is_unwritten(path: Path) -> bool:
    with closing(connect_read_only(path)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
    return version == 0 and table is None


def read_rows(
    path: Path,
    sql: str,
    parameters: Sequence[object] = (),
    *,
    connector: _Connector = connect_read_only,
) -> list[sqlite3.Row]:
    """Run one query, reading an absent file or an unwritten table as no rows."""

    if not path.is_file():
        return []
    try:
        with closing(connector(path)) as connection:
            return connection.execute(sql, tuple(parameters)).fetchall()
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error) or not _database_is_unwritten(path):
            raise
        return []


def read_application_rows(
    path: Path, sql: str, parameters: Sequence[object] = ()
) -> list[sqlite3.Row]:
    return read_rows(path, sql, parameters, connector=connect_application_read_only)
