"""Report the current market-store read failure without probing or rebuilding it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, connect_current


def unreadable_store_reason(sqlite_path: Path) -> str | None:
    """Return the actual read failure for command diagnostics, or None when readable."""
    try:
        conn = connect_current(sqlite_path)
    except (SQLiteSchemaError, sqlite3.Error) as exc:
        return f"SQLite store {sqlite_path} cannot be read: {exc}"
    if conn is None:
        return f"SQLite store not found: {sqlite_path}"
    conn.close()
    return None
