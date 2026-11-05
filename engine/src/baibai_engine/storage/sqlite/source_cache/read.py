"""Fail-close read connections keep obsolete market stores out of judgment views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.storage.sqlite.read import connect_read_only as connect_sqlite_read_only
from baibai_engine.storage.sqlite.read import written_schema_version
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an unwritten or current market store without initializing it."""

    connection = connect_sqlite_read_only(path)
    try:
        if written_schema_version(connection) is not None:
            validate_current_schema(connection)
        return connection
    except BaseException:
        connection.close()
        raise


__all__ = ["connect_read_only"]
