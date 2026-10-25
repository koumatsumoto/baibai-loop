from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_jobs.validation.market_context_stores import (
    main,
    validate_store,
)
from baibai_engine.data_collection.economic.documents import SourceDocument, store_document
from baibai_engine.data_collection.economic.records import store_snapshot
from baibai_engine.data_collection.economic.store import (
    SQLITE_SCHEMA_VERSION,
    EconomicSchemaError,
    initialize_database,
    record_source_run,
)


def _build_store(path: Path) -> None:
    connection = initialize_database(path)
    try:
        store_snapshot(
            connection,
            source_id="unregistered-raw-source",
            observed_at=date(2026, 7, 1),
            fetched_at=datetime(2026, 7, 2, tzinfo=UTC),
            records=[{"unused": None, "native": "--", "unbounded": 99999999}],
        )
        store_document(
            connection,
            SourceDocument(
                source_id="unregistered-document-source",
                request_key="2026-07",
                source_url="https://example.com/source",
                content_type="application/octet-stream",
                fetched_at=datetime(2026, 7, 2, tzinfo=UTC),
                payload=b"opaque original body",
                metadata={"period": "2026-07"},
            ),
        )
        record_source_run(
            connection,
            source_id="unregistered-raw-source",
            start=date(2026, 7, 1),
            end=date(2026, 7, 1),
            started_at=datetime(2026, 7, 2, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
        connection.commit()
    finally:
        connection.close()


def test_validate_store_accepts_three_collection_tables_without_l2_registry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "indicators.sqlite"
    _build_store(database)

    report = validate_store(database)

    assert report.valid
    assert report.schema_version == SQLITE_SCHEMA_VERSION
    assert report.source_records == 1
    assert report.source_documents == 1
    assert report.source_runs == 1


def test_validate_store_reads_without_modifying_or_creating_sidecars(
    tmp_path: Path,
) -> None:
    """The live store is git-ignored and irreplaceable in part; a scan must not touch it."""

    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    before = database.stat()
    before_bytes = database.read_bytes()

    report = validate_store(database)

    after = database.stat()
    assert report.valid
    assert report.schema_version == SQLITE_SCHEMA_VERSION
    assert (report.source_records, report.source_documents, report.source_runs) == (1, 1, 1)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert database.read_bytes() == before_bytes
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not Path(f"{database}-journal").exists()


def test_validate_store_refuses_a_store_on_another_schema(tmp_path: Path) -> None:
    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(EconomicSchemaError, match="unsupported economic source schema"):
        validate_store(database)


def test_main_validates_sources_without_reading_the_application_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    application_db = tmp_path / "stores/application/baibai.sqlite"
    application_db.parent.mkdir(parents=True)
    application_db.write_bytes(b"unreadable application store")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--db", str(database)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ok: schema v" in captured.out
    assert "1 native records; 1 documents; 1 source runs" in captured.out
    assert not captured.err
    assert application_db.read_bytes() == b"unreadable application store"


@pytest.mark.parametrize("damage", [None, "gzip", "shape", "count", "old_vintage"])
def test_validation_checks_native_payloads_without_imposing_scalar_semantics(tmp_path, damage):
    import gzip

    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        store_snapshot(
            connection,
            source_id="unregistered-raw-source",
            observed_at=date(2026, 7, 1),
            fetched_at=datetime(2026, 7, 2, tzinfo=UTC),
            records=[{"unused": None, "native": "--", "unbounded": 99999999}],
        )
        store_snapshot(
            connection,
            source_id="unregistered-raw-source",
            observed_at=date(2026, 7, 1),
            fetched_at=datetime(2026, 7, 3, tzinfo=UTC),
            records=[],
        )
        if damage in {"gzip", "shape", "old_vintage"}:
            payload = gzip.compress(b"{}") if damage == "shape" else b"broken gzip"
            connection.execute(
                "UPDATE economic_source_records SET payload=? WHERE fetched_at LIKE ?",
                (payload, "2026-07-02%" if damage == "old_vintage" else "2026-07-03%"),
            )
        elif damage == "count":
            connection.execute("UPDATE economic_source_records SET record_count=7")
    before = database.read_bytes()
    report = validate_store(database)
    assert report.valid is (damage is None)
    assert report.source_records == 2
    assert report.source_documents == 1
    assert report.source_runs == 1
    if damage is not None:
        assert all("native" in item for item in report.violations)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("damage", "older"),
    [
        ("{invalid", False),
        ("[]", False),
        ("{invalid", True),
        ("[]", True),
    ],
)
def test_validation_checks_every_document_metadata_revision_without_parsing_opaque_body(
    tmp_path: Path, damage: str, older: bool
) -> None:
    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        store_document(
            connection,
            SourceDocument(
                source_id="unregistered-document-source",
                request_key="2026-07",
                source_url="https://example.com/source",
                content_type="application/octet-stream",
                fetched_at=datetime(2026, 7, 3, tzinfo=UTC),
                payload=b"\x00\xff still opaque",
                metadata={"period": "2026-07", "revision": 2},
            ),
        )
        connection.execute(
            "UPDATE economic_source_documents SET metadata=? WHERE fetched_at LIKE ?",
            (damage, "2026-07-02%" if older else "2026-07-03%"),
        )
    before = database.read_bytes()
    report = validate_store(database)
    assert not report.valid
    assert report.source_documents == 2
    assert report.violation_count == 1
    assert "document metadata is not a JSON object" in report.violations[0]
    assert database.read_bytes() == before
