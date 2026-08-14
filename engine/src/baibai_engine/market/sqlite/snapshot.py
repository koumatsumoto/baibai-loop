"""Create one transactionally consistent immutable SQLite snapshot."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_snapshot(source: Path, output: Path) -> None:
    """Use SQLite backup so committed WAL pages belong to the captured generation."""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"SQLite snapshot output already exists: {output}")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with (
            sqlite3.connect(source_uri, uri=True, timeout=30) as source_db,
            sqlite3.connect(output, timeout=30) as target_db,
        ):
            source_db.backup(target_db, pages=1024, sleep=0.05)
        validate_snapshot(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def validate_snapshot(path: Path) -> int:
    """Return schema version after integrity validation of a sealed snapshot."""
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {path}")
        version = connection.execute("PRAGMA user_version").fetchone()
    if version is None:
        raise sqlite3.DatabaseError(f"SQLite user_version is unavailable: {path}")
    return int(version[0])
