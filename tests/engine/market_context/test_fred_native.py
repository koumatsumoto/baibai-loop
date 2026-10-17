"""A successful FRED missing-cell revision cannot revive an old scalar value."""

from contextlib import closing
from datetime import date

from tests.helpers.indicator_store import observation

from baibai_engine.collection.economic_providers import fred
from baibai_engine.collection.economic_records import read_snapshots
from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_missing_cell_and_empty_replacement_do_not_restore_prior_values(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["vix"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    replies = iter(
        [
            "observation_date,VIXCLS,unused\n2026-07-24,20,kept\n2026-07-27,21,.\n",
            "observation_date,VIXCLS,unused\n2026-07-24,.,kept\n2026-07-27,21,.\n",
            "observation_date,VIXCLS,unused\n",
        ]
    )
    monkeypatch.setattr(fred, "fetch_text", lambda *args, **kwargs: next(replies))
    # An old scalar cache is deliberately present, but is not native source evidence.
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, first, 99.0)])
    service = IndicatorsService(path)
    original = service.get_range("vix", start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 20.0),
        (second, 21.0),
    ]
    corrected = service.get_range("vix", start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 21.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader("vix", first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {"observation_date": "2026-07-24", "VIXCLS": ".", "unused": "kept"},
        )
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 99.0
    assert service.get_range("vix", start=first, end=second, refresh=True).observations == ()
