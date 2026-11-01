"""BOJ null corrections are source records, not permission to reuse old numbers."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _source

import baibai_engine.data_collection.acquisition.refresh as acquisition
import baibai_engine.data_collection.providers.boj.timeseries as boj_timeseries
from baibai_engine.data_collection.providers.boj.timeseries import parse_boj_timeseries_json
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


def test_null_revision_does_not_restore_prior_value_and_retains_metadata(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.policy_rate"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)

    def reply(values):
        return json.dumps(
            {
                "STATUS": 200,
                "MESSAGE": "synthetic",
                "RESULTSET": [
                    {
                        "SERIES_CODE": "STRDCLUCON",
                        "UNIT": "Percent",
                        "NOTE": "kept",
                        "VALUES": {
                            "SURVEY_DATES": [20260724, 20260727],
                            "VALUES": values,
                            "OTHER": "retained",
                        },
                    }
                ],
            }
        )

    replies = iter([reply([0.5, 0.6]), reply([None, 0.6])])
    monkeypatch.setattr(boj_timeseries, "fetch_text", lambda *args, **kwargs: next(replies))
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 0.5),
        (second, 0.6),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 0.6)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {
                "response_metadata": {"STATUS": 200, "MESSAGE": "synthetic"},
                "series_metadata": {"SERIES_CODE": "STRDCLUCON", "UNIT": "Percent", "NOTE": "kept"},
                "values_metadata": {"OTHER": "retained"},
                "survey_date": 20260724,
                "value": None,
            },
        )


class NativeParserTests(TestCase):
    def test_parse_boj_timeseries_json_filters_range_without_changing_cells(self) -> None:
        series = _source("boj_timeseries", "FM01:STRDCLUCON")
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "STRDCLUCON",
                        "VALUES": {
                            "SURVEY_DATES": [19980104, 19980105, 19980106],
                            "VALUES": [None, 0.49, 0.42],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series,
            text,
            start=date(1998, 1, 5),
            end=date(1998, 1, 5),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(list(observations), [date(1998, 1, 5)])
        self.assertEqual(observations[date(1998, 1, 5)][0]["value"], 0.49)

    def test_parse_boj_timeseries_json_reads_monthly_yyyymm_survey_dates(self) -> None:
        series = _source("boj_timeseries", "PR01:PRCG20_2200000000", frequency="monthly")
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "PRCG20_2200000000",
                        "VALUES": {
                            "SURVEY_DATES": [202605, 202606, 202607],
                            "VALUES": [134.9, 135.4, None],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series, text, start=date(2026, 5, 1), end=date(2026, 7, 31)
        )

        self.assertEqual(
            [(day, rows[0]["value"]) for day, rows in observations.items()],
            [(date(2026, 5, 1), 134.9), (date(2026, 6, 1), 135.4), (date(2026, 7, 1), None)],
        )

    def test_parse_boj_timeseries_json_reads_quarterly_yyyy0q_survey_dates(self) -> None:
        series = _source("boj_timeseries", "CO:TK99F1000601GCQ01000", frequency="quarterly")
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "TK99F1000601GCQ01000",
                        "VALUES": {
                            "SURVEY_DATES": [202504, 202601, 202602],
                            "VALUES": [15, 17, 22],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series, text, start=date(2025, 1, 1), end=date(2026, 12, 31)
        )

        # YYYY0Q maps Q1..Q4 to the last month of the quarter (Mar/Jun/Sep/Dec).
        self.assertEqual(
            [(day, rows[0]["value"]) for day, rows in observations.items()],
            [(date(2025, 12, 1), 15), (date(2026, 3, 1), 17), (date(2026, 6, 1), 22)],
        )

    def test_parse_boj_timeseries_json_rejects_wrong_length_for_frequency(self) -> None:
        series = _source("boj_timeseries", "PR01:PRCG20_2200000000", frequency="monthly")
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "PRCG20_2200000000",
                        "VALUES": {"SURVEY_DATES": [20260601], "VALUES": [135.4]},
                    }
                ],
            }
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "invalid survey date"):
            parse_boj_timeseries_json(series, text, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_timeseries_json_rejects_malformed_payload(self) -> None:
        series = _source("boj_timeseries", "FM01:STRDCLUCON")
        for payload, message in (
            ({"STATUS": 500, "MESSAGE": "failed"}, "status"),
            (
                {
                    "STATUS": 200,
                    "RESULTSET": [
                        {
                            "SERIES_CODE": "STRDCLUCON",
                            "VALUES": {"SURVEY_DATES": [19980105], "VALUES": []},
                        }
                    ],
                },
                "lengths differ",
            ),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(IndicatorsProviderError, message),
            ):
                parse_boj_timeseries_json(
                    series,
                    json.dumps(payload),
                    start=date(1998, 1, 1),
                    end=date(1998, 1, 31),
                )
