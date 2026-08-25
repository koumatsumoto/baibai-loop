"""The indicator-store merge must lose no row from either side."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import downgrade_to_previous_schema, observation

from baibai_batch.storage.merge_indicator_store import (
    MergeError,
    _internal_name,
    _schema_name,
    main,
    merge_stores,
)
from baibai_engine.macro.indicators.db import (
    SCHEMA_PATH,
    ObservationRecord,
    initialize_database,
    insert_observations,
    observations_in_range,
    record_provider_run,
    retract_observations,
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
    """One reading at this file's fixed vintage."""

    return observation(series_id, day, value, VINTAGE, source_url="https://example.com/series")


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def _observations(path: Path) -> set[tuple[object, ...]]:
    return set(_rows(path, "SELECT series_id, observed_at, value FROM observations"))


def test_merge_rejects_different_payload_for_the_same_observation_key(tmp_path: Path) -> None:
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

    with pytest.raises(
        MergeError,
        match="observations payload disagrees for shared key",
    ):
        merge_stores(cloud, local)

    # A conflicting vintage makes the source/target lineage ambiguous, so no
    # source-only row may be committed alongside it.
    assert _observations(local) == {
        ("us.10y", "2016-07-20", 1.55),
        ("us.10y", "2026-07-20", 4.99),
    }


def test_merge_rejects_different_payload_for_the_same_provider_run_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    _build_store(source, series_ids=("us.10y",), observations=())
    _build_store(target, series_ids=("us.10y",), observations=())
    insert_sql = (
        "INSERT INTO provider_runs("
        "run_id, provider, series_id, range_start, range_end, started_at, finished_at, "
        "status, record_count, error_message"
        ") VALUES ('shared-run', 'fred_csv', 'us.10y', '2026-07-01', '2026-07-24', "
        "'2026-07-24T00:00:00+00:00', '2026-07-24T00:00:01+00:00', 'ok', ?, NULL)"
    )
    with sqlite3.connect(source) as connection:
        connection.execute(insert_sql, (1,))
    with sqlite3.connect(target) as connection:
        connection.execute(insert_sql, (2,))

    with pytest.raises(
        MergeError,
        match="provider_runs payload disagrees for shared key",
    ):
        merge_stores(source, target)

    assert _rows(target, "SELECT run_id, record_count FROM provider_runs") == [("shared-run", 2)]


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


def test_merge_carries_a_retraction_to_the_other_store(tmp_path: Path) -> None:
    """The retraction has to travel like any fact, or the next push undoes the decision."""

    cloud = tmp_path / "cloud.sqlite"
    local = tmp_path / "local.sqlite"
    observation = _observation("jp.10y", date(2026, 7, 23), 1.62)
    _build_store(cloud, series_ids=("jp.10y",), observations=(observation,))
    _build_store(local, series_ids=("jp.10y",), observations=(observation,))
    connection = sqlite3.connect(local)
    connection.row_factory = sqlite3.Row
    try:
        retract_observations(
            connection,
            "jp.10y",
            [(date(2026, 7, 23), VINTAGE)],
            vintage_at=datetime(2026, 7, 25, tzinfo=UTC),
        )
        connection.commit()
    finally:
        connection.close()

    pull = merge_stores(cloud, local)
    push = merge_stores(local, cloud)

    assert pull.inserted == 0
    assert push.inserted == 1
    assert _rows(
        cloud,
        "SELECT observed_at, fetch_status FROM observations ORDER BY vintage_at",
    ) == [("2026-07-23", "ok"), ("2026-07-23", "retracted")]
    assert _read_range(cloud) == ()
    assert _read_range(local) == ()


def _read_range(path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return tuple(
            observations_in_range(connection, "jp.10y", date(2026, 7, 1), date(2026, 7, 31))
        )
    finally:
        connection.close()


def test_merge_refuses_a_source_behind_the_target(tmp_path: Path) -> None:
    """Both stores must be on the schema this code writes, whatever the gap.

    The source is the copy R2 holds, and the transfer script moves it forward before the
    merge sees it. Accepting a behind source here instead would mean merging under two
    different shapes and deciding which one the payload comparison speaks about.
    """
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    observation = _observation("jp.10y", date(2026, 7, 23), 1.62)
    _build_store(source, series_ids=("jp.10y",), observations=(observation,))
    _build_store(target, series_ids=("jp.10y",), observations=())
    downgrade_to_previous_schema(source)

    with pytest.raises(MergeError, match=r"schema is \d+ but this code expects"):
        merge_stores(source, target)

    assert _rows(target, "SELECT observed_at, value FROM observations") == []


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


def test_merge_rejects_target_value_outside_its_registry_band(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    _build_store(source, series_ids=("us.10y",), observations=())
    _build_store(target, series_ids=("us.10y",), observations=())
    _inject_source_observation(
        target,
        _observation("us.10y", date(2026, 7, 23), 999.0),
    )

    with pytest.raises(
        MergeError,
        match=r"main observation us\.10y 2026-07-23 value 999.*outside target",
    ):
        merge_stores(source, target)

    assert _observations(target) == {("us.10y", "2026-07-23", 999.0)}


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


def test_merge_rejects_a_store_column_name_that_is_not_an_identifier(tmp_path: Path) -> None:
    """Column names reach the statements as text, so a store may only carry plain ones."""

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
        connection.execute('ALTER TABLE observations ADD COLUMN "value"" , 1 AS x" TEXT')

    with pytest.raises(MergeError, match=r"column name is not a plain identifier"):
        merge_stores(cloud, local)

    assert _observations(local) == {("us.10y", "2016-07-20", 1.55)}


def test_merge_refuses_a_schema_it_does_not_attach() -> None:
    with pytest.raises(ValueError, match=r"unsupported SQLite schema name"):
        _schema_name("main; DROP TABLE observations")


def test_merge_refuses_a_table_name_that_is_not_an_identifier() -> None:
    with pytest.raises(ValueError, match=r"invalid SQL identifier"):
        _internal_name('observations" --')


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


def _contract_trigger_sql(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN ("
            "'validate_observation_plausibility_before_insert', "
            "'validate_observation_plausibility_before_update', "
            "'validate_series_contract_before_update'"
            ") ORDER BY name"
        )
    )
