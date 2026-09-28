from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from typing import cast
from unittest.mock import patch

from tests.engine.macro.indicator_fixtures import (
    _DASHBOARD_INDICATOR,
    _DASHBOARD_SELECTORS,
    _DASHBOARD_SOURCE_URL,
    _boj_workbook_bytes,
    _dashboard_payload,
    _dashboard_row,
    _dashboard_series,
    _estat_payload,
    _parse_dashboard,
    _RecordingSession,
    _series,
)
from tests.engine.macro.provider_fixtures import SnapshotFixtures

from baibai_engine.macro.indicators.definitions import (
    SeriesDefinition,
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    parse_boj_timeseries_json,
    parse_boj_xlsx,
    parse_estat_json,
    parse_mof_jgb_csv,
    parse_tsr_bankruptcies_json,
)
from baibai_engine.macro.indicators.providers.estat_dashboard import (
    EStatDashboardProvider,
    parse_dashboard_json,
)


class IndicatorsProviderParserTests(SnapshotFixtures):
    def test_parse_boj_xlsx_refuses_a_workbook_it_cannot_read_the_named_column_from(
        self,
    ) -> None:
        # One damaged BOJ workbook per row: the series it was requested for, the
        # bytes, the window, and the refusal. Every row is a way the published
        # workbook has changed shape without the values leaving their band, which is
        # what makes reading the wrong column silent.
        refusals: tuple[tuple[str, SeriesDefinition, bytes, date, date, str], ...] = (
            (
                "shifted_value_column_even_when_value_is_in_band",
                _series(
                    "boj",
                    "3|Monetary Base|8|Unit: 100 million yen",
                    unit="jpy-100m",
                    plausible_min=2000.0,
                    plausible_max=100000000.0,
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 9999.0, 350000.0)], header_column=4),
                date(2026, 1, 1),
                date(2026, 1, 31),
                "header mismatch",
            ),
            (
                "empty_selected_column_for_in_range_dates",
                _series("boj", "3|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"),
                _boj_workbook_bytes(
                    [
                        (None, date(2026, 1, 31), None, 350000.0),
                        (None, date(2026, 2, 28), None, 360000.0),
                    ]
                ),
                date(2026, 1, 1),
                date(2026, 2, 28),
                "no numeric values",
            ),
            (
                "scale_metadata_change_with_in_band_value",
                _series(
                    "boj",
                    "3|Monetary Base|8|Unit: 100 million yen",
                    unit="jpy-100m",
                    plausible_min=2000.0,
                    plausible_max=100000000.0,
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 350000.0)], metadata="Unit: yen"),
                date(2026, 1, 1),
                date(2026, 1, 31),
                "metadata column 8 mismatch",
            ),
            (
                "non_xlsx_bytes",
                _series("boj", "3|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"),
                b"not a zip",
                date(2026, 1, 1),
                date(2026, 12, 31),
                "not a .xlsx",
            ),
            (
                "non_numeric_column_index",
                _series(
                    "boj", "BS01'MABJMTA|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 1.0, 2.0)]),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "start with a 1-based column index",
            ),
        )

        for name, series, content, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_boj_xlsx(series, content, start=start, end=end)

    def test_parse_boj_xlsx_extracts_value_column_and_filters_range(self) -> None:
        content = _boj_workbook_bytes(
            [
                (None, date(2026, 1, 31), 350000.0, 9999.0),
                (None, date(2026, 2, 28), 360000.0, 9999.0),
                (None, date(2026, 3, 31), 370000.0, 9999.0),
            ]
        )
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        observations = parse_boj_xlsx(
            series, content, start=date(2026, 2, 1), end=date(2026, 3, 31)
        )

        self.assertEqual(
            [(obs.observed_at, obs.value) for obs in observations],
            [(date(2026, 2, 1), 360000.0), (date(2026, 3, 1), 370000.0)],
        )

    def test_parse_boj_xlsx_skips_header_and_valueless_rows(self) -> None:
        content = _boj_workbook_bytes(
            [
                ("マネタリーベース", None, None, None),
                (None, date(2026, 1, 31), None, None),
                (None, date(2026, 2, 28), 360000.0, None),
            ]
        )
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        observations = parse_boj_xlsx(
            series, content, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 360000.0)

    def test_parse_boj_xlsx_does_not_accept_expected_metadata_from_an_adjacent_series(
        self,
    ) -> None:
        content = _boj_workbook_bytes(
            [(date(2026, 1, 1), 119.0, 100.0)],
            header_column=2,
            header_label="Real exports",
            metadata_column=2,
            metadata="s.a., CY 2025=100, 2025 prices",
            other_metadata=(3, "s.a., CY 2020=100, 2020 prices"),
        )
        series = _series(
            "boj",
            "2|Real exports|2|s.a., CY 2020=100, 2020 prices",
            unit="index",
            plausible_min=1.0,
            plausible_max=2000.0,
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "metadata column 2 mismatch"):
            parse_boj_xlsx(
                series,
                content,
                start=date(2026, 1, 1),
                end=date(2026, 1, 31),
            )

    def test_parse_boj_xlsx_wraps_corrupt_zip_as_provider_error(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("not-a-workbook.txt", "garbage")
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "could not be read"):
            parse_boj_xlsx(
                series, buffer.getvalue(), start=date(2026, 1, 1), end=date(2026, 12, 31)
            )

    def test_parse_mof_jgb_csv_extracts_10y_and_filters_range(self) -> None:
        series = _series("mof_jgb", "10年")
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

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 7, 8))
        self.assertEqual(observations[0].value, 2.856)

    def test_parse_mof_jgb_csv_rejects_missing_column(self) -> None:
        series = _series("mof_jgb", "10年")
        text = "基準日,1年\nR8.7.8,1.18\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column 10年"):
            parse_mof_jgb_csv(series, text, start=date(2026, 7, 8), end=date(2026, 7, 8))

    def test_parse_estat_json_filters_range_and_skips_nonnumeric(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload(
            [
                {"@time": "2026000101", "$": "100.1"},
                {"@time": "2026000202", "$": "100.8"},
                {"@time": "2026000303", "$": "-"},
                {"@time": "2026000404", "$": "101.3"},
            ]
        )

        observations = parse_estat_json(series, text, start=date(2026, 2, 1), end=date(2026, 3, 31))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 2, 1))
        self.assertEqual(observations[0].value, 100.8)

    def test_parse_estat_json_handles_single_value_object(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload({"@time": "2026000505", "$": "102.0"})

        observations = parse_estat_json(
            series, text, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 1))
        self.assertEqual(observations[0].value, 102.0)

    def test_japanese_cpi_uses_2025_base_and_official_change_rates(self) -> None:
        by_id = {item.series_id: item for item in load_definitions().series}
        # Nationwide cells verified against e-Stat table 0004052037 on 2026-09-22.
        # The 2025 YoY retains the published old-base rate. Read the official
        # change-rate cells directly instead of recalculating from connected indices.
        for suffix, category, tab, unit, values in (
            ("headline", "0001", "1", "index", (97.5, 100.3, 102.2)),
            ("core", "0161", "1", "index", (97.7, 100.3, 102.0)),
            ("core_yoy", "0161", "3", "percent", (2.8, 2.7, 1.7)),
            ("services", "0220", "1", "index", (99.1, 100.6, 101.7)),
        ):
            with self.subTest(series=suffix):
                series = by_id[f"jp.cpi.{suffix}"]
                self.assertEqual(series.provider, "estat")
                self.assertEqual(series.frequency, "monthly")
                self.assertEqual(series.unit, unit)
                self.assertEqual(
                    series.provider_series_id,
                    f"0004052037?cdCat01={category}&cdArea=00000&cdTab={tab}",
                )
                cells = [
                    {
                        "@tab": tab,
                        "@cat01": category,
                        "@area": "00000",
                        "@time": f"{year}000808",
                        "$": str(value),
                    }
                    for year, value in zip((2024, 2025, 2026), values, strict=True)
                ]
                observations = parse_estat_json(
                    series,
                    _estat_payload(cells),
                    start=date(2024, 1, 1),
                    end=date(2026, 8, 31),
                )
                self.assertEqual([item.value for item in observations], list(values))
                self.assertEqual(
                    [item.observed_at for item in observations],
                    [date(year, 8, 1) for year in (2024, 2025, 2026)],
                )
                for attribute, wrong in (
                    ("@tab", "2"),
                    ("@cat01", "0162"),
                    ("@area", "13100"),
                ):
                    with (
                        self.subTest(attribute=attribute),
                        self.assertRaisesRegex(
                            IndicatorsProviderError, "outside the requested narrowing"
                        ),
                    ):
                        parse_estat_json(
                            series,
                            _estat_payload([*cells, {**cells[-1], attribute: wrong}]),
                            start=date(2024, 1, 1),
                            end=date(2026, 8, 31),
                        )

    def test_parse_estat_json_refuses_every_answer_it_cannot_read_one_series_from(
        self,
    ) -> None:
        # One malformed e-Stat answer per row: the series it was requested for, the
        # response body, the window, and the refusal it must produce. Every row is a shape
        # the API has actually returned; the parser has to keep refusing all of them.
        refusals: tuple[tuple[str, SeriesDefinition, str, date, date, str], ...] = (
            (
                "missing_structure",
                _series("estat", "0003427113", unit="index"),
                "{}",
                date(2026, 1, 1),
                date(2026, 12, 31),
                "GET_STATS_DATA",
            ),
            (
                # An e-Stat table carries dozens of series behind one statsDataId.
                #
                # A narrowing code the table does not define is answered by leaving that
                # dimension open, and the store's upsert would then keep whichever cell of
                # the period came last. The answer is checked cell by cell instead.
                "a_cell_outside_the_requested_narrowing",
                _series("estat", "0003355222?cdCat01=160&cdCat02=100&cdTab=100", unit="index"),
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
                _series("estat", "0003355222?lvCat01=3", unit="index"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "no row attribute",
            ),
            (
                "a_cell_spanning_more_than_one_month",
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "2025000103", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "more than one month",
            ),
            (
                "a_time_code_it_cannot_place",
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "202501", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "cannot place",
            ),
            (
                "a_rejected_request",
                _series("estat", "0003427113", unit="index"),
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
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}, next_key=100001),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "one page of a longer result",
            ),
        )

        for name, series, text, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_estat_json(series, text, start=start, end=end)

    def test_parse_estat_json_reads_a_month_whose_closing_month_is_left_open(self) -> None:
        """e-Stat published 2024-01 of the watcher survey as "2024000100".

        Reading the closing field as the month drops that observation, and the
        series then has a hole no error ever reports.
        """

        series = _series("estat", "0003348423", unit="pt")
        text = _estat_payload({"@time": "2024000100", "$": "50.2"})

        observations = parse_estat_json(
            series, text, start=date(2024, 1, 1), end=date(2024, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2024, 1, 1))
        self.assertEqual(observations[0].value, 50.2)

    def test_parse_estat_json_skips_the_year_totals_that_share_the_table(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload(
            [
                {"@time": "2025100000", "$": "112.3"},
                {"@time": "2025000000", "$": "112.5"},
                {"@time": "2025000101", "$": "110.0"},
            ]
        )

        observations = parse_estat_json(
            series, text, start=date(2025, 1, 1), end=date(2025, 12, 31)
        )

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2025, 1, 1), 110.0)]
        )

    def test_estat_dashboard_fetch_always_opens_the_request_at_the_history_floor(self) -> None:
        """ "No data" is the same answer as "unknown IndicatorCode" on this API.

        A request narrow enough to come back empty would make a mis-pinned series
        read as a quiet one, so every request asks from the floor, where a
        registered series always has observations. The stored window is still the
        requested one.
        """

        session = _RecordingSession(
            _dashboard_payload(
                [
                    _dashboard_row("20260400", "2.5"),
                    _dashboard_row("20260500", "2.4"),
                ]
            )
        )

        observations = EStatDashboardProvider().fetch(
            _dashboard_series(),
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            session=session,
        )

        self.assertEqual(session.params, {"TimeFrom": "19480100", "TimeTo": "20260500"})
        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 1), 2.4)],
        )

    def test_parse_dashboard_json_skips_null_markers(self) -> None:
        observations = _parse_dashboard(
            [
                _dashboard_row("20260300", "2.6"),
                _dashboard_row("20260400", "-"),
                _dashboard_row("20260500", "2.4"),
            ]
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 3, 1), 2.6), (date(2026, 5, 1), 2.4)],
        )

    def test_parse_dashboard_json_rejects_a_row_the_selectors_excluded(self) -> None:
        """One IndicatorCode answers several cycles and adjustments at once.

        A row from another one means the filter did not apply, and mixing a
        month-on-month change into an index level would read as the series itself.
        """

        for attribute, value in (
            ("@cycle", "3"),
            ("@isSeasonal", "1"),
            ("@indicator", "0302030202010090010"),
            ("@regionCode", "13000"),
        ):
            with self.subTest(attribute=attribute):
                row = _dashboard_row("20260500", "2.4")
                row["VALUE"][attribute] = value

                with self.assertRaisesRegex(IndicatorsProviderError, attribute):
                    _parse_dashboard([row])

    def test_parse_dashboard_json_leaves_a_preliminary_month_unwritten(self) -> None:
        # The declared publication lag belongs to the final print, so a month that
        # has only a preliminary print is not due yet rather than missing.
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"

        with self.assertLogs(
            "baibai_engine.macro.indicators.providers.estat_dashboard", level="WARNING"
        ) as logs:
            observations = _parse_dashboard(
                [
                    _dashboard_row("20260400", "2.6"),
                    _dashboard_row("20260500", "2.4"),
                    preliminary,
                ]
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 4, 1), 2.6), (date(2026, 5, 1), 2.4)],
        )
        message = "\n".join(logs.output)
        self.assertIn("test.series", message)
        self.assertIn("2026-06-01", message)

    def test_parse_dashboard_json_keeps_a_preliminary_month_out_of_the_unit_check(self) -> None:
        # A preliminary row is not part of the series being written, so its unit
        # must not read as the publisher mixing two units into one series.
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"
        preliminary["VALUE"]["@unit"] = "指数"

        observations = _parse_dashboard([_dashboard_row("20260500", "2.4"), preliminary])

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_dashboard_json_rejects_a_page_shorter_than_its_declared_total(self) -> None:
        text = _dashboard_payload([_dashboard_row("20260500", "2.4")], total=2)

        with self.assertRaisesRegex(IndicatorsProviderError, "1 rows for a declared total of 2"):
            parse_dashboard_json(
                _dashboard_series(),
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
                _dashboard_series(),
                text,
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                selectors=_DASHBOARD_SELECTORS,
            )

    def test_parse_dashboard_json_rejects_an_unreadable_time_code(self) -> None:
        # A changed time axis must fail rather than pass as a shorter history.
        for time_code in ("2026Q100", "202605", "20261300"):
            with (
                self.subTest(time_code=time_code),
                self.assertRaisesRegex(IndicatorsProviderError, "time code is unreadable"),
            ):
                _parse_dashboard([_dashboard_row(time_code, "2.4")])

    def test_parse_dashboard_json_rejects_a_value_that_is_not_a_string(self) -> None:
        # Missing numbers have their own markers, so a changed value type is a
        # changed response shape and must not read as a row that has no value.
        row = _dashboard_row("20260500", "2.4")
        row["VALUE"]["$"] = cast(str, 2.4)

        with self.assertRaisesRegex(IndicatorsProviderError, "value is not a string"):
            _parse_dashboard([row])

    def test_parse_dashboard_json_rejects_two_units_in_one_series(self) -> None:
        rows = [_dashboard_row("20260400", "2.5"), _dashboard_row("20260500", "2400")]
        rows[1]["VALUE"]["@unit"] = "120"

        with self.assertRaisesRegex(IndicatorsProviderError, "mixed units"):
            _parse_dashboard(rows)

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
                EStatDashboardProvider().fetch(
                    _dashboard_series(source_url=source_url),
                    start=date(2026, 1, 1),
                    end=date(2026, 5, 31),
                    session=_RecordingSession(_dashboard_payload([])),
                )

    def test_estat_dashboard_refuses_a_series_that_is_not_monthly(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "monthly series only"):
            EStatDashboardProvider().fetch(
                _dashboard_series(frequency="quarterly"),
                start=date(2026, 1, 1),
                end=date(2026, 5, 31),
                session=_RecordingSession(_dashboard_payload([])),
            )

    def test_parse_tsr_bankruptcies_json_reads_current_and_legacy_entries(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        text = json.dumps(
            [
                {
                    "period_division": "月次",
                    "title": "2026年6月の全国企業倒産1,021件",
                    "free_word": [],
                },
                {
                    "period_division": "月次",
                    "title": "2026年（令和8年） 5月度 全国企業倒産状況",
                    "free_word": [
                        "title",
                        "<table><tr><th>倒産件数</th><td>993 件</td></tr></table>",
                    ],
                },
                {
                    "period_division": "年間",
                    "title": "2025年の全国企業倒産10,000件",
                },
            ]
        )

        observations = parse_tsr_bankruptcies_json(
            series,
            text,
            start=date(2026, 5, 1),
            end=date(2026, 6, 1),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 1), 993.0), (date(2026, 6, 1), 1021.0)],
        )

    def test_parse_tsr_bankruptcies_json_refuses_a_listing_it_cannot_read_a_history_from(
        self,
    ) -> None:
        # One damaged TSR listing per row. The provider reads a monthly count out of
        # press-release titles, so a title it cannot parse, a month it never saw, and a
        # history that does not reach the floor are all silent holes unless refused.
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        refusals: tuple[tuple[str, str, date, date, str], ...] = (
            (
                "unparseable_monthly_entry",
                json.dumps(
                    [
                        {
                            "period_division": "月次",
                            "title": "月次の全国企業倒産状況",
                            "free_word": [],
                        }
                    ]
                ),
                date(2003, 1, 1),
                date(2026, 6, 1),
                "cannot parse monthly entry",
            ),
            (
                "history_gap",
                json.dumps(
                    [
                        {"period_division": "月次", "title": "2026年4月の全国企業倒産990件"},
                        {"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"},
                    ]
                ),
                date(2026, 4, 1),
                date(2026, 6, 1),
                "missing monthly entries",
            ),
            (
                "missing_history_floor",
                json.dumps(
                    [{"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"}]
                ),
                date(2003, 1, 1),
                date(2026, 7, 20),
                "must start at 2003-01-01",
            ),
        )

        for name, text, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_tsr_bankruptcies_json(series, text, start=start, end=end)

    def test_parse_tsr_bankruptcies_json_rejects_missing_latest_release(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 5, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with (
            patch(
                "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
                return_value=date(2026, 7, 28),
            ),
            self.assertRaisesRegex(IndicatorsProviderError, "latest expected release"),
        ):
            parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 7, 28),
            )

    def test_parse_tsr_bankruptcies_json_ignores_future_end_for_latest_release(
        self,
    ) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 6, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with patch(
            "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
            return_value=date(2026, 7, 24),
        ):
            observations = parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 12, 31),
            )

        self.assertEqual(len(observations), 282)
        self.assertEqual(observations[-1].observed_at, date(2026, 6, 1))

    def test_parse_tsr_bankruptcies_json_allows_prepublication_tail(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 5, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with patch(
            "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
            return_value=date(2026, 7, 5),
        ):
            observations = parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 7, 5),
            )

        self.assertEqual(observations[-1].observed_at, date(2026, 5, 1))

    def test_parse_boj_timeseries_json_filters_range_and_nulls(self) -> None:
        series = _series("boj_timeseries", "FM01:STRDCLUCON", unit="percent")
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
        self.assertEqual(observations[0].observed_at, date(1998, 1, 5))
        self.assertEqual(observations[0].value, 0.49)

    def test_parse_boj_timeseries_json_reads_monthly_yyyymm_survey_dates(self) -> None:
        series = _series(
            "boj_timeseries", "PR01:PRCG20_2200000000", unit="index", frequency="monthly"
        )
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
            [(o.observed_at, o.value) for o in observations],
            [(date(2026, 5, 1), 134.9), (date(2026, 6, 1), 135.4)],
        )

    def test_parse_boj_timeseries_json_reads_quarterly_yyyy0q_survey_dates(self) -> None:
        series = _series(
            "boj_timeseries", "CO:TK99F1000601GCQ01000", unit="pt", frequency="quarterly"
        )
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
            [(o.observed_at, o.value) for o in observations],
            [(date(2025, 12, 1), 15.0), (date(2026, 3, 1), 17.0), (date(2026, 6, 1), 22.0)],
        )

    def test_parse_boj_timeseries_json_rejects_wrong_length_for_frequency(self) -> None:
        series = _series(
            "boj_timeseries", "PR01:PRCG20_2200000000", unit="index", frequency="monthly"
        )
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
        series = _series("boj_timeseries", "FM01:STRDCLUCON", unit="percent")
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

    def test_split_stats_data_id_extracts_narrowing_params(self) -> None:
        from baibai_engine.macro.indicators.providers.estat import _split_stats_data_id

        stats_id, narrowing = _split_stats_data_id("0003427113?cdCat01=0001&cdArea=00000&cdTab=1")

        self.assertEqual(stats_id, "0003427113")
        self.assertEqual(narrowing, {"cdCat01": "0001", "cdArea": "00000", "cdTab": "1"})

    def test_split_stats_data_id_without_query_returns_empty_params(self) -> None:
        from baibai_engine.macro.indicators.providers.estat import _split_stats_data_id

        self.assertEqual(_split_stats_data_id("0003427113"), ("0003427113", {}))
