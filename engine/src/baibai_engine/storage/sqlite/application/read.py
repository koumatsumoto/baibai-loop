"""Read-only SQLite connections for domain queries and application views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.schema import SUPPORTED_APPLICATION_SCHEMA_VERSIONS
from baibai_engine.storage.sqlite.read import connect_read_only as connect_sqlite_read_only
from baibai_engine.storage.sqlite.read import written_schema_version


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open an unwritten or supported application store without mutating it."""

    resolved = database_path(path).resolve()
    connection = connect_sqlite_read_only(resolved)
    try:
        version = written_schema_version(connection)
        if version is not None and version not in SUPPORTED_APPLICATION_SCHEMA_VERSIONS:
            raise RuntimeError(
                "obsolete application database schema; publish a store cut over by the "
                "matching application release "
                f"(found user_version={version}, "
                f"supported={SUPPORTED_APPLICATION_SCHEMA_VERSIONS}): "
                f"{resolved}"
            )
        return connection
    except BaseException:
        connection.close()
        raise


__all__ = ["connect_read_only"]
