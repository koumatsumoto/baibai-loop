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
@pytest.mark.parametrize("compact", [False, True])
def test_snapshot_rejects_source_alias_without_changing_bytes(tmp_path, alias, compact):
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
        create_snapshot(source, output, compact=compact)
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


def test_compact_transfer_copy_keeps_runs_readers_payloads_and_wal(tmp_path):
    from tests.helpers.model_generation import empty_selection, generation, publish_generation
    from tests.helpers.screening_run import screening_run_payload, seed_saved_run

    from baibai_engine.machine_analysis.generation.query import read_generation
    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store.read import (
        ScreeningRunReader,
    )
    from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

    source, output = tmp_path / "runs.sqlite", tmp_path / "transfer.sqlite"
    original = generation()
    publish_generation(original, path=source)
    selection = empty_selection(original.generation_id)
    ScreeningSelectionStore(source).publish("saved-selection", selection)
    seed_saved_run(source, screening_run_payload(), run_revision_id="legacy-run")
    legacy = ScreeningRunReader(source).get_run("legacy-run")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE disposable (payload BLOB)")
        connection.execute("INSERT INTO disposable VALUES (zeroblob(262144))")
        connection.execute("DROP TABLE disposable")
        # Rowid is not a saved identity. Exercise readers with non-default values.
        for table in ("model_generation", "screening_selection", "screening_run"):
            connection.execute(f"UPDATE {table} SET rowid=900")
        connection.commit()
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
        schema = connection.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall()
        tables = (
            "model_generation",
            "generated_model",
            "screening_selection",
            "screening_run",
            "stock_analysis",
        )
        rows = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        source_bytes = source.read_bytes()
        wal_bytes = source.with_name(source.name + "-wal").read_bytes()
        assert main(["create", "--source", str(source), "--output", str(output), "--compact"]) == 0
        assert source.read_bytes() == source_bytes
        assert source.with_name(source.name + "-wal").read_bytes() == wal_bytes
        assert output.stat().st_size < connection.execute("PRAGMA page_count").fetchone()[0] * 4096
        with closing(sqlite3.connect(output)) as captured:
            assert captured.execute("PRAGMA freelist_count").fetchone()[0] == 0
            assert (
                captured.execute("SELECT sql FROM sqlite_schema ORDER BY name").fetchall() == schema
            )
            assert {
                table: captured.execute(f"SELECT * FROM {table}").fetchall() for table in tables
            } == rows
            # Prove current and historical readers do not depend on implicit rowids,
            # even if SQLite changes them when rewriting pages.
            for table in ("model_generation", "screening_selection", "screening_run"):
                captured.execute(f"UPDATE {table} SET rowid=42")
            captured.commit()
        assert read_generation(original.generation_id, path=output) == original
        assert ScreeningSelectionStore(output).read("saved-selection") == selection
        assert ScreeningRunReader(output).get_run("legacy-run") == legacy
        assert database_schema_version(output) == database_schema_version(source)
        assert not publish_generation(original, path=output)
        assert not ScreeningSelectionStore(output).publish("saved-selection", selection)


@pytest.mark.parametrize("free_pages", [False, True])
def test_compact_copy_only_vacuums_when_pages_are_free(tmp_path, monkeypatch, free_pages):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE kept (value TEXT)")
        db.execute("INSERT INTO kept VALUES ('saved')")
        if free_pages:
            db.execute("CREATE TABLE disposable (payload BLOB)")
            db.execute("INSERT INTO disposable VALUES (zeroblob(262144))")
            db.execute("DROP TABLE disposable")
    connect = sqlite3.connect
    statements = []

    def track(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", track)
    create_snapshot(source, output, compact=True)
    assert statements.count("VACUUM") == int(free_pages)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["vacuum", "validation"])
def test_compaction_failure_keeps_source_and_previous_output(
    tmp_path, monkeypatch, existing, failure
):
    from baibai_engine.storage.sqlite import snapshot

    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE kept (value TEXT)")
        db.execute("CREATE TABLE disposable (payload BLOB)")
        db.execute("INSERT INTO disposable VALUES (zeroblob(262144))")
        db.execute("DROP TABLE disposable")
    if existing:
        output.write_bytes(b"previous output")
    before, paths = source.read_bytes(), set(tmp_path.iterdir())
    validate = snapshot.validate_snapshot
    calls = 0

    def validation(path):
        nonlocal calls
        calls += 1
        if failure == "validation" and calls == 2:
            raise sqlite3.DatabaseError("post-compaction validation failed")
        return validate(path)

    connect = sqlite3.connect

    class FailingVacuum(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "VACUUM":
                raise sqlite3.OperationalError("compaction failed")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=FailingVacuum)

    monkeypatch.setattr(snapshot, "validate_snapshot", validation)
    if failure == "vacuum":
        monkeypatch.setattr(sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError):
        create_snapshot(source, output, compact=True)
    assert source.read_bytes() == before
    assert (output.read_bytes() if output.exists() else None) == (
        b"previous output" if existing else None
    )
    assert set(tmp_path.iterdir()) == paths


def test_compaction_validates_checkpointed_copy_after_vacuum(tmp_path, monkeypatch):
    from baibai_engine.storage.sqlite import snapshot

    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE kept (value TEXT)")
        db.execute("CREATE TABLE disposable (payload BLOB)")
        db.execute("INSERT INTO disposable VALUES (zeroblob(262144))")
        db.execute("DROP TABLE disposable")
    validate = snapshot.validate_snapshot
    free_page_counts = []

    def capture_validation(path):
        assert not path.with_name(path.name + "-wal").exists()
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            free_page_counts.append(db.execute("PRAGMA freelist_count").fetchone()[0])
        return validate(path)

    monkeypatch.setattr(snapshot, "validate_snapshot", capture_validation)
    create_snapshot(source, output, compact=True)
    assert len(free_page_counts) == 2
    assert free_page_counts[0] > 0
    assert free_page_counts[1] == 0
