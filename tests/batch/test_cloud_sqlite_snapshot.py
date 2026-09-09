from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_batch.storage.sqlite_snapshot import (
    create_snapshot,
    database_schema_version,
    validate_database,
)


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    output.write_bytes(b"old snapshot")
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


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_snapshot_rejects_source_alias_without_changing_bytes(tmp_path, alias):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE kept (value)")
    output = source
    if alias != "same":
        output = tmp_path / "alias.sqlite"
        if alias == "symlink":
            output.symlink_to(source)
        else:
            output.hardlink_to(source)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="same"):
        create_snapshot(source, output)
    assert source.read_bytes() == before


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["missing", "invalid", "backup", "validation"])
def test_snapshot_failure_preserves_output(tmp_path, monkeypatch, existing, failure):
    from baibai_engine.market.sqlite import snapshot

    source = tmp_path / "source.sqlite"
    output = tmp_path / "output.sqlite"
    if existing:
        with sqlite3.connect(output) as db:
            db.execute("CREATE TABLE kept (value)")
    before = output.read_bytes() if existing else None
    if failure == "invalid":
        source.write_text("not SQLite")
    elif failure != "missing":
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE source_values (value)")
    if failure == "backup":

        def fail_backup(source, output):
            output.write_bytes(b"partial")
            raise sqlite3.DatabaseError("backup failed")

        monkeypatch.setattr(
            "baibai_batch.storage.sqlite_snapshot.create_market_snapshot", fail_backup
        )
    elif failure == "validation":

        def fail_validation(path):
            raise sqlite3.DatabaseError("validation failed")

        monkeypatch.setattr(snapshot, "validate_snapshot", fail_validation)
    paths_before = set(tmp_path.iterdir())
    with pytest.raises((FileNotFoundError, sqlite3.DatabaseError)):
        create_snapshot(source, output)
    assert (output.read_bytes() if output.exists() else None) == before
    assert set(tmp_path.iterdir()) == paths_before


@pytest.mark.parametrize("failure", [False, True])
def test_snapshot_closes_owned_connections(tmp_path, monkeypatch, failure):
    from baibai_engine.market.sqlite import snapshot

    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE source_values (value)")
    db.close()
    connect = sqlite3.connect
    opened = []

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    if failure:

        def fail_validation(path):
            raise sqlite3.DatabaseError("validation failed")

        monkeypatch.setattr(snapshot, "validate_snapshot", fail_validation)
        with pytest.raises(sqlite3.DatabaseError):
            create_snapshot(source, tmp_path / "output.sqlite")
    else:
        create_snapshot(source, tmp_path / "output.sqlite")
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
