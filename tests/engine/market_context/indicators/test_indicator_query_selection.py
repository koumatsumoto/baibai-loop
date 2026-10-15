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
