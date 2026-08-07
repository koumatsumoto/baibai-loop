from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tools.cloud.migrate_store import main, migrate_market_store

from baibai_engine.market.sqlite import SQLiteSchemaError
from baibai_engine.market.sqlite.migrations import BASELINE_VERSION
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
from baibai_engine.screening.sqlite_cache import open_connection


def _store(path: Path) -> Path:
    open_connection(path).close()
    return path


def _version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_a_copy_already_current_is_reported_and_left_alone(tmp_path: Path) -> None:
    """Every push runs this, including the ones where R2 is already up to date."""
    store = _store(tmp_path / "market.sqlite")
    before = store.read_bytes()

    assert migrate_market_store(store) == SQLITE_SCHEMA_VERSION
    assert store.read_bytes() == before


def test_a_copy_too_old_for_the_migration_path_is_refused(tmp_path: Path) -> None:
    """The step moves a copy forward; it does not rebuild one the path cannot reach."""
    store = _store(tmp_path / "market.sqlite")
    connection = sqlite3.connect(store)
    try:
        connection.execute(f"PRAGMA user_version = {BASELINE_VERSION - 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteSchemaError, match="unsupported screening SQLite schema"):
        migrate_market_store(store)

    assert _version(store) == BASELINE_VERSION - 1


def test_a_path_that_is_not_a_store_is_named_rather_than_creating_one(tmp_path: Path) -> None:
    """Opening a store creates it, so a typo would otherwise publish an empty database."""
    absent = tmp_path / "absent.sqlite"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        main(["--store", "market", "--path", str(absent)])

    assert not absent.exists()
