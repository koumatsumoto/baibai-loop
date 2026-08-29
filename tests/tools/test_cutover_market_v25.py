from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tools.migrations.cutover_market_v25 import (
    CURRENT_EARNINGS_TABLE,
    REMOVED_TABLE,
    SOURCE_EARNINGS_TABLE,
    cutover,
)

from baibai_engine.market.sqlite import open_connection


def _v24_store(path: Path) -> Path:
    open_connection(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            f'ALTER TABLE "{CURRENT_EARNINGS_TABLE}" RENAME TO "{SOURCE_EARNINGS_TABLE}"'
        )
        connection.execute(
            f'INSERT INTO "{SOURCE_EARNINGS_TABLE}" VALUES (?, ?)',
            ("2026-09-01", "2331"),
        )
        connection.execute(
            f'CREATE TABLE "{REMOVED_TABLE}" (doc_id TEXT PRIMARY KEY, payload TEXT) STRICT'
        )
        connection.execute(f'INSERT INTO "{REMOVED_TABLE}" VALUES (?, ?)', ("doc-1", "retired"))
        connection.execute("PRAGMA user_version = 24")
    return path


def test_cutover_drops_only_the_retired_table(tmp_path: Path) -> None:
    store = _v24_store(tmp_path / "market.sqlite")
    with sqlite3.connect(store) as connection:
        connection.execute(
            "INSERT INTO jquants_master_snapshots (snapshot_date, ticker, name) VALUES (?, ?, ?)",
            ("2026-08-14", "2331", "ALSOK"),
        )

    counts = cutover(store)

    assert counts["jquants_master_snapshots"] == 1
    assert counts[CURRENT_EARNINGS_TABLE] == 1
    with sqlite3.connect(store) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                (REMOVED_TABLE,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM jquants_master_snapshots").fetchone()[0] == 1
        )
        assert (
            connection.execute(f'SELECT count(*) FROM "{CURRENT_EARNINGS_TABLE}"').fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cutover_leaves_a_current_store_unchanged(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    open_connection(store).close()
    before = store.read_bytes()

    cutover(store)

    assert store.read_bytes() == before


def test_cutover_rejects_an_unexpected_version(tmp_path: Path) -> None:
    store = _v24_store(tmp_path / "market.sqlite")
    with sqlite3.connect(store) as connection:
        connection.execute("PRAGMA user_version = 23")

    with pytest.raises(ValueError, match="requires schema 24"):
        cutover(store)
