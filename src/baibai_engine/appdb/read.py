"""Read-only SQLite connections for domain queries and application views."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import database_path


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    resolved = database_path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


__all__ = ["connect_read_only"]
