from __future__ import annotations

import hashlib
import sqlite3
import sys
from contextlib import closing

import pytest

from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    QUANTS_SCORE_DDL,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.store import (
    save_observations,
)
from baibai_engine.storage.sqlite.source_cache import schema
from tools import enable_kabumap_uncomputed as migration

# The pre-cutover table definition is fixed independently of the new owner model.
LEGACY_TABLE_SQL = (
    f"CREATE TABLE {QUANTS_SCORE_TABLE}("
    "ticker TEXT NOT NULL,fetched_at_utc TEXT NOT NULL,snapshot_date TEXT NOT NULL,"
    "distributor TEXT NOT NULL,origin TEXT,source_url TEXT NOT NULL,"
    "parser_version TEXT NOT NULL,contract_version TEXT NOT NULL,"
    "score_as_of_date TEXT NOT NULL,score_scale_max INTEGER NOT NULL,"
    "composite_scale_max INTEGER NOT NULL,growth_score REAL,undervaluation_score REAL,"
    "size_score REAL,technical_score REAL,financial_health_score REAL,market_trend_score REAL,"
    "composite_score REAL,industry_average_score REAL,PRIMARY KEY(ticker,fetched_at_utc))"
)
LEGACY_DDL = (
    LEGACY_TABLE_SQL
    + ";"
    + f"CREATE INDEX idx_{QUANTS_SCORE_TABLE}_snapshot ON {QUANTS_SCORE_TABLE}(snapshot_date);"
)


@pytest.fixture
def work_path(tmp_path):
    path = tmp_path / "cutover"
    path.mkdir()
    return path


def _source(path):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(schema._SCHEMA_SQL.replace(QUANTS_SCORE_DDL, LEGACY_DDL))
        conn.execute("PRAGMA user_version=50")
        for rowid, ticker, growth in ((7, "7203", 6.3), (41, "6758", None)):
            conn.execute(
                f"INSERT INTO {QUANTS_SCORE_TABLE} VALUES ({','.join('?' for _ in range(19))})",
                (
                    ticker,
                    "2026-11-10T07:00:00.123456+00:00",
                    "2026-11-10",
                    "kabumap",
                    "Quants Research",
                    f"https://fixture.invalid/{ticker}",
                    "kabumap-parser-v1",
                    "1",
                    "2026-11-09",
                    10,
                    100,
                    growth,
                    None,
                    7.0,
                    4.5,
                    1.0,
                    None,
                    56.1,
                    40.0,
                ),
            )
            conn.execute(f"UPDATE {QUANTS_SCORE_TABLE} SET rowid=? WHERE ticker=?", (rowid, ticker))
        conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-11-10', 1)")
        conn.execute(
            "INSERT INTO equity_web_source_responses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "receipt-original",
                "attempt-original",
                "kabumap",
                "7203",
                "page",
                None,
                "https://fixture.invalid/7203",
                "2026-11-10T07:00:00.123456+00:00",
                "2026-11-10",
                "utf-8",
                b"\x1f\x8bsynthetic retained bytes\x00\xff",
            ),
        )
        conn.executescript(
            "CREATE TABLE local_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB);"
            "INSERT INTO local_audit(id,payload) VALUES (17,x'0001ff');"
            f"CREATE INDEX fixture_quants_growth ON {QUANTS_SCORE_TABLE}(growth_score);"
            f"CREATE VIEW fixture_quants_view AS SELECT ticker FROM {QUANTS_SCORE_TABLE};"
            f"CREATE TRIGGER fixture_quants_trigger AFTER INSERT ON {QUANTS_SCORE_TABLE} "
            "BEGIN INSERT INTO local_audit(payload) VALUES (new.ticker); END;"
            "CREATE TRIGGER fixture_audit_trigger AFTER INSERT ON local_audit "
            f"BEGIN SELECT count(*) FROM {QUANTS_SCORE_TABLE}; END;"
            "CREATE TABLE fixture_quants_link(ticker TEXT, fetched_at_utc TEXT, "
            f"FOREIGN KEY(ticker,fetched_at_utc) REFERENCES {QUANTS_SCORE_TABLE}"
            "(ticker,fetched_at_utc));"
            "INSERT INTO fixture_quants_link VALUES "
            "('7203','2026-11-10T07:00:00.123456+00:00');"
        )
    return path


