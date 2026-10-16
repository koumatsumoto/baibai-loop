"""UMich missing revisions retain the source row and remove the old L2 value."""

from contextlib import closing
from datetime import date

from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.providers import umich_sca
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_missing_revision_and_empty_history_do_not_restore_scalar_cache(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.consumer_sentiment"]
    first, second = date(2026, 5, 1), date(2026, 6, 1)
    replies = iter([
        "Month,YYYY,ICS_ALL,unused\nMay,2026,50,kept\nJune,2026,51,other\n",
        "Month,YYYY,ICS_ALL,unused\nMay,2026,,kept\nJune,2026,51,other\n",
        "Month,YYYY,ICS_ALL,unused\n",
    ])
    monkeypatch.setattr(umich_sca, "fetch_text", lambda *args, **kwargs: next(replies))
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, first, 99.0)])
    service = IndicatorsService(path)
    original = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in original.observations] == [(first, 50.0), (second, 51.0)]
    corrected = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 51.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_key=native_source_key(series)))
        assert snapshot.records == ({"Month": "May", "YYYY": "2026", "ICS_ALL": "", "unused": "kept"},)
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 99.0
    assert service.get_range(series.series_id, start=first, end=second, refresh=True).observations == ()
