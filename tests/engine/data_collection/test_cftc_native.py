"""Native source contracts for cftc_native."""

from __future__ import annotations

import json
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _source

from baibai_engine.data_collection.providers.cftc import parse_cftc_records
from baibai_engine.data_collection.transport.context import IndicatorsProviderError


class NativeParserTests(TestCase):
    def test_parse_cftc_records_retains_both_native_position_legs(self) -> None:
        series = _source(
            "cftc",
            "097741",
        )
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "097741",
                    "noncomm_positions_long_all": "115965",
                    "noncomm_positions_short_all": "238628",
                }
            ]
        )

        observations = parse_cftc_records(
            series, text, start=date(2026, 7, 1), end=date(2026, 7, 31)
        )

        self.assertEqual(len(observations), 1)
        row = observations[date(2026, 7, 14)][0]
        self.assertEqual(row["noncomm_positions_long_all"], "115965")
        self.assertEqual(row["noncomm_positions_short_all"], "238628")

    def test_parse_cftc_records_rejects_mismatched_contract_code(self) -> None:
        series = _source(
            "cftc",
            "097741",
        )
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "999999",
                    "noncomm_positions_long_all": "1",
                    "noncomm_positions_short_all": "2",
                }
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "does not match requested"):
            parse_cftc_records(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_cftc_records_preserves_values_for_l2_range_policy(self) -> None:
        series = _source(
            "cftc",
            "097741",
        )
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "097741",
                    "noncomm_positions_long_all": "9000000",
                    "noncomm_positions_short_all": "1",
                }
            ]
        )

        observations = parse_cftc_records(
            series, text, start=date(2026, 7, 1), end=date(2026, 7, 31)
        )
        self.assertEqual(
            observations[date(2026, 7, 14)][0]["noncomm_positions_long_all"], "9000000"
        )
