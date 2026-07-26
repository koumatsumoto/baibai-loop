from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tools.validate_indicator_store import validate_store

from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    ObservationRecord,
    initialize_database,
    insert_observations,
    open_connection,
)
from baibai_engine.macro.indicators.definitions import (
    IndicatorDefinitions,
    SeriesDefinition,
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
                    series_id="test.series",
                    observed_at=date(2026, 7, 1),
                    value=4.2,
                    unit="percent",
                    source_url="https://example.com/data.csv",
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
        (
            "DROP TABLE registry_prune_authorizations",
            "registry table contract mismatch.*registry_prune_authorizations",
        ),
        (
            "INSERT INTO registry_prune_authorizations(series_id) VALUES ('test.series')",
            "registry prune authorization state.*must be empty",
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


def test_validate_store_scans_a_v2_store_without_modifying_or_creating_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "macro.sqlite"
    definition = _definition()
    _build_store(database, definition)
    with sqlite3.connect(database) as connection:
        for trigger in (
            "validate_observation_plausibility_before_insert",
            "validate_observation_plausibility_before_update",
            "validate_series_contract_before_update",
            "protect_series_from_implicit_prune",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE registry_prune_authorizations")
        connection.execute("DROP TABLE registry_state")
        connection.execute("ALTER TABLE series DROP COLUMN plausible_max")
        connection.execute("ALTER TABLE series DROP COLUMN plausible_min")
        connection.execute("PRAGMA user_version = 2")
    before = database.stat()

    report = validate_store(
        database,
        definitions=IndicatorDefinitions(series=(definition,)),
    )

    after = database.stat()
    assert report.valid
    assert report.schema_version == 2
    assert report.observations == 1
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not Path(f"{database}-journal").exists()
