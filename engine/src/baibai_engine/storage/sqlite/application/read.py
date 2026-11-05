"""Read-only SQLite connections for domain queries and application views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION
from baibai_engine.storage.sqlite.read import connect_read_only as connect_sqlite_read_only
from baibai_engine.storage.sqlite.read import written_schema_version


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open an unwritten or current application store without mutating it."""

    resolved = database_path(path).resolve()
    connection = connect_sqlite_read_only(resolved)
    try:
        version = written_schema_version(connection)
        if version is not None and version != APPLICATION_SCHEMA_VERSION:
            raise RuntimeError(
                "obsolete application database schema; publish a store cut over by the "
                "matching application release "
                f"(found user_version={version}, expected={APPLICATION_SCHEMA_VERSION}): "
                f"{resolved}"
            )
        return connection
    except BaseException:
        connection.close()
        raise


__all__ = ["connect_read_only"]
