"""Sparse bankruptcy history remains stale and insufficient in market Reading."""

import json
from datetime import date
from unittest import TestCase

from baibai_engine.machine_analysis.observations.economic_indicators.tsr_bankruptcies import (
    parse_tsr_bankruptcies_json,
)


class ReadingTests(TestCase):
    def test_tsr_partial_history_is_visible_as_stale_and_insufficient_in_reading(self) -> None:
        from tests.helpers.indicator_store import store_reader

        from baibai_engine.machine_analysis.generation.runners.reading_rules import (
            load_reading_rules,
        )
        from baibai_engine.machine_analysis.models.market_context.reading.compute import (
            compute_reading,
        )
        from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
            load_definitions,
        )

        series = load_definitions().by_id()["jp.bankruptcies"]
        result = parse_tsr_bankruptcies_json(
            series,
            json.dumps([{"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"}]),
            start=date(2003, 1, 1),
            end=date(2027, 1, 1),
        )
        snapshot = compute_reading(
            series=[series],
            reader=store_reader(result),
            rules=load_reading_rules(),
            rules_revision="test",
            asof=date(2027, 1, 1),
        )
        reading = snapshot.series[0]
        self.assertEqual(reading.latest_value, 1021.0)
        self.assertEqual(reading.observed_at, date(2026, 6, 1))
        self.assertTrue(reading.stale)
        self.assertTrue(reading.insufficient_history)
        self.assertIsNone(reading.percentile)
        self.assertIsNone(reading.z_score)
