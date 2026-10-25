"""Yahoo null close revisions retain OHLCV and do not revive earlier values."""

import json
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.economic import acquisition as acquisition
from baibai_engine.data_collection.economic import store as db
from baibai_engine.data_collection.economic.providers import yahoo
from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.market_context.indicator_service import IndicatorsService
from baibai_engine.machine_analysis.market_context.indicators.definitions import load_definitions
from baibai_engine.machine_analysis.market_context.reading.reader import (
    build_store_observation_reader,
)


def test_null_close_revision_preserves_source_fields_and_removes_prior_value(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["gold"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    stamps = [
        int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
        for day in (first, second)
    ]
    metadata = {"symbol": "GC=F", "currency": "USD", "exchangeTimezoneName": "America/New_York"}

    def reply(closes):
        return json.dumps(
            {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "meta": metadata,
                            "timestamp": stamps,
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [3990, 4040],
                                        "high": [4010, 4060],
                                        "low": [3980, 4030],
                                        "volume": [100, 200],
                                        "close": closes,
                                    }
                                ],
                                "adjclose": [{"adjclose": [4000, 4050]}],
                            },
                        }
                    ],
                }
            }
        )

    replies = iter([reply([4000, 4050]), reply([None, 4050])])
    urls = []

    def fetch(session, url, **kwargs):
        urls.append(url)
        return next(replies)

    monkeypatch.setattr(yahoo, "fetch_text", fetch)
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 4000.0),
        (second, 4050.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 4050.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {
                "timestamp": stamps[0],
                "metadata": {"meta": metadata},
                "indicators": {
                    "quote": [
                        {"open": 3990, "high": 4010, "low": 3980, "volume": 100, "close": None}
                    ],
                    "adjclose": [{"adjclose": 4000}],
                },
            },
        )
    assert urls == [series.source_url, series.source_url]


def test_wrong_symbol_cannot_be_read_as_the_requested_series(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["gold"]
    day = date(2026, 7, 24)
    payload = json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "OTHER", "currency": "USD"},
                        "timestamp": [int(datetime(2026, 7, 24, tzinfo=UTC).timestamp())],
                        "indicators": {"quote": [{"close": [4000]}]},
                    }
                ],
            }
        }
    )
    monkeypatch.setattr(yahoo, "fetch_text", lambda *args, **kwargs: payload)
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=day, end=day)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with pytest.raises(ValueError, match="symbol does not match"):
        IndicatorsService(path).get_range(series.series_id, start=day, end=day)
