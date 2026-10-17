from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.market_context import indicator_service as service
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots, store_snapshot
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_fetch_cache_latest_and_rebuild_share_publication_vintage_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["jp.foreign_flows"]
    observed_at = date(2024, 8, 23)
    asof = date(2024, 8, 31)
    publications = {
        published: [
            {
                "Section": "TSEPrime",
                "PubDate": published.isoformat(),
                "StDate": "2024-08-19",
                "EnDate": observed_at.isoformat(),
                "FrgnBal": value,
            }
        ]
        for published, value in [(date(2024, 8, 29), 100), (date(2024, 8, 30), 200)]
    }
    future_date = date(2024, 9, 10)
    with closing(db.initialize_database(path)) as connection:
        store_snapshot(
            connection,
            source_key="jquants_flows:TSEPrime",
            observed_at=future_date,
            fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
            records=[
                {
                    "Section": "TSEPrime",
                    "PubDate": future_date.isoformat(),
                    "StDate": "2024-08-19",
                    "EnDate": observed_at.isoformat(),
                    "FrgnBal": 300,
                }
            ],
        )
        connection.commit()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return publications

    monkeypatch.setattr(service, "fetch_native_records", fetch)
    monkeypatch.setattr(service, "_today_jst", lambda: asof)
    reader = service.IndicatorsService(path)
    fresh = reader.get_range(definition.series_id, start=observed_at, end=asof, refresh=True)
    cached = reader.get_range(definition.series_id, start=observed_at, end=asof)
    latest = reader.get_latest(definition.series_id, refresh=True)
    rebuilt = reader.refresh_all_history(definition.series_id, end=asof)
    assert fresh.observations == cached.observations == latest.observations == rebuilt.observations
    assert [item.value for item in fresh.observations] == [200]
    assert fresh.observations[0].vintage_at == datetime(2024, 8, 30, tzinfo=UTC)
    assert not fresh.cache_hit
    assert cached.cache_hit
    assert not latest.cache_hit
    assert not rebuilt.cache_hit
    assert len(calls) == 3
    with closing(db.open_read_only_connection(path)) as connection:
        future = build_store_observation_reader(
            connection, series=[definition], vintage_cutoff=future_date
        )
        assert [item.value for item in future(definition.series_id, observed_at, observed_at)] == [
            300
        ]
        assert (
            next(
                read_snapshots(
                    connection, future_date, future_date, source_key="jquants_flows:TSEPrime"
                )
            ).records[0]["FrgnBal"]
            == 300
        )
        # Two received publication records produce one selected L2 observation.
        assert [row[0] for row in connection.execute("SELECT record_count FROM provider_runs")] == [
            2,
            2,
            2,
        ]


def test_refresh_failure_after_inserting_values_rolls_back_the_whole_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["jp.bankruptcies"]
    day = date(2026, 7, 1)
    old = observation(definition, day, 1000.0, datetime(2026, 7, 15, tzinfo=UTC))
    new = observation(definition, day, 1001.0, datetime(2026, 7, 16, tzinfo=UTC))
    with closing(db.initialize_database(path)) as connection:
        db.insert_observations(connection, [old])
        connection.commit()

    record_run = db.record_provider_run

    def fail_success_record(connection, **kwargs):
        if kwargs["status"] == "ok":
            raise RuntimeError("cannot record successful refresh")
        return record_run(connection, **kwargs)

    monkeypatch.setattr(service, "_fetch_observations_with_retry", lambda *args, **kwargs: [new])
    monkeypatch.setattr(db, "record_provider_run", fail_success_record)
    with pytest.raises(RuntimeError, match="cannot record successful refresh"):
        service.IndicatorsService(path).get_range(
            definition.series_id, start=day, end=day, refresh=True
        )
    with closing(db.open_read_only_connection(path)) as connection:
        assert db.observations_in_range(connection, definition.series_id, day, day) == (old,)
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        run = connection.execute("SELECT status, record_count FROM provider_runs").fetchone()
        assert tuple(run) == ("failed", 0)


def test_retraction_failure_after_rewrite_does_not_commit_a_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["jp.bankruptcies"]
    day = date(2026, 6, 1)
    vintage = datetime(2026, 7, 10, tzinfo=UTC)
    original = observation(definition, day, 1000.0, vintage)
    with closing(db.initialize_database(path)) as connection:
        db.insert_observations(connection, [original])
        connection.commit()

    retract = db.retract_observations

    def fail_after_rewrite(connection, *args, **kwargs):
        retract(connection, *args, **kwargs)
        raise RuntimeError("retraction interrupted after rewrite")

    monkeypatch.setattr(db, "retract_observations", fail_after_rewrite)
    with pytest.raises(RuntimeError, match="retraction interrupted"):
        service.IndicatorsService(path).retract(definition.series_id, [(day, vintage)])
    with closing(db.open_read_only_connection(path)) as connection:
        assert db.observations_in_range(connection, definition.series_id, day, day) == (original,)
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_vintage_order_and_identity_use_instants_not_timezone_spelling(tmp_path: Path) -> None:
    definition = load_definitions().by_id()["us.10y"]
    day = date(2026, 10, 15)
    old_jst = datetime.fromisoformat("2026-10-16T09:00:00+09:00")
    new_utc = datetime.fromisoformat("2026-10-16T01:00:00+00:00")
    with closing(db.initialize_database(tmp_path / "indicators.sqlite")) as connection:
        db.insert_observations(connection, [observation(definition, day, 1.0, old_jst)])
        db.insert_observations(connection, [observation(definition, day, 2.0, new_utc)])
        selected = db.latest_observation(connection, definition.series_id)
        assert selected is not None
        assert selected.value == 2.0
        assert selected.vintage_at == new_utc
        assert db.observations_in_range(connection, definition.series_id, day, day) == (selected,)
        db.insert_observations(
            connection,
            [observation(definition, day, 1.0, old_jst.astimezone(UTC))],
            deduplicate_unchanged=False,
        )
        assert db.row_count(connection, "observations") == 2
        assert db.latest_observation(connection, definition.series_id) == selected


def test_observation_vintage_without_timezone_is_not_guessed() -> None:
    definition = load_definitions().by_id()["us.10y"]
    with pytest.raises(ValueError, match="vintage_at must include a timezone"):
        observation(
            definition,
            date(2026, 10, 15),
            1.0,
            datetime(2026, 10, 16, tzinfo=UTC).replace(tzinfo=None),
        )
