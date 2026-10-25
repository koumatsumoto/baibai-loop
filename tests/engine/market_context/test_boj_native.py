"""BOJ null corrections are source records, not permission to reuse old numbers."""

import json
from contextlib import closing
from datetime import date

from baibai_engine.data_collection.economic import acquisition as acquisition
from baibai_engine.data_collection.economic import store as db
from baibai_engine.data_collection.economic.providers import boj_timeseries
from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.market_context.indicator_service import IndicatorsService
from baibai_engine.machine_analysis.market_context.indicators.definitions import load_definitions
from baibai_engine.machine_analysis.market_context.reading.reader import (
    build_store_observation_reader,
)


def test_null_revision_does_not_restore_prior_value_and_retains_metadata(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.policy_rate"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)

    def reply(values):
        return json.dumps(
            {
                "STATUS": 200,
                "MESSAGE": "synthetic",
                "RESULTSET": [
                    {
                        "SERIES_CODE": "STRDCLUCON",
                        "UNIT": "Percent",
                        "NOTE": "kept",
                        "VALUES": {
                            "SURVEY_DATES": [20260724, 20260727],
                            "VALUES": values,
                            "OTHER": "retained",
                        },
                    }
                ],
            }
        )

    replies = iter([reply([0.5, 0.6]), reply([None, 0.6])])
    monkeypatch.setattr(boj_timeseries, "fetch_text", lambda *args, **kwargs: next(replies))
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 0.5),
        (second, 0.6),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 0.6)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {
                "response_metadata": {"STATUS": 200, "MESSAGE": "synthetic"},
                "series_metadata": {"SERIES_CODE": "STRDCLUCON", "UNIT": "Percent", "NOTE": "kept"},
                "values_metadata": {"OTHER": "retained"},
                "survey_date": 20260724,
                "value": None,
            },
        )
