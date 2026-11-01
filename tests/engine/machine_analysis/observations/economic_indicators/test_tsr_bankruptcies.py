"""Economic reading semantics, distinct from native source receipt."""

from __future__ import annotations

import json
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _series

from baibai_engine.machine_analysis.observations.economic_indicators.tsr_bankruptcies import (
    parse_tsr_bankruptcies_json,
)


class ReadingTests(TestCase):
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

    def test_parse_tsr_bankruptcies_json_rejects_unreadable_or_conflicting_months(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        for rows, reason in (
            ([{"period_division": "月次", "title": "月次の全国企業倒産状況"}], "cannot parse"),
            (
                [
                    {"period_division": "月次", "title": "2026年6月の全国企業倒産100件"},
                    {"period_division": "月次", "title": "2026年6月の全国企業倒産200件"},
                ],
                "conflicting values",
            ),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, reason),
            ):
                parse_tsr_bankruptcies_json(
                    series, json.dumps(rows), start=date(2003, 1, 1), end=date(2026, 12, 31)
                )

    def test_tsr_keeps_available_months_without_inventing_missing_history(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = [
            {"period_division": "月次", "title": "2026年4月の全国企業倒産990件"},
            {"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"},
        ]
        result = parse_tsr_bankruptcies_json(
            series, json.dumps(rows), start=date(2003, 1, 1), end=date(2027, 1, 1)
        )
        self.assertEqual(
            [(item.observed_at, item.value) for item in result],
            [(date(2026, 4, 1), 990.0), (date(2026, 6, 1), 1021.0)],
        )

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
