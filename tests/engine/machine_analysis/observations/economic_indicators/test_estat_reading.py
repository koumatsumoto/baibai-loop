"""Economic reading semantics, distinct from native source receipt."""

from __future__ import annotations

from datetime import date
from typing import cast
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _DASHBOARD_SELECTORS,
    _dashboard_payload,
    _dashboard_row,
    _estat_payload,
    _parse_dashboard,
    _read_estat,
    _series,
    _source,
)

from baibai_engine.data_collection.catalog import require_source
from baibai_engine.data_collection.providers.estat.api import parse_estat_json
from baibai_engine.data_collection.providers.estat.dashboard import parse_dashboard_json
from baibai_engine.data_collection.transport.context import IndicatorsProviderError
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)


class ReadingTests(TestCase):
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

        observations = _read_estat(
            series,
            _source("estat", "0003427113"),
            text,
            start=date(2026, 2, 1),
            end=date(2026, 3, 31),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 2, 1))
        self.assertEqual(observations[0].value, 100.8)

    def test_parse_estat_json_handles_single_value_object(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload({"@time": "2026000505", "$": "102.0"})

        observations = _read_estat(
            series,
            _source("estat", "0003427113"),
            text,
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
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
                    series.reading_key,
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
                observations = _read_estat(
                    series,
                    require_source(series.source_id),
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
                        _read_estat(
                            series,
                            require_source(series.source_id),
                            _estat_payload([*cells, {**cells[-1], attribute: wrong}]),
                            start=date(2024, 1, 1),
                            end=date(2026, 8, 31),
                        )

    def test_parse_estat_json_reads_a_month_whose_closing_month_is_left_open(self) -> None:
        """e-Stat published 2024-01 of the watcher survey as "2024000100".

        Reading the closing field as the month drops that observation, and the
        series then has a hole no error ever reports.
        """

        series = _series("estat", "0003348423", unit="pt")
        text = _estat_payload({"@time": "2024000100", "$": "50.2"})

        observations = _read_estat(
            series,
            _source("estat", "0003348423"),
            text,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
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

        observations = _read_estat(
            series,
            _source("estat", "0003427113"),
            text,
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
        )

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2025, 1, 1), 110.0)]
        )

    def test_estat_native_cells_retain_null_markers_units_and_attributes(self) -> None:
        series = _series("estat", "0003427113?cdCat01=0001", unit="index")
        rows = [
            {
                "@time": "2026000505",
                "@cat01": "0001",
                "@unit": "指数",
                "$": "100.8",
                "@note": "revised",
            },
            {"@time": "2026000606", "@cat01": "0001", "@unit": "指数", "$": "***"},
        ]
        source = _source("estat", "0003427113?cdCat01=0001")
        native = parse_estat_json(
            source, _estat_payload(rows), start=date(2026, 5, 1), end=date(2026, 6, 30)
        )
        self.assertEqual(native, {date(2026, 5, 1): [rows[0]], date(2026, 6, 1): [rows[1]]})
        self.assertEqual(
            [
                item.value
                for item in _read_estat(
                    series,
                    source,
                    _estat_payload(rows),
                    start=date(2026, 5, 1),
                    end=date(2026, 6, 30),
                )
            ],
            [100.8],
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

    def test_dashboard_reading_excludes_retained_preliminary_prints(self) -> None:
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"
        rows = [_dashboard_row("20260400", "2.6"), _dashboard_row("20260500", "2.4"), preliminary]
        observations = _parse_dashboard(rows)
        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 4, 1), 2.6), (date(2026, 5, 1), 2.4)],
        )
        native = parse_dashboard_json(
            _dashboard_payload(rows),
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            selectors=_DASHBOARD_SELECTORS,
        )
        self.assertEqual(native[date(2026, 6, 1)], [preliminary["VALUE"]])

    def test_parse_dashboard_json_keeps_a_preliminary_month_out_of_the_unit_check(self) -> None:
        # A retained preliminary row is not part of the final-value Reading, so its
        # unit does not contaminate the final values selected by L2.
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"
        preliminary["VALUE"]["@unit"] = "指数"

        observations = _parse_dashboard([_dashboard_row("20260500", "2.4"), preliminary])

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

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

        with self.assertRaisesRegex(ValueError, "value is not a string"):
            _parse_dashboard([row])

    def test_parse_dashboard_json_rejects_two_units_in_one_series(self) -> None:
        rows = [_dashboard_row("20260400", "2.5"), _dashboard_row("20260500", "2400")]
        rows[1]["VALUE"]["@unit"] = "120"

        with self.assertRaisesRegex(ValueError, "mixed units"):
            _parse_dashboard(rows)

    def test_dashboard_preserves_null_and_unknown_attributes_before_reading(self) -> None:
        row = _dashboard_row("20260500", "-")
        row["VALUE"]["publisher_note"] = "unavailable"
        native = parse_dashboard_json(
            _dashboard_payload([row]),
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            selectors=_DASHBOARD_SELECTORS,
        )
        self.assertEqual(native, {date(2026, 5, 1): [row["VALUE"]]})
        self.assertEqual(_parse_dashboard([row]), [])

    def test_dashboard_checks_units_for_the_reading_window_not_unused_history(self) -> None:
        old = _dashboard_row("20260400", "2500")
        old["VALUE"]["@unit"] = "120"
        observations = _parse_dashboard(
            [old, _dashboard_row("20260500", "2.4")], start=date(2026, 5, 1)
        )
        self.assertEqual([item.value for item in observations], [2.4])

    def test_dashboard_reading_preserves_zero_and_does_not_hide_null_duplicates(self) -> None:
        self.assertEqual(
            [item.value for item in _parse_dashboard([_dashboard_row("20260500", "0")])], [0.0]
        )
        self.assertEqual(
            _parse_dashboard([_dashboard_row("20260500", "2.4"), _dashboard_row("20260500", "-")]),
            [],
        )
