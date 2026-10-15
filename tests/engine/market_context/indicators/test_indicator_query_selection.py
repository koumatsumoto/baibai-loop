from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicators import db, service
from baibai_engine.market_context.indicators.definitions import load_definitions


def test_fetch_cache_latest_and_rebuild_share_publication_vintage_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["jp.foreign_flows"]
    observed_at = date(2024, 8, 23)
    asof = date(2024, 8, 31)
    first, revised, future = (
        observation(
            definition,
            observed_at,
            value,
            vintage_at=vintage,
            period_start=date(2024, 8, 19),
            period_end=observed_at,
        )
        for value, vintage in (
            (100.0, datetime(2024, 8, 29, tzinfo=UTC)),
            (200.0, datetime(2024, 8, 30, tzinfo=UTC)),
            (300.0, datetime(2024, 9, 10, tzinfo=UTC)),
        )
    )
    with closing(db.initialize_database(path)) as connection:
        db.insert_observations(connection, [future])
        connection.commit()

    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return [first, revised]

    monkeypatch.setattr(service, "_fetch_observations_with_retry", fetch)
    monkeypatch.setattr(service, "_today_jst", lambda: asof)
    reader = service.IndicatorsService(path)
    fresh = reader.get_range(definition.series_id, start=observed_at, end=asof, refresh=True)
    cached = reader.get_range(definition.series_id, start=observed_at, end=asof)
    latest = reader.get_latest(definition.series_id, refresh=True)
    rebuilt = reader.refresh_all_history(definition.series_id, end=asof)

    assert fresh.observations == cached.observations == latest.observations == rebuilt.observations
    assert fresh.observations == (revised,)
    assert not fresh.cache_hit
    assert cached.cache_hit
    assert not latest.cache_hit
    assert not rebuilt.cache_hit
    assert len(calls) == 3
    # Selection does not discard the source's revisions or relabel future knowledge.
    with closing(db.open_read_only_connection(path)) as connection:
        assert db.latest_observation(connection, definition.series_id) == future
        counts = connection.execute("SELECT record_count FROM provider_runs").fetchall()
        assert [row[0] for row in counts] == [2, 2, 2]


def test_refresh_failure_after_inserting_values_rolls_back_the_whole_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "indicators.sqlite"
    definition = load_definitions().by_id()["us.10y"]
    day = date(2026, 7, 15)
    old = observation(definition, day, 1.0, datetime(2026, 7, 15, tzinfo=UTC))
    new = observation(definition, day, 2.0, datetime(2026, 7, 16, tzinfo=UTC))
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
