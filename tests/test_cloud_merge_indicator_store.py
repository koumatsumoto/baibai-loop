"""The indicator-store merge must lose no row from either side."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tools.cloud.merge_indicator_store import MergeError, main, merge_stores

from baibai_engine.macro.indicators.db import (
    SCHEMA_PATH,
    ObservationRecord,
    initialize_database,
    insert_observations,
    record_provider_run,
    seed_definitions,
)
from baibai_engine.macro.indicators.definitions import IndicatorDefinitions, load_definitions

STARTED_AT = datetime(2026, 7, 24, 11, 47, tzinfo=UTC)
# Every observation carries an explicit vintage so a row present in both stores collides on
# the primary key instead of arriving twice under two fetch times.
VINTAGE = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)


def _build_store(
    path: Path,
    *,
    series_ids: Sequence[str],
    observations: Sequence[ObservationRecord],
    run_series: Sequence[str] = (),
    generation: int = 1,
) -> None:
    registry = load_definitions().by_id()
    definitions = IndicatorDefinitions(
        series=tuple(registry[item] for item in series_ids),
        generation=generation,
    )
    connection = initialize_database(path, definitions=definitions)
    try:
        insert_observations(connection, list(observations), deduplicate_unchanged=False)
        for series_id in run_series:
            record_provider_run(
                connection,
                provider=registry[series_id].provider,
                series_id=series_id,
                start=date(2026, 7, 1),
                end=date(2026, 7, 24),
                started_at=STARTED_AT,
                status="ok",
                record_count=1,
            )
        connection.commit()
    finally:
        connection.close()


def _observation(series_id: str, day: date, value: float) -> ObservationRecord:
    return ObservationRecord(
        series_id=series_id,
        observed_at=day,
        value=value,
        unit="percent",
        source_url="https://example.com/series",
        vintage_at=VINTAGE,
    )


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def _observations(path: Path) -> set[tuple[object, ...]]:
    return set(_rows(path, "SELECT series_id, observed_at, value FROM observations"))


def test_merge_adds_source_only_rows_and_keeps_the_target_value(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(
            _observation("us.10y", date(2026, 7, 23), 4.31),
            _observation("us.10y", date(2026, 7, 20), 4.20),
        ),
    )
    _build_store(
        local,
        series_ids=("us.10y", "jp.2y"),
        observations=(
            _observation("us.10y", date(2016, 7, 20), 1.55),
            _observation("us.10y", date(2026, 7, 20), 4.99),
        ),
    )

    report = merge_stores(cloud, local)

    # The cloud-only day arrives, the deep history stays, and the day both stores hold keeps
    # the target's value: the merge adds rows and never rewrites what the target knows.
    assert _observations(local) == {
        ("us.10y", "2026-07-23", 4.31),
        ("us.10y", "2016-07-20", 1.55),
        ("us.10y", "2026-07-20", 4.99),
    }
    observations = next(item for item in report.tables if item.table == "observations")
    assert (observations.source_rows, observations.inserted, observations.target_rows_after) == (
        2,
        1,
        3,
    )


def test_merge_is_idempotent(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 23), 4.31),),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )

    first = merge_stores(cloud, local)
    second = merge_stores(cloud, local)

    assert first.inserted == 1
    assert second.inserted == 0


def test_merge_carries_the_cloud_fetch_record_of_a_registered_series(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("jp.10y", date(2026, 7, 23), 1.62),),
        run_series=("jp.10y",),
    )
    _build_store(
        local,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )

    merge_stores(cloud, local)

    assert ("jp.10y", "2026-07-23", 1.62) in _observations(local)
    # The cloud's own fetch record survives, which is what the data-health panel reads to
    # tell a silent provider from a merely stale series.
    assert _rows(local, "SELECT series_id FROM provider_runs") == [("jp.10y",)]
    assert _rows(local, "PRAGMA foreign_key_check") == []


def test_merge_skips_facts_of_a_series_the_target_registry_does_not_define(
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(
            _observation("jp.10y", date(2026, 7, 23), 1.62),
            _observation("us.10y", date(2026, 7, 23), 4.31),
        ),
        run_series=("jp.10y",),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
        generation=2,
    )

    report = merge_stores(cloud, local)

    # A series only the cloud holds is retired relative to the target's explicit
    # registry snapshot, so carrying it would publish facts the target does not define.
    assert _rows(local, "SELECT series_id FROM series") == [("us.10y",)]
    assert _observations(local) == {
        ("us.10y", "2016-07-20", 1.55),
        ("us.10y", "2026-07-23", 4.31),
    }
    assert _rows(local, "SELECT series_id FROM provider_runs") == []
    assert report.retired_series == ("jp.10y",)
    assert report.skipped == 2
    assert "jp.10y" in report.render()


def test_merge_rejects_source_series_missing_from_same_generation_target(
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("jp.10y", date(2026, 7, 23), 1.62),),
        run_series=("jp.10y",),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )

    with pytest.raises(
        MergeError,
        match=r"source registry contains series missing from same-generation target: jp\.10y",
    ):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_rejects_zero_fact_source_series_missing_from_same_generation_target(
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(),
    )
    before = {
        table: _rows(local, f'SELECT * FROM "{table}" ORDER BY 1')
        for table in ("series", "aliases", "observations", "provider_runs")
    }

    with pytest.raises(
        MergeError,
        match=r"source registry contains series missing from same-generation target: jp\.10y",
    ):
        merge_stores(cloud, local)

    after = {
        table: _rows(local, f'SELECT * FROM "{table}" ORDER BY 1')
        for table in ("series", "aliases", "observations", "provider_runs")
    }
    assert after == before


def test_merge_uses_retained_target_metadata_after_stale_registry_open(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("jp.10y", date(2026, 7, 23), 1.62),),
        run_series=("jp.10y",),
    )
    _build_store(
        local,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    registry = load_definitions().by_id()
    stale_definitions = IndicatorDefinitions(series=(registry["us.10y"],))
    with sqlite3.connect(local) as connection:
        seed_definitions(connection, stale_definitions)
        connection.commit()

    report = merge_stores(cloud, local)

    # A stale branch's ordinary open keeps metadata for the newer jp.10y series,
    # so its cloud facts remain mergeable and cannot be lost on upload.
    assert ("jp.10y",) in _rows(local, "SELECT series_id FROM series")
    assert ("jp.10y", "2026-07-23", 1.62) in _observations(local)
    assert report.retired_series == ()
    assert report.inserted == 2
    assert report.skipped == 0


def test_merge_rejects_target_with_older_registry_generation(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y", "jp.10y"),
        observations=(_observation("jp.10y", date(2026, 7, 23), 1.62),),
        generation=2,
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 22), 4.31),),
        generation=1,
    )

    with pytest.raises(
        MergeError,
        match="target registry generation is older than source; target=1, source=2",
    ):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2026-07-22", 4.31)}


def test_merge_leaves_the_registry_owned_tables_to_the_target(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 23), 4.31),),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    # An alias the registry no longer generates, left in the cloud copy by an earlier publish.
    with sqlite3.connect(cloud) as connection:
        connection.execute("INSERT INTO aliases(alias, series_id) VALUES ('JGB 10Y', 'us.10y')")

    merge_stores(cloud, local)

    assert ("JGB 10Y",) not in _rows(local, "SELECT alias FROM aliases")


def test_merge_refuses_a_store_on_a_different_schema(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 23), 4.31),),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    with sqlite3.connect(cloud) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(MergeError, match="schema is 99"):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_rejects_source_value_outside_target_band_without_changing_target(
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(cloud, series_ids=("us.10y",), observations=())
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    _inject_source_observation(
        cloud,
        _observation("us.10y", date(2026, 7, 23), 999.0),
    )

    with pytest.raises(
        MergeError,
        match=r"us\.10y 2026-07-23 value 999.*outside target plausible range \[-20, 30\]",
    ):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_rejects_source_unit_mismatch_without_changing_target(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(cloud, series_ids=("us.10y",), observations=())
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    _inject_source_observation(
        cloud,
        ObservationRecord(
            series_id="us.10y",
            observed_at=date(2026, 7, 23),
            value=4.31,
            unit="basis-points",
            source_url="https://example.com/series",
            vintage_at=VINTAGE,
        ),
    )

    with pytest.raises(
        MergeError,
        match=r"us\.10y 2026-07-23 has unit 'basis-points'.*expects 'percent'",
    ):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_rejects_an_incomplete_active_target_band(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 23), 4.31),),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    with sqlite3.connect(local) as connection:
        connection.execute(
            "UPDATE series SET plausible_min = NULL, plausible_max = NULL "
            "WHERE series_id = 'us.10y'"
        )

    with pytest.raises(MergeError, match=r"us\.10y has an incomplete plausible range"):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_rejects_a_store_with_a_missing_contract_trigger(tmp_path: Path) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(cloud, series_ids=("us.10y",), observations=())
    _build_store(local, series_ids=("us.10y",), observations=())
    with sqlite3.connect(local) as connection:
        connection.execute("DROP TRIGGER validate_observation_plausibility_before_insert")

    with pytest.raises(MergeError, match=r"trigger contract mismatch.*missing"):
        merge_stores(cloud, local)


def test_merge_rejects_an_unexpected_target_trigger_before_it_can_delete_history(
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    _build_store(
        cloud,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2026, 7, 23), 4.31),),
    )
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )
    with sqlite3.connect(local) as connection:
        connection.execute(
            "CREATE TRIGGER unexpected_history_delete AFTER INSERT ON observations "
            "BEGIN DELETE FROM observations WHERE observed_at < NEW.observed_at; END"
        )

    with pytest.raises(MergeError, match=r"trigger contract mismatch.*unexpected"):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_main_reports_a_missing_store(tmp_path: Path, capsys) -> None:
    local = tmp_path / "local.sqlite"
    _build_store(
        local,
        series_ids=("us.10y",),
        observations=(_observation("us.10y", date(2016, 7, 20), 1.55),),
    )

    assert main(["--source", str(tmp_path / "absent.sqlite"), "--target", str(local)]) == 1

    assert "does not exist" in capsys.readouterr().err


def _inject_source_observation(path: Path, observation: ObservationRecord) -> None:
    """Model a source produced by an older or faulty writer for merge preflight tests."""

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER validate_observation_plausibility_before_insert")
        insert_observations(connection, [observation], deduplicate_unchanged=False)
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()
