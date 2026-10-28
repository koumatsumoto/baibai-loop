"""UMich missing revisions retain the source row and remove the old L2 value."""

from contextlib import closing
from datetime import date

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.receipts.native_sources import store as db
import baibai_engine.data_collection.providers.university_of_michigan.sentiment as umich_sca
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.service import IndicatorsService
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import load_definitions
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)


def test_missing_revision_and_empty_history_do_not_restore_prior_value(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.consumer_sentiment"]
    first, second = date(2026, 5, 1), date(2026, 6, 1)
    replies = iter(
        [
            "Month,YYYY,ICS_ALL,unused\nMay,2026,50,kept\nJune,2026,51,other\n",
            "Month,YYYY,ICS_ALL,unused\nMay,2026,,kept\nJune,2026,51,other\n",
            "Month,YYYY,ICS_ALL,unused\n",
        ]
    )
    monkeypatch.setattr(umich_sca, "fetch_text", lambda *args, **kwargs: next(replies))
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 50.0),
        (second, 51.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 51.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {"Month": "May", "YYYY": "2026", "ICS_ALL": "", "unused": "kept"},
        )
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    assert service.get_range(series.series_id, start=first, end=second).observations == ()
