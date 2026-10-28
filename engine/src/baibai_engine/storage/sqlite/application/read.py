"""Read-only SQLite connections for domain queries and application views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.schema import APPLICATION_SCHEMA_VERSION


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open an unwritten or current application store without mutating it."""

    resolved = database_path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
        has_tables = (
            connection.execute(
                "SELECT 1 FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            is not None
        )
        if version == 0 and not has_tables:
            return connection
        if version != APPLICATION_SCHEMA_VERSION:
            raise RuntimeError(
                "obsolete application database schema; publish a store cut over by the "
                "matching application release "
                f"(found user_version={version}, expected={APPLICATION_SCHEMA_VERSION}): "
                f"{resolved}"
            )
        return connection
    except Exception:
        connection.close()
        raise


__all__ = ["connect_read_only"]
