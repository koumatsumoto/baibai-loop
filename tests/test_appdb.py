from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from baibai_engine.appdb.migrations import LATEST_VERSION, Migration
from baibai_engine.appdb.write import (
    backup_database,
    connect_rw,
    initialize_database,
)
from baibai_engine.foundation.time import JST


def test_init_is_idempotent_and_enables_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"

    assert initialize_database(path) == LATEST_VERSION
    assert initialize_database(path) == LATEST_VERSION

    connection = connect_rw(path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    finally:
        connection.close()


def test_failed_migration_rolls_back_schema_data_and_version(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    migrations = (
        Migration(1, ("CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT",)),
        Migration(
            2,
            (
                "CREATE TABLE transient (id INTEGER PRIMARY KEY) STRICT",
                "INSERT INTO missing_table VALUES (1)",
            ),
        ),
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(path, migrations=migrations)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='transient'"
            ).fetchone()[0]
            == 0
        )


def test_backup_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    writer = connect_rw(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("INSERT INTO app_meta (key, value) VALUES ('k', 'v')")
        target = backup_database(
            path,
            backup_dir=tmp_path / "backups",
            now=datetime(2026, 7, 19, 12, 0, tzinfo=JST),
        )
    finally:
        writer.close()

    assert target is not None
    with sqlite3.connect(target) as backup:
        assert backup.execute("SELECT value FROM app_meta WHERE key='k'").fetchone()[0] == "v"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []


def test_backup_reports_absent_database(tmp_path: Path) -> None:
    assert backup_database(tmp_path / "missing.sqlite") is None
