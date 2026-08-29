from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tools.migrations.cutover_runs_v4 import RunStoreCutoverError, cutover

from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL


def _v3_store(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("ALTER TABLE screening_run ADD COLUMN application_git_commit TEXT")
        connection.execute("ALTER TABLE screening_selection ADD COLUMN application_git_commit TEXT")
        connection.executescript(
            """
            CREATE TABLE selection_entry (
                selection_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                selected INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (selection_id, ordinal)
            ) STRICT, WITHOUT ROWID;
            """
        )
        connection.execute("ALTER TABLE screening_selection ADD COLUMN profile TEXT")
        connection.execute("ALTER TABLE screening_selection ADD COLUMN publication_kind TEXT")
        connection.execute("ALTER TABLE screening_selection ADD COLUMN source_selection_id TEXT")
        connection.execute(
            "INSERT INTO screening_run "
            "(run_revision_id, public_run_id, run_date, asof_date, run_at, universe_size, "
            "rules_ref, created_at, payload, application_git_commit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-old",
                "public-old",
                "2026-08-28",
                "2026-08-28",
                "2026-08-28T16:45:00+09:00",
                1,
                None,
                "2026-08-28T16:45:00+09:00",
                "{}",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO screening_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-old", 0, "2331", "サービス業", None, None, None, None, 0.1, "{}"),
        )
        connection.execute(
            "INSERT INTO screening_selection "
            "(selection_id, run_revision_id, profile, created_at, payload, "
            "publication_kind) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "selection-old",
                "run-old",
                "production",
                "2026-08-28T16:46:00+09:00",
                "{}",
                "machine",
            ),
        )
        connection.execute(
            "INSERT INTO selection_entry "
            "(selection_id, ordinal, ticker, selected, payload) VALUES (?, ?, ?, ?, ?)",
            ("selection-old", 0, "2331", 1, "{}"),
        )
        connection.execute("PRAGMA user_version = 3")
    return path


def test_cutover_replaces_only_rebuildable_rows_with_an_empty_current_store(
    tmp_path: Path,
) -> None:
    path = _v3_store(tmp_path / "runs.sqlite")

    discarded = cutover(path)

    assert discarded == {
        "selection_entry": 1,
        "screening_selection": 1,
        "screening_candidate": 1,
        "screening_run": 1,
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (RUN_STORE_SCHEMA_VERSION,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM screening_run").fetchone() == (0,)
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(screening_selection)")
        }
        assert "publication_kind" not in columns
        assert "source_selection_id" not in columns
        assert "profile" not in columns
        assert "application_git_commit" not in columns
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'selection_entry'"
            ).fetchone()
            is None
        )


def test_cutover_rejects_a_current_store_without_discarding_it(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("PRAGMA user_version = 4")

    before = path.read_bytes()
    with pytest.raises(RunStoreCutoverError, match="requires schema 3"):
        cutover(path)
    assert path.read_bytes() == before


def test_cutover_rejects_an_unexpected_table(tmp_path: Path) -> None:
    path = _v3_store(tmp_path / "runs.sqlite")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT) STRICT")

    with pytest.raises(RunStoreCutoverError, match="unexpected tables"):
        cutover(path)
