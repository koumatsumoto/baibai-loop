"""A null BOJ workbook cell supersedes prior numbers without losing the row."""

from contextlib import closing
from datetime import date

from tests.engine.market_context.indicators.indicator_fixtures import _boj_workbook_bytes

from baibai_engine.data_collection.economic import acquisition as acquisition
from baibai_engine.data_collection.economic import store as db
from baibai_engine.data_collection.economic.providers import boj
from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.market_context.indicator_service import IndicatorsService
from baibai_engine.machine_analysis.market_context.indicators.definitions import load_definitions
from baibai_engine.machine_analysis.market_context.reading.reader import build_store_observation_reader


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
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 350000.0),
        (second, 360000.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 360000.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        row = snapshot.records[0]
        assert row["row"][:4] == [None, "2026-05-31T00:00:00", None, 17]
        assert row["preamble"][0][2] == "Monetary Base"
        assert row["preamble"][1][7] == "Unit: 100 million yen"
        assert row["sheet"] == "Sheet"
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    assert service.get_range(series.series_id, start=first, end=second).observations == ()
