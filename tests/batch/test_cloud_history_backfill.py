from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from baibai_batch.jobs.history_backfill import run_history_backfill


def _store(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE chunks (name TEXT PRIMARY KEY)")


def _is_upload(command: Sequence[str]) -> bool:
    return command[-1] == "push-market"


def test_history_failure_uploads_committed_partial_progress_then_fails(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> int:
        commands.append(tuple(command))
        if _is_upload(command):
            return 0
        with sqlite3.connect(store) as connection:
            connection.execute("INSERT INTO chunks VALUES ('daily-bars')")
        return 1

    result = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )

    assert result == 1
    assert _is_upload(commands[-1])
    with sqlite3.connect(store) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_master_failure_uploads_dates_committed_after_history_success(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> int:
        commands.append(tuple(command))
        if _is_upload(command):
            return 0
        if "backfill-master" in command:
            with sqlite3.connect(store) as connection:
                connection.execute("INSERT INTO chunks VALUES ('master-2024-01-31')")
            return 1
        return 0

    result = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from="2024-01-01",
        sqlite_path=store,
        runner=runner,
    )

    assert result == 1
    assert any("backfill-history" in command for command in commands)
    assert any("backfill-master" in command for command in commands)
    assert _is_upload(commands[-1])


def test_unchanged_failure_skips_large_store_upload(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> int:
        commands.append(tuple(command))
        return 1

    result = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )

    assert result == 1
    assert not any(_is_upload(command) for command in commands)


def test_redispatch_reuses_persisted_chunk_without_fetch_or_upload(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)
    fetches = 0
    uploads = 0

    def runner(command: Sequence[str]) -> int:
        nonlocal fetches, uploads
        if _is_upload(command):
            uploads += 1
            return 0
        with sqlite3.connect(store) as connection:
            cached = connection.execute(
                "SELECT 1 FROM chunks WHERE name = 'fin-summaries'"
            ).fetchone()
            if cached is None:
                fetches += 1
                connection.execute("INSERT INTO chunks VALUES ('fin-summaries')")
                return 1
        return 0

    first = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )
    second = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )

    assert first == 1
    assert second == 0
    assert fetches == 1
    assert uploads == 1


def test_corrupt_partial_store_is_not_uploaded(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)
    uploads = 0

    def runner(command: Sequence[str]) -> int:
        nonlocal uploads
        if _is_upload(command):
            uploads += 1
            return 0
        store.write_bytes(b"not sqlite")
        return 1

    with pytest.raises(sqlite3.DatabaseError):
        run_history_backfill(
            start="2024-01-01",
            end="2024-12-31",
            master_month_end_from=None,
            sqlite_path=store,
            runner=runner,
        )

    assert uploads == 0


def test_source_failure_code_is_preserved_when_upload_also_fails(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)

    def runner(command: Sequence[str]) -> int:
        if _is_upload(command):
            return 23
        with sqlite3.connect(store) as connection:
            connection.execute("INSERT INTO chunks VALUES ('daily-bars')")
        return 7

    result = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )

    assert result == 7


def test_upload_failure_is_returned_after_successful_backfill(tmp_path: Path) -> None:
    store = tmp_path / "market.sqlite"
    _store(store)

    def runner(command: Sequence[str]) -> int:
        if _is_upload(command):
            return 23
        with sqlite3.connect(store) as connection:
            connection.execute("INSERT INTO chunks VALUES ('daily-bars')")
        return 0

    result = run_history_backfill(
        start="2024-01-01",
        end="2024-12-31",
        master_month_end_from=None,
        sqlite_path=store,
        runner=runner,
    )

    assert result == 23
