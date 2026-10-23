from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.storage.application import LATEST_VERSION
from baibai_engine.storage.application.write import (
    backup_database,
    initialize_database,
    prune_backups,
)


def test_initialize_creates_only_the_current_application_schema(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"

    assert initialize_database(path) == LATEST_VERSION
    assert initialize_database(path) == LATEST_VERSION

    with closing(sqlite3.connect(path)) as connection, connection:
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
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE old_state (value TEXT)")
        connection.execute(f"PRAGMA user_version = {LATEST_VERSION - 1}")

    with pytest.raises(RuntimeError, match="explicit semantic cutover"):
        initialize_database(path)


def test_backup_captures_uncheckpointed_wal_without_changing_source(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO ledger_meta "
            "(singleton, schema_version, portfolio_scope, as_of, payload) "
            "VALUES (1, 1, 'repository_only', '2026-08-29', '{}')"
        )
        connection.commit()
        wal = path.with_name(path.name + "-wal")
        before = path.read_bytes(), wal.read_bytes()
        assert before[1]

        backup = backup_database(
            path,
            backup_dir=tmp_path / "backups",
            now=datetime.fromisoformat("2026-08-29T12:00:00+09:00"),
        )

        assert (path.read_bytes(), wal.read_bytes()) == before
        assert backup is not None
        with closing(sqlite3.connect(backup)) as snapshot:
            assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert snapshot.execute("SELECT count(*) FROM ledger_meta").fetchone()[0] == 1


def test_failed_backup_validation_preserves_generations_and_next_success_retention(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    backups = tmp_path / "backups"
    instant = datetime.fromisoformat("2026-08-29T12:00:00+09:00")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        generations = [
            backup_database(path, backup_dir=backups, now=instant + timedelta(minutes=i), keep=2)
            for i in range(2)
        ]
        assert all(generation is not None for generation in generations)
        kept = {item: item.read_bytes() for item in backups.iterdir()}
        # A restore candidate with a broken application reference must not become
        # a retained generation, even though SQLite's structural check succeeds.
        connection.execute(
            "INSERT INTO stock_assessment_review VALUES "
            "('orphan-review', 'missing-assessment', '2026-08-29T12:00:00+09:00', '{}')"
        )
        connection.commit()
        wal = path.with_name(path.name + "-wal")
        before = path.read_bytes(), wal.read_bytes()

        with pytest.raises(sqlite3.IntegrityError, match="backup foreign_key_check failed"):
            backup_database(path, backup_dir=backups, now=instant + timedelta(minutes=2), keep=2)

        assert {item: item.read_bytes() for item in backups.iterdir()} == kept
        assert (path.read_bytes(), wal.read_bytes()) == before
        connection.execute("DELETE FROM stock_assessment_review")
        connection.commit()
        latest = backup_database(
            path, backup_dir=backups, now=instant + timedelta(minutes=3), keep=2
        )
        assert set(backups.iterdir()) == {generations[1], latest}
        assert generations[1] is not None
        assert generations[1].read_bytes() == kept[generations[1]]
        for item in backups.iterdir():
            with closing(sqlite3.connect(item)) as snapshot:
                assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert snapshot.execute("PRAGMA foreign_key_check").fetchall() == []


def test_interrupted_backup_cleans_temporary_generation(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "app.sqlite"
    initialize_database(path)
    backups = tmp_path / "backups"
    before = path.read_bytes()
    original_connect = sqlite3.connect

    class InterruptedBackup(sqlite3.Connection):
        def backup(self, destination, **kwargs):
            destination.execute("CREATE TABLE incomplete_checkpoint(value TEXT)")
            assert list(backups.glob("baibai-*.sqlite")) == []
            raise sqlite3.OperationalError("synthetic backup interruption")

    def connect(database, *args, **kwargs):
        if database == path:
            kwargs["factory"] = InterruptedBackup
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic backup interruption"):
        backup_database(path, backup_dir=backups)
    assert list(backups.iterdir()) == []
    assert path.read_bytes() == before


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


def test_prune_backups_compares_recorded_instants_across_offsets(tmp_path: Path) -> None:
    earlier = tmp_path / "baibai-20260829T090000000000+0900.sqlite"
    later = tmp_path / "baibai-20260829T010000000000+0000.sqlite"
    earlier.touch()
    later.touch()
    assert prune_backups(tmp_path, keep=1) == [earlier]
    assert later.exists()


def test_prune_backups_does_not_delete_when_a_timestamp_is_unreadable(tmp_path: Path) -> None:
    valid = tmp_path / "baibai-20260829T090000000000+0900.sqlite"
    unknown = tmp_path / "baibai-undated.sqlite"
    valid.touch()
    unknown.touch()
    with pytest.raises(ValueError, match="does not match format"):
        prune_backups(tmp_path, keep=1)
    assert valid.exists()
    assert unknown.exists()


def test_backup_rejects_naive_time_without_creating_a_generation(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    initialize_database(db)
    instant = datetime.fromisoformat("2026-08-29T12:00:00+09:00").replace(tzinfo=None)
    with pytest.raises(ValueError, match="UTC offset"):
        backup_database(db, now=instant)
    assert not (tmp_path / "backups").exists()


def test_info_reads_an_old_store_without_writer_access(tmp_path, monkeypatch, capsys) -> None:
    import baibai_engine.storage.application.cli as cli

    db = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE historical_value (value INTEGER)")
        connection.execute("INSERT INTO historical_value VALUES (1)")
        connection.execute("PRAGMA user_version = 1")
    before = db.read_bytes()
    reader = cli.connect_read_only
    connections = []

    def checked_reader(path):
        connection = reader(path)
        connections.append(connection.execute("PRAGMA query_only").fetchone()[0])
        return connection

    monkeypatch.setattr(cli, "connect_read_only", checked_reader)
    assert cli.main(["info", "--db", str(db)]) == 0
    assert connections == [1]
    assert "historical_value: 1" in capsys.readouterr().out
    assert db.read_bytes() == before


def test_prune_backups_leaves_failed_restore_copy_outside_generation_set(tmp_path: Path) -> None:
    earlier = tmp_path / "baibai-20260829T090000000000+0900.sqlite"
    later = tmp_path / "baibai-20260830T090000000000+0900.sqlite"
    failed_restore = tmp_path / "failed-restore-20260830T090000.sqlite"
    for path in (earlier, later, failed_restore):
        path.touch()
    assert prune_backups(tmp_path, keep=1) == [earlier]
    assert later.exists()
    assert failed_restore.exists()
