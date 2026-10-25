import sqlite3
from contextlib import closing

import pytest
from tools.migrate_model_store import main, migrate_copy, verify_migration

from baibai_engine.storage.machine_analysis import (
    GENERATION_SCHEMA_SQL,
    SCHEMA_SQL,
    migrate_run_store_v6,
)
from baibai_jobs.storage.sqlite_snapshot import database_schema_version


def legacy(path):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL.removesuffix(GENERATION_SCHEMA_SQL))
    connection.execute("PRAGMA user_version=6")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "old",
            "old",
            "2026-10-23",
            "2026-10-23",
            "2026-10-23T00:00:00Z",
            1,
            None,
            "old",
            '{"original":true}',
        ),
    )
    connection.execute(
        "INSERT INTO stock_analysis VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("old", 0, "1234", None, None, None, None, None, 0.100049, '{"value":null}'),
    )
    connection.commit()
    return connection


def rows(connection):
    return [
        connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in ("screening_run", "stock_analysis")
    ]


def test_migration_copy_includes_wal_and_preserves_source_and_legacy_payloads(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "migrated.sqlite"
    with closing(legacy(source)) as original:
        before = rows(original)
        source_bytes = source.read_bytes()
        report = migrate_copy(source, output)
        assert source.read_bytes() == source_bytes
        assert rows(original) == before
        assert database_schema_version(source) == 6
        assert report["source_schema_version"] == 6
        assert report["output_schema_version"] == 7
        assert report["production_published"] is False
        assert report["legacy_row_counts"] == {"screening_run": 1, "stock_analysis": 1}
        with closing(sqlite3.connect(output)) as migrated:
            assert rows(migrated) == before
            assert migrated.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
            for table in ("model_generation", "generated_model", "screening_selection"):
                assert migrated.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert migrate_run_store_v6(output) == 7


def test_existing_output_is_not_clobbered_even_by_same_source(tmp_path):
    source = tmp_path / "source.sqlite"
    with closing(legacy(source)):
        before = source.read_bytes()
        with pytest.raises(FileExistsError):
            migrate_copy(source, source)
        assert source.read_bytes() == before
        assert database_schema_version(source) == 6
    output = tmp_path / "output.sqlite"
    output.write_bytes(b"do not replace")
    with pytest.raises(FileExistsError):
        migrate_copy(source, output)
    assert output.read_bytes() == b"do not replace"


def test_missing_source_does_not_create_empty_database(tmp_path):
    source, output = tmp_path / "missing.sqlite", tmp_path / "output.sqlite"
    with pytest.raises(FileNotFoundError):
        migrate_copy(source, output)
    with pytest.raises(FileNotFoundError):
        migrate_run_store_v6(source)
    assert not source.exists()
    assert not output.exists()


def test_unsupported_schema_leaves_source_untouched_and_reports_failure(tmp_path, capsys):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    with closing(legacy(source)) as original:
        original.execute("PRAGMA user_version=5")
        original.commit()
        assert main(["--source", str(source), "--output", str(output)]) == 1
        assert "migration requires schema 6" in capsys.readouterr().err
        assert database_schema_version(source) == database_schema_version(output) == 5
        with closing(sqlite3.connect(output)) as migrated:
            assert rows(original) == rows(migrated)


def test_failed_schema_change_rolls_back_tables_and_version(tmp_path):
    path = tmp_path / "runs.sqlite"
    with closing(legacy(path)) as original:
        original.execute("CREATE TABLE generated_model (collision TEXT)")
        original.commit()
        before = rows(original)
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            migrate_run_store_v6(path)
        assert database_schema_version(path) == 6
        assert rows(original) == before
        assert (
            original.execute(
                "SELECT name FROM sqlite_schema WHERE name='model_generation'"
            ).fetchall()
            == []
        )


def test_readback_verification_detects_payload_change_without_count_change(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    with closing(legacy(source)):
        migrate_copy(source, output)
        assert main(["--source", str(source), "--output", str(output), "--verify-only"]) == 0
        with closing(sqlite3.connect(output)) as changed:
            changed.execute("UPDATE stock_analysis SET payload='{}'")
            changed.commit()
        with pytest.raises(RuntimeError, match="changed legacy rows"):
            verify_migration(source, output)
