"""MOF current-month missing corrections supersede history, without discarding it."""

from contextlib import closing
from datetime import date
from urllib.parse import urljoin

from tests.helpers.indicator_store import observation

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators import db
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.providers import mof_jgb
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots
from baibai_engine.market_context.reading.reader import build_store_observation_reader


def test_current_month_missing_cell_overrides_history_and_prior_scalar(tmp_path, monkeypatch):
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
    with closing(db.initialize_database(path)) as connection, connection:
        db.insert_observations(connection, [observation(series, first, 9.0)])
    service = IndicatorsService(path)
    original = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 1.2),
        (second, 1.1),
    ]
    corrected = service.get_range(series.series_id, start=first, end=second, refresh=True)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 1.1)]
    current_url = urljoin(series.source_url, "../jgbcm.csv")
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        assert reader("jp.10y", first, first)[0].value == 2.3
        snapshot = next(
            read_snapshots(connection, first, first, source_key=native_source_key(series))
        )
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
        assert connection.execute("SELECT value FROM observations").fetchone()[0] == 9.0
    assert (
        service.get_range(series.series_id, start=first, end=second, refresh=True).observations
        == ()
    )
    assert urls == [series.source_url, current_url] * 3
