"""MOF current-month missing corrections supersede history, without discarding it."""

from contextlib import closing
from datetime import date
from urllib.parse import urljoin

from baibai_engine.data_collection.economic import acquisition as acquisition
from baibai_engine.data_collection.economic import store as db
from baibai_engine.data_collection.economic.providers import mof_jgb
from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.service import IndicatorsService
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import load_definitions
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)


def test_current_month_missing_cell_overrides_history_and_prior_value(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.2y"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    header = "synthetic rates,unit percent\n基準日,2年,10年\n"
    history = header + "R8.7.24,1.0,2.0\nR8.7.27,1.1,2.1\n"
    replies = iter(
        [
            history,
            header + "R8.7.24,1.2,2.2\n",
            history,
            header + "R8.7.24,-,2.3\n",
            header,
            header,
        ]
    )
    urls = []

    def fetch(session, url, **kwargs):
        urls.append(url)
        return next(replies).encode("cp932")

    monkeypatch.setattr(mof_jgb, "fetch_bytes", fetch)
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 1.2),
        (second, 1.1),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 1.1)]
    current_url = urljoin(series.source_url, "../jgbcm.csv")
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        assert reader("jp.10y", first, first)[0].value == 2.3
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {
                "row": {"基準日": "R8.7.24", "2年": "1.0", "10年": "2.0"},
                "source_url": series.source_url,
                "preamble": [["synthetic rates", "unit percent"]],
            },
            {
                "row": {"基準日": "R8.7.24", "2年": "-", "10年": "2.3"},
                "source_url": current_url,
                "preamble": [["synthetic rates", "unit percent"]],
            },
        )
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    assert service.get_range(series.series_id, start=first, end=second).observations == ()
    assert urls == [series.source_url, current_url] * 3
