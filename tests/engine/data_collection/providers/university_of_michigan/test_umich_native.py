"""UMich missing revisions retain the source row and remove the old L2 value."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.university_of_michigan.sentiment as umich_sca
from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.providers.university_of_michigan.sentiment import (
    parse_umich_table,
)
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


class NativeParserTests(TestCase):
    def test_parse_umich_table_reads_the_column_for_each_published_month(self) -> None:
        series = require_source("umich-sca-ics")
        # The early history is quarterly, so consecutive rows can skip months.
        text = "Month,YYYY,ICS_ALL\nNovember,1952,86.2\nFebruary,1953,90.7\nJune,2026,49.5\n"

        observations = parse_umich_table(series, text, start=date(1952, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            observations,
            {
                date(1952, 11, 1): [{"Month": "November", "YYYY": "1952", "ICS_ALL": "86.2"}],
                date(1953, 2, 1): [{"Month": "February", "YYYY": "1953", "ICS_ALL": "90.7"}],
                date(2026, 6, 1): [{"Month": "June", "YYYY": "2026", "ICS_ALL": "49.5"}],
            },
        )

    def test_parse_umich_table_filters_to_the_requested_window(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nApril,2026,49.8\nMay,2026,44.8\nJune,2026,49.5\n"

        observations = parse_umich_table(
            series, text, start=date(2026, 5, 1), end=date(2026, 5, 31)
        )

        self.assertEqual(list(observations), [date(2026, 5, 1)])

    def test_parse_umich_table_retains_a_month_published_without_a_value(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nMay,2026,44.8\nJune,2026,\n"

        observations = parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            observations[date(2026, 6, 1)],
            [{"Month": "June", "YYYY": "2026", "ICS_ALL": ""}],
        )

    def test_parse_umich_table_rejects_a_row_that_is_not_a_month_of_a_year(self) -> None:
        series = require_source("umich-sca-ics")
        text = "Month,YYYY,ICS_ALL\nQ2,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "not a month of a year"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))
