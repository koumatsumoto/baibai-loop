"""Fail-close read connections keep obsolete market stores out of judgment views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import validate_current_schema


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an unwritten or current market store without initializing it."""

    resolved = path.resolve()
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
        validate_current_schema(connection)
        return connection
    except Exception:
        connection.close()
        raise


__all__ = ["connect_read_only"]
