"""BOJ null corrections are source records, not permission to reuse old numbers."""

import json
from contextlib import closing
from datetime import date

from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.providers import boj_timeseries
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_null_revision_does_not_restore_scalar_cache_and_retains_metadata(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.policy_rate"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    def reply(values):
        return json.dumps({"STATUS": 200, "MESSAGE": "synthetic", "RESULTSET": [{
            "SERIES_CODE": "STRDCLUCON", "UNIT": "Percent", "NOTE": "kept",
            "VALUES": {"SURVEY_DATES": [20260724, 20260727], "VALUES": values, "OTHER": "retained"},
        }]})
    replies = iter([reply([0.5, 0.6]), reply([None, 0.6])])
    monkeypatch.setattr(boj_timeseries, "fetch_text", lambda *args, **kwargs: next(replies))
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, first, 9.0)])
    service = IndicatorsService(path)
    original = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in original.observations] == [(first, 0.5), (second, 0.6)]
    corrected = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 0.6)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_key=native_source_key(series)))
        assert snapshot.records == ({
            "response_metadata": {"STATUS": 200, "MESSAGE": "synthetic"},
            "series_metadata": {"SERIES_CODE": "STRDCLUCON", "UNIT": "Percent", "NOTE": "kept"},
            "values_metadata": {"OTHER": "retained"}, "survey_date": 20260724, "value": None,
        },)
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 9.0
