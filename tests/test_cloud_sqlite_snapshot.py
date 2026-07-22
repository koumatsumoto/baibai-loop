from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tools.cloud.sqlite_snapshot import create_snapshot, validate_database


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE values_for_test (value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_for_test VALUES ('visible')")
        connection.commit()
        create_snapshot(source, output)

    with sqlite3.connect(output) as snapshot:
        assert snapshot.execute("SELECT value FROM values_for_test").fetchone() == ("visible",)


def test_validate_database_rejects_non_sqlite_file(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError):
        validate_database(invalid)
