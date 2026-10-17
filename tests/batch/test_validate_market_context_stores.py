from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.assessment_history import historical_context_payload, seed_history

from baibai_batch.validation.market_context_stores import (
    main,
    validate_published_contexts,
    validate_store,
)
from baibai_engine.collection.economic_documents import SourceDocument, store_document
from baibai_engine.collection.economic_records import store_snapshot
from baibai_engine.collection.economic_store import (
    SQLITE_SCHEMA_VERSION,
    EconomicSchemaError,
    initialize_database,
    record_source_run,
)
from baibai_engine.market_context.indicators.definitions import (
    IndicatorDefinitions,
)
from baibai_engine.research.market_context.models import (
    ASSESSMENT_SCHEMA_VERSION,
    StoredAssessment,
)
from baibai_engine.storage.application.write import (
    initialize_database as initialize_application_database,
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


def _publish_report(path: Path) -> StoredAssessment:
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(path, document.document, supersedes=None)
    return document


def test_validate_published_contexts_reads_every_current_contract_revision(
    tmp_path: Path,
) -> None:
    application_db = tmp_path / "app.sqlite"
    _publish_report(application_db)

    report = validate_published_contexts(application_db)

    assert report.valid
    assert report.documents == 1
    assert report.warnings == ()


def test_validate_published_contexts_warns_instead_of_failing_on_a_retired_series(
    tmp_path: Path,
) -> None:
    """Retiring a series must stay a normal operation: the report still has to read."""

    application_db = tmp_path / "app.sqlite"
    _publish_report(application_db)

    report = validate_published_contexts(
        application_db,
        definitions=IndicatorDefinitions(series=()),
    )

    assert report.valid
    assert report.documents == 1
    assert any("cites retired series: us.10y" in warning for warning in report.warnings)


def test_validate_published_contexts_fails_when_a_stored_revision_cannot_be_read(
    tmp_path: Path,
) -> None:
    application_db = tmp_path / "app.sqlite"
    initialize_application_database(application_db)
    payload = historical_context_payload()
    del payload["summary"]
    with closing(sqlite3.connect(application_db)) as connection, connection:
        connection.execute(
            "INSERT INTO market_context_assessment (context_id, schema_version, as_of, published_at, "
            "supersedes_id, payload) VALUES (?, ?, ?, ?, NULL, ?)",
            (
                payload["context_id"],
                ASSESSMENT_SCHEMA_VERSION,
                payload["as_of"],
                payload["published_at"],
                json.dumps(payload),
            ),
        )

    report = validate_published_contexts(application_db)

    assert not report.valid
    assert report.documents == 1
    assert "cannot be read" in report.failures[0]


def test_main_reports_both_stores_and_fails_on_an_unreadable_revision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "indicators.sqlite"
    _build_store(database)
    application_db = tmp_path / "app.sqlite"
    initialize_application_database(application_db)
    payload = historical_context_payload()
    payload["summary"] = "  "
    with closing(sqlite3.connect(application_db)) as connection, connection:
        connection.execute(
            "INSERT INTO market_context_assessment (context_id, schema_version, as_of, published_at, "
            "supersedes_id, payload) VALUES (?, ?, ?, ?, NULL, ?)",
            (
                payload["context_id"],
                ASSESSMENT_SCHEMA_VERSION,
                payload["as_of"],
                payload["published_at"],
                json.dumps(payload),
            ),
        )

    exit_code = main(["--db", str(database), "--app-db", str(application_db)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ok: schema v" in captured.out
    assert "published market context assessment revisions cannot be read" in captured.err


def test_main_skips_the_report_check_without_an_application_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh checkout has published nothing; absence is a valid state."""

    database = tmp_path / "indicators.sqlite"
    _build_store(database)

    exit_code = main(["--db", str(database), "--app-db", str(tmp_path / "absent.sqlite")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "skip: no application store" in captured.out


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
