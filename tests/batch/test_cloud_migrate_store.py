from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_batch.storage.migrate_store import (
    MARKET_SOURCE_SCHEMA_VERSION,
    MARKET_V27_COLUMNS,
    main,
    migrate_indicator_store,
    migrate_market_store,
)
from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    open_connection,
)
from baibai_engine.market.sqlite import SQLITE_SCHEMA_VERSION as MARKET_SCHEMA_VERSION


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
    store = _store(tmp_path / "macro.sqlite")
    with sqlite3.connect(store) as connection:
        before = connection.execute("SELECT count(*) FROM series").fetchone()[0]

    assert migrate_indicator_store(store) == SQLITE_SCHEMA_VERSION
    with sqlite3.connect(store) as connection:
        assert connection.execute("SELECT count(*) FROM series").fetchone()[0] == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_copy_too_old_for_the_migration_path_is_refused(tmp_path: Path) -> None:
    """The step moves a copy forward; it does not rebuild one the path cannot reach."""
    store = _store(tmp_path / "macro.sqlite")
    connection = sqlite3.connect(store)
    try:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 2}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(IndicatorsSchemaError, match="unsupported indicator SQLite schema"):
        migrate_indicator_store(store)

    assert _version(store) == SQLITE_SCHEMA_VERSION - 2


def test_a_path_that_is_not_a_store_is_named_rather_than_creating_one(tmp_path: Path) -> None:
    """Opening a store creates it, so a typo would otherwise publish an empty database."""
    absent = tmp_path / "absent.sqlite"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        main(["--store", "macro", "--path", str(absent)])

    assert not absent.exists()


def test_market_copy_moves_only_schema_26_to_current(tmp_path: Path) -> None:
    from baibai_engine.market.sqlite import open_connection as open_market_connection

    store = tmp_path / "market.sqlite"
    open_market_connection(store).close()
    connection = sqlite3.connect(store)
    try:
        for column in MARKET_V27_COLUMNS:
            connection.execute(f"ALTER TABLE edinet_metrics DROP COLUMN {column}")
        connection.execute(f"PRAGMA user_version = {MARKET_SOURCE_SCHEMA_VERSION}")
        connection.commit()
    finally:
        connection.close()

    assert migrate_market_store(store) == MARKET_SCHEMA_VERSION
    assert _version(store) == MARKET_SCHEMA_VERSION
    with sqlite3.connect(store) as connection:
        actual = {row[1] for row in connection.execute("PRAGMA table_info(edinet_metrics)")}
    assert set(MARKET_V27_COLUMNS) <= actual


def test_current_market_copy_is_validated_and_left_alone(tmp_path: Path) -> None:
    from baibai_engine.market.sqlite import open_connection as open_market_connection

    store = tmp_path / "market.sqlite"
    open_market_connection(store).close()

    assert migrate_market_store(store) == MARKET_SCHEMA_VERSION
    assert _version(store) == MARKET_SCHEMA_VERSION
