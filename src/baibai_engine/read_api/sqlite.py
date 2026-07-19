"""Read-only SQLite connection helpers for application-facing stores."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


__all__ = ["connect_read_only"]
