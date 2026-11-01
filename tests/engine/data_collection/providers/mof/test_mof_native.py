"""MOF current-month missing corrections supersede history, without discarding it."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest import TestCase
from urllib.parse import urljoin

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _source

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.mof.jgb as mof_jgb
from baibai_engine.data_collection.providers.mof.jgb import parse_mof_jgb_csv
from baibai_engine.data_collection.receipts.native_sources import store as db
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
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


class NativeParserTests(TestCase):
    def test_parse_mof_jgb_csv_preserves_all_yields_and_missing_cells(self) -> None:
        series = _source("mof_jgb")
        text = "\n".join(
            [
                "国債金利情報,,,,,,,,,,,(単位 : %)",
                "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年",
                "R8.7.7,1.168,1.402,1.565,1.816,2.007,2.172,2.342,2.524,2.68,2.834,3.415",
                "R8.7.8,1.18,1.433,1.59,1.843,2.039,2.198,2.368,2.552,2.705,2.856,3.445",
                "R8.7.9,1.18,1.433,1.59,1.843,2.039,2.198,2.368,2.552,2.705,-,3.445",
            ]
        )

        observations = parse_mof_jgb_csv(
            series,
            text,
            start=date(2026, 7, 8),
            end=date(2026, 7, 9),
        )

        self.assertEqual(list(observations), [date(2026, 7, 8), date(2026, 7, 9)])
        self.assertEqual(observations[date(2026, 7, 8)][0]["row"]["10年"], "2.856")
        self.assertEqual(observations[date(2026, 7, 9)][0]["row"]["10年"], "-")
        self.assertEqual(observations[date(2026, 7, 9)][0]["row"]["15年"], "3.445")
