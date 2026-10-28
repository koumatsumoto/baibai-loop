"""Latest means the last available saved observation, not a freshness filter."""

from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.economic.records import store_snapshot
from baibai_engine.data_collection.economic.store import initialize_database
from baibai_engine.machine_analysis.models.market_context import indicator_service
from baibai_engine.machine_analysis.models.market_context.indicators.definitions import load_definitions


@pytest.mark.parametrize("age", [14, 15, 60, 400, 22000])
def test_latest_retains_old_observation_date_and_never_fetches(tmp_path, monkeypatch, age):
    today = date(2026, 10, 27)
    day = today - timedelta(days=age)
    monkeypatch.setattr(indicator_service, "_today_jst", lambda: today)
    path = tmp_path / "economic.sqlite"
    series = load_definitions().by_id()["us.10y"]
    with closing(initialize_database(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id=series.source_id,
            observed_at=day,
            fetched_at=datetime(2026, 10, 27, tzinfo=UTC),
            records=[{"row": {"Time Period": day.isoformat(), "RIFLGFCY10_N.B": "4.0"}}],
        )
        # A more recent explicitly missing value is not filled with the old one.
        store_snapshot(
            connection,
            source_id=series.source_id,
            observed_at=today,
            fetched_at=datetime(2026, 10, 27, tzinfo=UTC),
            records=[{"row": {"Time Period": today.isoformat(), "RIFLGFCY10_N.B": "ND"}}],
        )
    service = indicator_service.IndicatorsService(path)
    latest = service.get_latest(series.series_id)
    assert [(value.observed_at, value.value) for value in latest.observations] == [(day, 4.0)]
    assert service.get_range(series.series_id, start=today, end=today).observations == ()


def test_old_observation_uses_today_publication_cutoff(tmp_path, monkeypatch):
    today = date(2026, 10, 27)
    day = date(2026, 1, 9)
    monkeypatch.setattr(indicator_service, "_today_jst", lambda: today)
    path = tmp_path / "economic.sqlite"
    series = load_definitions().by_id()["jp.foreign_flows"]
    with closing(initialize_database(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id=series.source_id,
            observed_at=today,
            fetched_at=datetime(2026, 10, 27, tzinfo=UTC),
            records=[
                {
                    "Section": "TSEPrime",
                    "PubDate": today.isoformat(),
                    "StDate": "2026-01-05",
                    "EnDate": day.isoformat(),
                    "FrgnBal": 100,
                }
            ],
        )
    result = indicator_service.IndicatorsService(path).get_latest(series.series_id)
    assert [(value.observed_at, value.value) for value in result.observations] == [(day, 100)]