def _preserved_contents(path):
    with closing(sqlite3.connect(path)) as conn:
        objects = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name!=? ORDER BY type,name",
            (QUANTS_SCORE_TABLE,),
        ).fetchall()
        table_hashes = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name!=? ORDER BY name",
            (QUANTS_SCORE_TABLE,),
        ):
            quoted = '"' + table.replace('"', '""') + '"'
            rows = conn.execute(f"SELECT rowid,* FROM {quoted} ORDER BY rowid").fetchall()
            table_hashes[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return objects, table_hashes


def test_migration_preserves_every_original_field_rowid_and_other_store_objects(work_path):
    source = _source(work_path / "schema 50 #?.sqlite")
    destination = work_path / "schema51.sqlite"
    before = source.read_bytes()
    preserved = _preserved_contents(source)
    with closing(sqlite3.connect(source)) as conn:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({QUANTS_SCORE_TABLE})")]
        rows = conn.execute(f"SELECT rowid,* FROM {QUANTS_SCORE_TABLE} ORDER BY rowid").fetchall()

    migration.copy_with_kabumap_uncomputed(source, destination)

    assert source.read_bytes() == before
    assert _preserved_contents(destination) == preserved
    with closing(schema.open_connection(destination)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (51,)
        assert (
            conn.execute(
                f"SELECT rowid,{','.join(columns)} FROM {QUANTS_SCORE_TABLE} ORDER BY rowid"
            ).fetchall()
            == rows
        )
        assert conn.execute(
            f"SELECT availability FROM {QUANTS_SCORE_TABLE} ORDER BY rowid"
        ).fetchall() == [("available",), ("available",)]
        assert conn.execute("SELECT * FROM fixture_quants_view ORDER BY ticker").fetchall() == [
            ("6758",),
            ("7203",),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert sorted(path.name for path in work_path.iterdir()) == [source.name, destination.name]


def test_migrated_store_roundtrips_not_computed_without_running_migration_triggers(work_path):
    source = _source(work_path / "old.sqlite")
    destination = work_path / "new.sqlite"
    migration.copy_with_kabumap_uncomputed(source, destination)
    observation = QuantsScoreObservation(
        ticker="200A",
        fetched_at_utc="2026-11-11T07:00:00+00:00",
        source_url="https://fixture.invalid/200A",
        parser_version="kabumap-parser-v2",
        availability="not_computed",
        score_as_of_date=None,
    )
    assert save_observations(destination, [observation]) == 1
    assert save_observations(destination, [observation]) == 0
    with closing(schema.open_connection(destination)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {QUANTS_SCORE_TABLE} WHERE ticker='200A'").fetchone()
        assert QuantsScoreObservation.model_validate(dict(row)) == observation
        assert [tuple(row) for row in conn.execute("SELECT * FROM local_audit ORDER BY id")] == [
            (17, b"\x00\x01\xff"),
            (18, "200A"),
        ]


@pytest.mark.parametrize("version", [0, 49, 51, 52])
def test_migration_refuses_every_non50_version_without_output(work_path, version):
    source = _source(work_path / "old.sqlite")
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute(f"PRAGMA user_version={version}")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="source must have market schema 50"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE edinet_list_responses",
        "DROP INDEX idx_source_coverage_source_window",
        "ALTER TABLE jquants_market_calendar ADD COLUMN unexpected TEXT",
        f"ALTER TABLE {QUANTS_SCORE_TABLE} ADD COLUMN availability TEXT",
        f"ALTER TABLE {QUANTS_SCORE_TABLE} ADD COLUMN generated TEXT GENERATED ALWAYS AS (ticker)",
    ],
)
def test_migration_refuses_malformed_schema50_and_keeps_source(work_path, damage):
    source = _source(work_path / "old.sqlite")
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute(damage)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="source schema-50"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


def test_migration_rejects_partial_required_index_with_same_columns(work_path):
    source = _source(work_path / "old.sqlite")
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.execute("DROP INDEX idx_edinet_list_responses_date_received")
        conn.execute(
            "CREATE INDEX idx_edinet_list_responses_date_received "
            "ON edinet_list_responses(doc_date, received_at_utc) WHERE doc_date>'2026-01-01'"
        )
    before = source.read_bytes()
    with pytest.raises(ValueError, match="source schema-50"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


@pytest.mark.parametrize("failure", ["corrupt", "foreign_key"])
def test_migration_rejects_unhealthy_source_without_output(work_path, failure):
    source = _source(work_path / "old.sqlite")
    if failure == "corrupt":
        source.write_bytes(b"not a SQLite database")
        error, match = sqlite3.DatabaseError, "not a database"
    else:
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("INSERT INTO fixture_quants_link VALUES ('9999','absent')")
        error, match = ValueError, "foreign key check failed"
    before = source.read_bytes()
    with pytest.raises(error, match=match):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


def test_source_connection_is_readonly(work_path, monkeypatch):
    source = _source(work_path / "old.sqlite")
    validate = migration._validate_schema50

    def verify_readonly(conn):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM jquants_market_calendar")
        validate(conn)

    monkeypatch.setattr(migration, "_validate_schema50", verify_readonly)
    migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")


def test_backup_includes_committed_wal_rows_without_modifying_source(work_path):
    source = _source(work_path / "old.sqlite")
    destination = work_path / "new.sqlite"
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO jquants_market_calendar VALUES ('2026-11-11', 1)")
        writer.commit()
        wal = source.with_name(source.name + "-wal")
        before, wal_before = source.read_bytes(), wal.read_bytes()

        migration.copy_with_kabumap_uncomputed(source, destination)

        assert source.read_bytes() == before
        assert wal.read_bytes() == wal_before
        assert _preserved_contents(destination) == _preserved_contents(source)
        with closing(schema.open_connection(destination)) as target:
            assert target.execute(
                "SELECT count(*) FROM jquants_market_calendar WHERE day='2026-11-11'"
            ).fetchone() == (1,)
            assert target.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert not destination.with_name(destination.name + "-wal").exists()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, sqlite3.OperationalError])
def test_transaction_failure_preserves_source_and_removes_unpublished_target(
    work_path, monkeypatch, failure
):
    source = _source(work_path / "old.sqlite")
    before = source.read_bytes()
    rebuild = migration._rebuild_quants

    def interrupt(conn):
        rebuild(conn)
        conn.execute("DELETE FROM equity_web_source_responses")
        raise failure("interrupted after rebuild")

    monkeypatch.setattr(migration, "_rebuild_quants", interrupt)
    with pytest.raises(failure, match="interrupted after rebuild"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


def test_final_schema_validation_failure_leaves_no_destination(work_path, monkeypatch):
    source = _source(work_path / "old.sqlite")
    before = source.read_bytes()

    def fail(conn):
        raise schema.SQLiteSchemaError("final shape rejected")

    monkeypatch.setattr(schema, "validate_current_schema", fail)
    with pytest.raises(schema.SQLiteSchemaError, match="final shape rejected"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert source.read_bytes() == before
    assert list(work_path.iterdir()) == [source]


def test_no_source_or_existing_output_is_overwritten(work_path):
    source = _source(work_path / "old.sqlite")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="must differ"):
        migration.copy_with_kabumap_uncomputed(source, source)
    existing = work_path / "existing.sqlite"
    existing.write_bytes(b"keep existing output")
    with pytest.raises(FileExistsError):
        migration.copy_with_kabumap_uncomputed(source, existing)
    assert existing.read_bytes() == b"keep existing output"
    assert source.read_bytes() == before


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_destination_symlink_or_stale_sidecar_is_rejected(work_path, suffix):
    source = _source(work_path / "old.sqlite")
    destination = work_path / "new.sqlite"
    occupied = work_path / (destination.name + suffix)
    occupied.symlink_to(work_path / "absent.sqlite")
    with pytest.raises(FileExistsError):
        migration.copy_with_kabumap_uncomputed(source, destination)
    assert occupied.is_symlink()
    assert not destination.exists()


def test_racing_destination_is_not_overwritten(work_path, monkeypatch):
    source = _source(work_path / "old.sqlite")
    destination = work_path / "new.sqlite"
    original_link = migration.os.link

    def race(temporary, target):
        destination.write_bytes(b"another writer won")
        original_link(temporary, target)

    monkeypatch.setattr(migration.os, "link", race)
    with pytest.raises(FileExistsError):
        migration.copy_with_kabumap_uncomputed(source, destination)
    assert destination.read_bytes() == b"another writer won"
    assert set(work_path.iterdir()) == {source, destination}


@pytest.mark.parametrize("opener", [schema.open_connection, schema.connect_current])
def test_ordinary_open_still_rejects_schema50_without_migration(work_path, opener):
    source = _source(work_path / "old.sqlite")
    before = source.read_bytes()
    with pytest.raises(schema.SQLiteSchemaError, match="found user_version=50, expected=51"):
        opener(source)
    assert source.read_bytes() == before


def test_migration_cli_only_writes_explicit_new_local_copy(work_path, monkeypatch):
    source = _source(work_path / "old.sqlite")
    destination = work_path / "new.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        ["enable_kabumap_uncomputed", "--source", str(source), "--destination", str(destination)],
    )
    migration.main()
    with closing(schema.open_connection(destination)) as conn:
        schema.validate_current_schema(conn)


def test_migration_refuses_future_current_contract(work_path, monkeypatch):
    source = _source(work_path / "old.sqlite")
    monkeypatch.setattr(schema, "SQLITE_SCHEMA_VERSION", 52)
    with pytest.raises(ValueError, match="one-time cutover"):
        migration.copy_with_kabumap_uncomputed(source, work_path / "new.sqlite")
    assert list(work_path.iterdir()) == [source]
