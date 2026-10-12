from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.assessment_history import historical_context_payload, seed_history

from baibai_batch.validation.market_context_stores import (
    main,
    validate_published_contexts,
    validate_store,
)
from baibai_engine.market_context.assessment.models import (
    ASSESSMENT_SCHEMA_VERSION,
    StoredAssessment,
)
from baibai_engine.market_context.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    initialize_database,
    insert_observations,
    open_connection,
)
from baibai_engine.market_context.indicators.definitions import (
    IndicatorDefinitions,
    SeriesDefinition,
    load_definitions,
)
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.storage.application.write import (
    initialize_database as initialize_application_database,
)


def _definition(*, maximum: float | None = 20.0) -> SeriesDefinition:
    return SeriesDefinition(
        series_id="test.series",
        name="Test Series",
        category="test",
        geography="world",
        frequency="daily",
        unit="percent",
        provider="fred_csv",
        provider_series_id="TEST",
        source_id="test-source",
        source_url="https://example.com/data.csv",
        plausible_min=-2.0,
        plausible_max=maximum,
    )


def _build_store(path: Path, definition: SeriesDefinition) -> None:
    connection = initialize_database(
        path,
        definitions=IndicatorDefinitions(series=(definition,)),
    )
    try:
        insert_observations(
            connection,
            [
                ObservationRecord(
                    series_id=definition.series_id,
                    observed_at=date(2026, 7, 1),
                    value=4.2,
                    unit=definition.unit,
                    source_url=definition.source_url,
                    vintage_at=datetime(2026, 7, 2, tzinfo=UTC),
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_validate_store_accepts_every_stored_vintage_within_registry_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)

    report = validate_store(
        database,
        definitions=IndicatorDefinitions(series=(definition,)),
    )

    assert report.valid
    assert report.schema_version == SQLITE_SCHEMA_VERSION
    assert report.registry_series == 1
    assert report.observations == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "DELETE FROM registry_state",
            "registry state.*must contain exactly singleton=1",
        ),
    ],
)
def test_validate_store_rejects_invalid_registry_generation_state(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)

    with pytest.raises(IndicatorsSchemaError, match=message):
        validate_store(
            database,
            definitions=IndicatorDefinitions(series=(definition,)),
        )
    with pytest.raises(IndicatorsSchemaError, match=message):
        open_connection(
            database,
            definitions=IndicatorDefinitions(series=(definition,)),
        )


def test_validate_store_reports_band_drift_with_observation_identity(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)

    report = validate_store(
        database,
        definitions=IndicatorDefinitions(series=(_definition(maximum=4.0),)),
    )

    assert not report.valid
    assert report.violation_count == 1
    assert "test.series 2026-07-01 vintage 2026-07-02T00:00:00+00:00" in report.violations[0]
    assert "value 4.2 outside plausible range [-2, 4]" in report.violations[0]


def test_validate_store_reports_a_series_absent_from_current_registry(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    _build_store(database, _definition())

    report = validate_store(
        database,
        definitions=IndicatorDefinitions(series=()),
    )

    assert not report.valid
    assert report.violation_count == 1
    assert "series is absent from the current registry" in report.violations[0]


def test_validate_store_reads_without_modifying_or_creating_sidecars(
    tmp_path: Path,
) -> None:
    """The live store is git-ignored and irreplaceable in part; a scan must not touch it."""

    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)
    before = database.stat()

    report = validate_store(
        database,
        definitions=IndicatorDefinitions(series=(definition,)),
    )

    after = database.stat()
    assert report.valid
    assert report.schema_version == SQLITE_SCHEMA_VERSION
    assert report.observations == 1
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not Path(f"{database}-journal").exists()


def test_validate_store_refuses_a_store_on_another_schema(tmp_path: Path) -> None:
    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

    with pytest.raises(IndicatorsSchemaError, match="unsupported indicator SQLite schema"):
        validate_store(database, definitions=IndicatorDefinitions(series=(definition,)))


def _publish_report(path: Path) -> StoredAssessment:
    document = StoredAssessment.from_payload(historical_context_payload())
    seed_history(path, (document).document, supersedes=None)
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
    with sqlite3.connect(application_db) as connection:
        connection.execute(
            "INSERT INTO macro_context (context_id, schema_version, as_of, published_at, "
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
    database = tmp_path / "macro.sqlite"
    _build_store(database, load_definitions().by_id()["us.10y"])
    application_db = tmp_path / "app.sqlite"
    initialize_application_database(application_db)
    payload = historical_context_payload()
    payload["summary"] = "  "
    with sqlite3.connect(application_db) as connection:
        connection.execute(
            "INSERT INTO macro_context (context_id, schema_version, as_of, published_at, "
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
    assert "published macro context revisions cannot be read" in captured.err


def test_main_skips_the_report_check_without_an_application_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh checkout has published nothing; absence is a valid state."""

    database = tmp_path / "macro.sqlite"
    _build_store(database, load_definitions().by_id()["us.10y"])

    exit_code = main(["--db", str(database), "--app-db", str(tmp_path / "absent.sqlite")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "skip: no application store" in captured.out
