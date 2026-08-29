"""Shared read-only access to the application DB for every read_api query."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.appdb.read import connect_read_only as connect_read_only

__all__ = ["connect_read_only", "is_unwritten_store", "read_rows"]


def is_unwritten_store(error: sqlite3.OperationalError) -> bool:
    """Tell "the writer has not created this table here" from a broken query.

    Every table this package reads belongs to the current application schema, so a
    missing table means the writer has not initialized this store — a state the read
    side renders as an empty view rather than a 500 or a halted export. A malformed
    query or a missing column raises the same class of error and must keep raising,
    or a defect in this package would hide behind that silence.
    """

    return "no such table" in str(error)


def read_rows(path: Path, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
    """Run one query, reading an absent file or an unwritten table as no rows."""

    if not path.is_file():
        return []
    try:
        with closing(connect_read_only(path)) as connection:
            return connection.execute(sql, tuple(parameters)).fetchall()
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error):
            raise
        return []
