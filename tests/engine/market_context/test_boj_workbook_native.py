"""A null BOJ workbook cell supersedes prior numbers without losing the row."""

from contextlib import closing
from datetime import date

from tests.engine.market_context.indicators.indicator_fixtures import _boj_workbook_bytes
from tests.helpers.indicator_store import observation

from baibai_engine.collection.economic_providers import boj
from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_null_revision_and_all_null_workbook_leave_no_stale_reading(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.monetary_base"]
    first, second = date(2026, 5, 1), date(2026, 6, 1)
    replies = iter(
        [
            _boj_workbook_bytes(
                [(None, date(2026, 5, 31), 350000, 17), (None, date(2026, 6, 30), 360000, 18)]
            ),
            _boj_workbook_bytes(
                [(None, date(2026, 5, 31), None, 17), (None, date(2026, 6, 30), 360000, 18)]
            ),
            _boj_workbook_bytes(
                [(None, date(2026, 5, 31), None, 17), (None, date(2026, 6, 30), None, 18)]
            ),
        ]
    )
    monkeypatch.setattr(boj, "fetch_bytes", lambda *args, **kwargs: next(replies))
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, first, 99999.0)])
    service = IndicatorsService(path)
    original = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 350000.0),
        (second, 360000.0),
    ]
    corrected = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 360000.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(
            read_snapshots(connection, first, first, source_key=native_source_key(series))
        )
        row = snapshot.records[0]
        assert row["row"][:4] == [None, "2026-05-31T00:00:00", None, 17]
        assert row["preamble"][0][2] == "Monetary Base"
        assert row["preamble"][1][7] == "Unit: 100 million yen"
        assert row["sheet"] == "Sheet"
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 99999.0
    assert (
        service.get_range(series.series_id, start=first, end=second, refresh=True).observations
        == ()
    )
