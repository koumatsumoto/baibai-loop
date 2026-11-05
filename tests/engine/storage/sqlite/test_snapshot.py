from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from baibai_engine.storage.sqlite.snapshot import (
    backup_to_new_file,
    create_snapshot,
    database_schema_version,
    main,
    validate_database,
    validate_snapshot,
)


def test_new_backup_returns_schema_and_includes_wal_rows(tmp_path: Path) -> None:
    source = tmp_path / "source #1?.sqlite"
    output = tmp_path / "snapshot #1?.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA user_version=42")
        connection.execute("CREATE TABLE kept (value TEXT)")
        connection.execute("INSERT INTO kept VALUES ('from WAL')")
        connection.commit()
        assert backup_to_new_file(source, output) == 42

    assert validate_snapshot(output) == 42
    with closing(sqlite3.connect(output)) as connection:
        assert connection.execute("SELECT value FROM kept").fetchone() == ("from WAL",)


def test_new_backup_does_not_replace_an_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE kept (value)")
    source_before = source.read_bytes()
    output.write_bytes(b"keep existing snapshot")

    with pytest.raises(FileExistsError, match="output already exists"):
        backup_to_new_file(source, output)

    assert output.read_bytes() == b"keep existing snapshot"
    assert source.read_bytes() == source_before


def test_new_backup_removes_an_unvalidated_output(tmp_path, monkeypatch):
    from baibai_engine.storage.sqlite import snapshot

    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE kept (value)")
    before = source.read_bytes()

    def fail_validation(path):
        raise sqlite3.DatabaseError("validation failed")

    monkeypatch.setattr(snapshot, "validate_snapshot", fail_validation)
    with pytest.raises(sqlite3.DatabaseError, match="validation failed"):
        backup_to_new_file(source, output)
    assert not output.exists()
    assert source.read_bytes() == before


def test_snapshot_includes_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    output.write_bytes(b"old snapshot")
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE values_for_test (value TEXT NOT NULL)")
        connection.execute("INSERT INTO values_for_test VALUES ('visible')")
        connection.commit()
        create_snapshot(source, output)

    with closing(sqlite3.connect(output)) as snapshot, snapshot:
        assert snapshot.execute("SELECT value FROM values_for_test").fetchone() == ("visible",)


def test_validate_database_rejects_non_sqlite_file(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError):
        validate_database(invalid)


@pytest.mark.parametrize("schema_version", [0, 13, 32])
def test_snapshot_preserves_source_schema_and_rows(tmp_path: Path, schema_version: int) -> None:
    source = tmp_path / "source.sqlite"
    restored = tmp_path / "restored.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {schema_version}")
        connection.execute(
            "CREATE TABLE edinet_documents (doc_date TEXT NOT NULL, doc_id TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO edinet_documents VALUES ('2026-07-10', 'S100KEPT')")
        connection.commit()

    create_snapshot(source, restored)

    assert database_schema_version(restored) == schema_version
    with closing(sqlite3.connect(restored)) as connection, connection:
        assert connection.execute("SELECT doc_date, doc_id FROM edinet_documents").fetchall() == [
            ("2026-07-10", "S100KEPT")
        ]


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_snapshot_rejects_source_alias_without_changing_bytes(tmp_path, alias):
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
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
    from baibai_engine.storage.sqlite import snapshot

    source = tmp_path / "source.sqlite"
    output = tmp_path / "output.sqlite"
    if existing:
        with closing(sqlite3.connect(output)) as db, db:
            db.execute("CREATE TABLE kept (value)")
    before = output.read_bytes() if existing else None
    if failure == "invalid":
        source.write_text("not SQLite")
    elif failure != "missing":
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("CREATE TABLE source_values (value)")
    if failure == "backup":

        def fail_backup(source, output):
            output.write_bytes(b"partial")
            raise sqlite3.DatabaseError("backup failed")

        monkeypatch.setattr("baibai_engine.storage.sqlite.snapshot.backup_to_new_file", fail_backup)
    elif failure == "validation":

        def fail_validation(path):
            raise sqlite3.DatabaseError("validation failed")

        monkeypatch.setattr(snapshot, "validate_snapshot", fail_validation)
    paths_before = set(tmp_path.iterdir())
    with pytest.raises((FileNotFoundError, sqlite3.DatabaseError)):
        create_snapshot(source, output)
    assert (output.read_bytes() if output.exists() else None) == before
    assert set(tmp_path.iterdir()) == paths_before


def test_snapshot_cli_keeps_create_check_and_version(tmp_path, capsys):
    source = tmp_path / "source.sqlite"
    output = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("PRAGMA user_version=42")
    assert main(["create", "--source", str(source), "--output", str(output)]) == 0
    assert main(["check", "--path", str(output), "--schema-version", "42"]) == 0
    assert main(["version", "--path", str(output)]) == 0
    assert capsys.readouterr().out == "42\n"
    with pytest.raises(sqlite3.DatabaseError, match="expected=43 actual=42"):
        main(["check", "--path", str(output), "--schema-version", "43"])


@pytest.mark.parametrize("failure", [False, True])
def test_snapshot_closes_owned_connections(tmp_path, monkeypatch, failure):
    from baibai_engine.storage.sqlite import snapshot

    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE source_values (value)")
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


@pytest.mark.parametrize(
    "filename", ["store?copy.sqlite", "store#copy.sqlite", "store%20copy.sqlite"]
)
def test_validation_reads_the_exact_path_with_uri_characters(tmp_path: Path, filename: str) -> None:
    path = tmp_path / filename
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version = 42")
        connection.execute("CREATE TABLE kept (value TEXT)")
        connection.execute("INSERT INTO kept VALUES ('retained')")
    before = path.read_bytes()
    paths_before = set(tmp_path.iterdir())

    validate_database(path)
    assert database_schema_version(path) == 42
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == paths_before
