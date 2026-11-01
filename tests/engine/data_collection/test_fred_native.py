"""A successful FRED missing-cell revision cannot revive an earlier value."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _source

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.providers import fred
from baibai_engine.data_collection.providers.fred import parse_fred_csv
from baibai_engine.data_collection.receipts.native_sources import store as db
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.transport.context import IndicatorsProviderError
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)


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
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range("vix", start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 20.0),
        (second, 21.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range("vix", start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 21.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader("vix", first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {"observation_date": "2026-07-24", "VIXCLS": ".", "unused": "kept"},
        )
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    assert service.get_range("vix", start=first, end=second).observations == ()


class NativeParserTests(TestCase):
    def test_parse_fred_csv_filters_range_without_discarding_missing_cells(self) -> None:
        series = _source("fred_csv", "DGS10")
        text = "observation_date,DGS10\n2026-05-01,4.39\n2026-05-02,.\n2026-05-04,4.45\n"

        observations = parse_fred_csv(
            series,
            text,
            start=date(2026, 5, 2),
            end=date(2026, 5, 5),
        )

        self.assertEqual(
            observations,
            {
                date(2026, 5, 2): [{"observation_date": "2026-05-02", "DGS10": "."}],
                date(2026, 5, 4): [{"observation_date": "2026-05-04", "DGS10": "4.45"}],
            },
        )

    def test_parse_fred_csv_rejects_missing_date_header_instead_of_empty_history(self) -> None:
        series = _source("fred_csv", "DGS10")
        with self.assertRaisesRegex(IndicatorsProviderError, "missing column observation_date"):
            parse_fred_csv(
                series,
                "date,DGS10\n2026-05-01,4.39\n",
                start=date(2026, 5, 1),
                end=date(2026, 5, 2),
            )
