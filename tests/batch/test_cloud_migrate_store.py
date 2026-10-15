from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from baibai_batch.storage.migrate_store import main, migrate_indicator_store
from baibai_engine.market_context.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    open_connection,
)


def _store(path: Path) -> Path:
    open_connection(path).close()
    return path


def _version(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def test_a_copy_already_current_is_reported_and_left_alone(tmp_path: Path) -> None:
    """Every push runs this, including the ones where R2 is already up to date."""
    store = _store(tmp_path / "indicators.sqlite")
    with closing(sqlite3.connect(store)) as connection:
        before = connection.execute("SELECT count(*) FROM series").fetchone()[0]

    assert migrate_indicator_store(store) == SQLITE_SCHEMA_VERSION
    with closing(sqlite3.connect(store)) as connection:
        assert connection.execute("SELECT count(*) FROM series").fetchone()[0] == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_copy_too_old_for_the_migration_path_is_refused(tmp_path: Path) -> None:
    """The step moves a copy forward; it does not rebuild one the path cannot reach."""
    store = _store(tmp_path / "indicators.sqlite")
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
        main(["--path", str(absent)])

    assert not absent.exists()


def test_cli_migrates_the_indicator_copy_without_a_redundant_store_selector(tmp_path, capsys):
    store = _store(tmp_path / "indicators.sqlite")
    assert main(["--path", str(store)]) == 0
    assert f"migrated indicator copy to schema {SQLITE_SCHEMA_VERSION}" in capsys.readouterr().out
