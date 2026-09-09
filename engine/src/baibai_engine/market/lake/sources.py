"""L1 exportの入力snapshotを検査し、objectの同一性検証に使うdigestを産む。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite_snapshot(path: Path, *, expected_schema_version: int) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError("SQLite source snapshot failed quick_check")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != expected_schema_version:
            raise ValueError("SQLite source snapshot schema version does not match")
