from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from baibai_engine.appdb import LATEST_VERSION
from baibai_engine.appdb.write import backup_database, initialize_database, prune_backups


def test_initialize_creates_only_the_current_application_schema(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"

    assert initialize_database(path) == LATEST_VERSION
    assert initialize_database(path) == LATEST_VERSION

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        ledger_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ledger_event)").fetchall()
        }
    assert "proposal" not in tables
    assert "decision_reference" in ledger_columns
    assert "proposal_id" not in ledger_columns


def test_obsolete_database_requires_an_explicit_cutover(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old_state (value TEXT)")
        connection.execute(f"PRAGMA user_version = {LATEST_VERSION - 1}")

    with pytest.raises(RuntimeError, match="explicit semantic cutover"):
        initialize_database(path)


def test_backup_captures_current_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO ledger_meta "
            "(singleton, schema_version, portfolio_scope, as_of, payload) "
            "VALUES (1, 1, 'repository_only', '2026-08-29', '{}')"
        )

    backup = backup_database(
        path,
        backup_dir=tmp_path / "backups",
        now=datetime.fromisoformat("2026-08-29T12:00:00+09:00"),
    )

    assert backup is not None
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM ledger_meta").fetchone()[0] == 1


def test_prune_backups_keeps_the_newest_files(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    paths = [backups / f"baibai-2026082{day}T000000000000+0900.sqlite" for day in range(1, 4)]
    for path in paths:
        path.touch()

    assert prune_backups(backups, keep=2) == [paths[0]]
    assert [path.name for path in sorted(backups.iterdir())] == [
        paths[1].name,
        paths[2].name,
    ]
