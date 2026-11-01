"""Native source contracts for estat_native."""

from __future__ import annotations

import json
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _DASHBOARD_INDICATOR,
    _DASHBOARD_SELECTORS,
    _DASHBOARD_SOURCE_URL,
    _dashboard_payload,
    _dashboard_row,
    _estat_payload,
    _RecordingSession,
    _source,
)

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.providers.estat.api import parse_estat_json
from baibai_engine.data_collection.providers.estat.dashboard import (
    EStatDashboardSource,
    parse_dashboard_json,
)
from baibai_engine.data_collection.transport.context import IndicatorsProviderError


class NativeParserTests(TestCase):
    def test_parse_estat_json_refuses_every_answer_it_cannot_read_one_series_from(
        self,
    ) -> None:
        # One malformed e-Stat answer per row: the series it was requested for, the
        # response body, the window, and the refusal it must produce. Every row is a shape
        # the API has actually returned; the parser has to keep refusing all of them.
        refusals: tuple[tuple[str, SourceDefinition, str, date, date, str], ...] = (
            (
                "missing_structure",
                _source("estat", "0003427113"),
                "{}",
                date(2026, 1, 1),
                date(2026, 12, 31),
                "GET_STATS_DATA",
            ),
            (
                # An e-Stat table carries dozens of series behind one statsDataId.
                #
                # A narrowing code the table does not define is answered by leaving that
                # dimension open. Check each cell before unrelated series enter the
                # requested source; retaining rows is not proof that the filter applied.
                "a_cell_outside_the_requested_narrowing",
                _source("estat", "0003355222?cdCat01=160&cdCat02=100&cdTab=100"),
                _estat_payload(
                    [
                        {
                            "@tab": "100",
                            "@cat01": "160",
                            "@cat02": "100",
                            "@time": "2026000505",
                            "$": "961973.8",
                        },
                        {
                            "@tab": "100",
                            "@cat01": "110",
                            "@cat02": "100",
                            "@time": "2026000505",
                            "$": "2874019.3",
                        },
                    ]
                ),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "outside the requested narrowing",
            ),
            (
                "a_narrowing_key_it_cannot_check",
                _source("estat", "0003355222?lvCat01=3"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "no row attribute",
            ),
            (
                "a_cell_spanning_more_than_one_month",
                _source("estat", "0003427113"),
                _estat_payload({"@time": "2025000103", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "more than one month",
            ),
            (
                "a_time_code_it_cannot_place",
                _source("estat", "0003427113"),
                _estat_payload({"@time": "202501", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "cannot place",
            ),
            (
                "a_rejected_request",
                _source("estat", "0003427113"),
                json.dumps(
                    {
                        "GET_STATS_DATA": {
                            "RESULT": {"STATUS": 100, "ERROR_MSG": "統計表が存在しません。"}
                        }
                    }
                ),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "status=100",
            ),
            (
                "one_page_of_a_longer_result",
                _source("estat", "0003427113"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}, next_key=100001),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "one page of a longer result",
            ),
        )

        for name, source, text, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_estat_json(source, text, start=start, end=end)

    def test_estat_dashboard_fetch_always_opens_the_request_at_the_history_floor(self) -> None:
        """ "No data" is the same answer as "unknown IndicatorCode" on this API.

        A request narrow enough to come back empty would make a mis-pinned series
        read as a quiet one, so every request asks from the floor, where a
        configured series is expected to have history. This does not guarantee API
        availability. The stored window is still the requested one.
        """

        session = _RecordingSession(
            _dashboard_payload(
                [
                    _dashboard_row("20260400", "2.5"),
                    _dashboard_row("20260500", "2.4"),
                ]
            )
        )

        observations = EStatDashboardSource().fetch_records(
            _source(
                "estat_dashboard",
                _DASHBOARD_INDICATOR,
                source_url=_DASHBOARD_SOURCE_URL,
                frequency="monthly",
            ),
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            session=session,
        )

        self.assertEqual(session.params, {"TimeFrom": "19480100", "TimeTo": "20260500"})
        self.assertEqual(
            observations,
            {date(2026, 5, 1): [_dashboard_row("20260500", "2.4")["VALUE"]]},
        )

    def test_parse_dashboard_json_rejects_a_page_shorter_than_its_declared_total(self) -> None:
        text = _dashboard_payload([_dashboard_row("20260500", "2.4")], total=2)

        with self.assertRaisesRegex(IndicatorsProviderError, "1 rows for a declared total of 2"):
            parse_dashboard_json(
                text,
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                selectors=_DASHBOARD_SELECTORS,
            )

    def test_parse_dashboard_json_rejects_an_api_error_status(self) -> None:
        text = json.dumps(
            {"GET_STATS": {"RESULT": {"status": "1", "errorMsg": "該当データはありませんでした。"}}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "returned status 1"):
            parse_dashboard_json(
                text,
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                selectors=_DASHBOARD_SELECTORS,
            )

    def test_estat_dashboard_requires_the_source_url_to_pin_one_series(self) -> None:
        """The source URL is the provenance stored on every observation.

        Keeping the identity anywhere else would let the recorded provenance and
        the fetched series drift apart, and a corrected selector would then layer a
        second statistic onto the same series instead of replacing it.
        """

        base = "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"
        for source_url, message in (
            (base, "missing selector"),
            (f"{base}?IndicatorCode={_DASHBOARD_INDICATOR}&Cycle=1", "missing selector"),
            (f"{_DASHBOARD_SOURCE_URL}&Unit=001", "unsupported"),
            (f"{_DASHBOARD_SOURCE_URL}&Cycle=1", "repeated"),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
                    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode="
                ),
                "empty",
            ),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
                    "&Cycle=3&IsSeasonalAdjustment=2&RegionCode=00000"
                ),
                "monthly cycle only",
            ),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode=0302030202010090010"
                    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode=00000"
                ),
                "for series",
            ),
        ):
            with (
                self.subTest(source_url=source_url),
                self.assertRaisesRegex(IndicatorsProviderError, message),
            ):
                EStatDashboardSource().fetch_records(
                    _source(
                        "estat_dashboard",
                        _DASHBOARD_INDICATOR,
                        source_url=source_url,
                        frequency="monthly",
                    ),
                    start=date(2026, 1, 1),
                    end=date(2026, 5, 31),
                    session=_RecordingSession(_dashboard_payload([])),
                )

    def test_estat_dashboard_refuses_a_series_that_is_not_monthly(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "monthly series only"):
            EStatDashboardSource().fetch_records(
                _source(
                    "estat_dashboard",
                    _DASHBOARD_INDICATOR,
                    source_url=_DASHBOARD_SOURCE_URL,
                    frequency="quarterly",
                ),
                start=date(2026, 1, 1),
                end=date(2026, 5, 31),
                session=_RecordingSession(_dashboard_payload([])),
            )

    def test_split_stats_data_id_extracts_narrowing_params(self) -> None:
        from baibai_engine.data_collection.providers.estat.api import _split_stats_data_id

        stats_id, narrowing = _split_stats_data_id("0003427113?cdCat01=0001&cdArea=00000&cdTab=1")

        self.assertEqual(stats_id, "0003427113")
        self.assertEqual(narrowing, {"cdCat01": "0001", "cdArea": "00000", "cdTab": "1"})

    def test_split_stats_data_id_without_query_returns_empty_params(self) -> None:
        from baibai_engine.data_collection.providers.estat.api import _split_stats_data_id

        self.assertEqual(_split_stats_data_id("0003427113"), ("0003427113", {}))
