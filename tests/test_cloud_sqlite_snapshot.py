from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tools.cloud.sqlite_snapshot import (
    create_snapshot,
    database_schema_version,
    validate_database,
)


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


def test_v13_rollback_snapshot_preserves_edinet_rows_and_schema(tmp_path: Path) -> None:
    source = tmp_path / "market-v13.sqlite"
    restored = tmp_path / "restored.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA user_version = 13")
        connection.execute(
            "CREATE TABLE edinet_documents (doc_date TEXT NOT NULL, doc_id TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO edinet_documents VALUES ('2026-07-10', 'S100KEPT')")
        connection.commit()

    create_snapshot(source, restored)

    assert database_schema_version(restored) == 13
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT doc_date, doc_id FROM edinet_documents").fetchall() == [
            ("2026-07-10", "S100KEPT")
        ]
