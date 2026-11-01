"""Economic reading semantics, distinct from native source receipt."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _boj_workbook_bytes, _series, _source

from baibai_engine.data_collection.providers.boj.workbooks import parse_boj_xlsx
from baibai_engine.data_collection.transport.context import IndicatorsProviderError
from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.native import _one_value
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    reading_observation,
)


def _read_boj(series, content, *, start, end):
    records = parse_boj_xlsx(_source("boj"), content, start=start, end=end)
    return [
        reading_observation(series, observed_at=day, value=value, vintage_at=None)
        for day, rows in records.items()
        if (value := _one_value(series, rows)) is not None
    ]


class ReadingTests(TestCase):
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
                self.assertRaisesRegex((IndicatorsProviderError, ValueError), reason),
            ):
                _read_boj(series, content, start=start, end=end)

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

        observations = _read_boj(series, content, start=date(2026, 2, 1), end=date(2026, 3, 31))

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

        observations = _read_boj(series, content, start=date(2026, 1, 1), end=date(2026, 12, 31))

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

        with self.assertRaisesRegex(ValueError, "metadata column 2 mismatch"):
            _read_boj(
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
            _read_boj(series, buffer.getvalue(), start=date(2026, 1, 1), end=date(2026, 12, 31))
