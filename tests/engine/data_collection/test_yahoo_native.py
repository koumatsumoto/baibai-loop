"""Yahoo null close revisions retain OHLCV and do not revive earlier values."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime
from unittest import TestCase

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _source

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.providers import yahoo
from baibai_engine.data_collection.providers.yahoo import parse_yahoo_chart
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


def test_null_close_revision_preserves_source_fields_and_removes_prior_value(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["gold"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    stamps = [
        int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
        for day in (first, second)
    ]
    metadata = {"symbol": "GC=F", "currency": "USD", "exchangeTimezoneName": "America/New_York"}

    def reply(closes):
        return json.dumps(
            {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "meta": metadata,
                            "timestamp": stamps,
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [3990, 4040],
                                        "high": [4010, 4060],
                                        "low": [3980, 4030],
                                        "volume": [100, 200],
                                        "close": closes,
                                    }
                                ],
                                "adjclose": [{"adjclose": [4000, 4050]}],
                            },
                        }
                    ],
                }
            }
        )

    replies = iter([reply([4000, 4050]), reply([None, 4050])])
    urls = []

    def fetch(session, url, **kwargs):
        urls.append(url)
        return next(replies)

    monkeypatch.setattr(yahoo, "fetch_text", fetch)
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 4000.0),
        (second, 4050.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range(series.series_id, start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 4050.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader(series.series_id, first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {
                "timestamp": stamps[0],
                "metadata": {"meta": metadata},
                "indicators": {
                    "quote": [
                        {"open": 3990, "high": 4010, "low": 3980, "volume": 100, "close": None}
                    ],
                    "adjclose": [{"adjclose": 4000}],
                },
            },
        )
    assert urls == [series.source_url, series.source_url]


def test_wrong_symbol_cannot_be_read_as_the_requested_series(tmp_path, monkeypatch):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["gold"]
    day = date(2026, 7, 24)
    payload = json.dumps(
        {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": "OTHER", "currency": "USD"},
                        "timestamp": [int(datetime(2026, 7, 24, tzinfo=UTC).timestamp())],
                        "indicators": {"quote": [{"close": [4000]}]},
                    }
                ],
            }
        }
    )
    monkeypatch.setattr(yahoo, "fetch_text", lambda *args, **kwargs: payload)
    outcome = acquisition.refresh_sources([series.source_id], db_path=path, start=day, end=day)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with pytest.raises(ValueError, match="symbol does not match"):
        IndicatorsService(path).get_range(series.series_id, start=day, end=day)


class NativeParserTests(TestCase):
    def test_parse_yahoo_chart_filters_range_without_discarding_null_close(self) -> None:
        series = _source(
            "yahoo",
            "GC=F",
        )

        def ts(year: int, month: int, day: int) -> int:
            return int(datetime(year, month, day, 14, 30, tzinfo=UTC).timestamp())

        text = json.dumps(
            {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "timestamp": [ts(2026, 5, 1), ts(2026, 5, 4), ts(2026, 5, 5)],
                            "indicators": {"quote": [{"close": [4000.0, None, 4050.5]}]},
                        }
                    ],
                }
            }
        )

        observations = parse_yahoo_chart(series, text, start=date(2026, 5, 4), end=date(2026, 5, 5))

        self.assertEqual(list(observations), [date(2026, 5, 4), date(2026, 5, 5)])
        self.assertEqual(
            observations[date(2026, 5, 4)][0]["indicators"]["quote"], [{"close": None}]
        )
        self.assertEqual(
            observations[date(2026, 5, 5)][0]["indicators"]["quote"], [{"close": 4050.5}]
        )

    def test_parse_yahoo_chart_rejects_unpaired_dates_or_values(self) -> None:
        series = _source(
            "yahoo",
            "GC=F",
        )
        stamp = int(datetime(2026, 5, 1, tzinfo=UTC).timestamp())
        for timestamps, closes in (
            ([stamp, stamp + 86400], [4000.0]),
            ([stamp], [4000.0, 4010.0]),
        ):
            with self.subTest(timestamps=timestamps, closes=closes):
                text = json.dumps(
                    {
                        "chart": {
                            "error": None,
                            "result": [
                                {
                                    "timestamp": timestamps,
                                    "indicators": {"quote": [{"close": closes}]},
                                }
                            ],
                        }
                    }
                )
                with self.assertRaisesRegex(IndicatorsProviderError, "lengths differ"):
                    parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))

    def test_parse_yahoo_chart_rejects_error_payload(self) -> None:
        series = _source(
            "yahoo",
            "BADSYM",
        )
        text = json.dumps(
            {"chart": {"result": None, "error": {"code": "Not Found", "description": "no data"}}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "Yahoo chart error"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_yahoo_chart_rejects_missing_quote(self) -> None:
        series = _source(
            "yahoo",
            "GC=F",
        )
        text = json.dumps(
            {"chart": {"error": None, "result": [{"timestamp": [1], "indicators": {}}]}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "missing quote"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))
