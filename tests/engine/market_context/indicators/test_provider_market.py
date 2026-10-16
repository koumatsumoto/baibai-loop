from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest import TestCase

from tests.engine.market_context.indicators.indicator_fixtures import (
    _multpl_monthly_table,
    _series,
)

from baibai_engine.market_context.indicators.providers import (
    IndicatorsProviderError,
    parse_multpl_table,
    parse_yahoo_chart,
)
from baibai_engine.market_context.indicators.providers.cftc import parse_cftc_records
from baibai_engine.market_context.indicators.providers.nikkei_indexes import parse_nikkei_valuation


class IndicatorsProviderParserTests(TestCase):
    def test_parse_cftc_records_retains_both_native_position_legs(self) -> None:
        series = _series("cftc", "097741", unit="contracts")
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
        series = _series("cftc", "097741", unit="contracts")
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
        series = _series("cftc", "097741", unit="contracts")
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

    def test_parse_nikkei_valuation_takes_weighted_average_and_filters_range(self) -> None:
        series = _series("nikkei_indexes", "per", unit="ratio")
        html = (
            "<table><tbody>"
            "<tr><!--daily_changing--><td>2026.06.30</td>"
            "<!--daily_changing--><td>18.40</td><!--daily_changing--><td>25.20</td></tr>"
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>17.82</td><!--daily_changing--><td>25.11</td></tr>"
            "</tbody></table>"
        )

        observations = parse_nikkei_valuation(
            series, html, start=date(2026, 7, 1), end=date(2026, 7, 31)
        )

        # The weighted-average column (17.82), not the index-based one (25.11).
        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2026, 7, 24), 17.82)]
        )

    def test_parse_nikkei_valuation_leaves_range_policy_to_storage(self) -> None:
        series = _series("nikkei_indexes", "per", unit="ratio")
        html = (
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>99.90</td><!--daily_changing--><td>120.0</td></tr>"
        )

        observations = parse_nikkei_valuation(
            series, html, start=date(2026, 7, 1), end=date(2026, 7, 31)
        )
        self.assertEqual(observations[0].value, 99.9)

    def test_parse_yahoo_chart_filters_range_without_discarding_null_close(self) -> None:
        series = _series("yahoo", "GC=F", unit="usd-per-oz")

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
        series = _series("yahoo", "GC=F", unit="usd-per-oz")
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
        series = _series("yahoo", "BADSYM", unit="index")
        text = json.dumps(
            {"chart": {"result": None, "error": {"code": "Not Found", "description": "no data"}}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "Yahoo chart error"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_yahoo_chart_rejects_missing_quote(self) -> None:
        series = _series("yahoo", "GC=F", unit="usd-per-oz")
        text = json.dumps(
            {"chart": {"error": None, "result": [{"timestamp": [1], "indicators": {}}]}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "missing quote"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))

    def test_multpl_retains_cells_and_source_explanation(self) -> None:
        series = _series("multpl", "shiller-pe", unit="ratio")
        html = """<table id="datatable">
          <caption>† Estimate</caption>
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Oct 15, 2026</td><td>41.01</td></tr>
          <tr><td>Oct 1, 2026</td><td><abbr title="Estimate">†</abbr> 40.81%</td></tr>
          <tr><td>Sep 1, 2026</td><td>null</td></tr>
        </table>
        <div id="description">Data from <a href="http://www.econ.yale.edu/~shiller/data.htm">Yale</a>.</div>"""
        rows = parse_multpl_table(series, html, start=date(2026, 9, 1), end=date(2026, 10, 15))
        self.assertEqual(list(rows), [date(2026, 10, 15), date(2026, 10, 1), date(2026, 9, 1)])
        self.assertEqual(rows[date(2026, 10, 1)][0]["value_text"], "† 40.81%")
        self.assertEqual(
            rows[date(2026, 10, 1)][0]["value_annotations"], [{"text": "†", "title": "Estimate"}]
        )
        self.assertEqual(rows[date(2026, 10, 1)][0]["caption_text"], "† Estimate")
        self.assertEqual(rows[date(2026, 9, 1)][0]["value_text"], "null")
        self.assertEqual(rows[date(2026, 10, 1)][0]["description_text"], "Data from Yale.")
        self.assertEqual(
            rows[date(2026, 10, 1)][0]["description_links"],
            [{"text": "Yale", "href": "http://www.econ.yale.edu/~shiller/data.htm"}],
        )

    def test_multpl_does_not_invent_explanation(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = '<table id="datatable"><tr><td>Oct 15, 2026</td><td>41.01</td></tr></table>'
        rows = parse_multpl_table(series, html, start=date(2026, 10, 15), end=date(2026, 10, 15))
        self.assertNotIn("description_text", rows[date(2026, 10, 15)][0])
        self.assertNotIn("description_links", rows[date(2026, 10, 15)][0])
        self.assertNotIn("caption_text", rows[date(2026, 10, 15)][0])
        self.assertNotIn("value_annotations", rows[date(2026, 10, 15)][0])

    def test_multpl_rejects_missing_or_malformed_table(self) -> None:
        series = _series("multpl", "shiller-pe")
        for html, reason in (
            ("<html>no table</html>", "table missing or empty"),
            ('<table id="datatable"><tr><td>Oct 15, 2026</td></tr></table>', "exactly two"),
            (
                '<table id="datatable"><tr><td>bad date</td><td>41.01</td></tr></table>',
                "invalid table date",
            ),
        ):
            with self.subTest(html=html), self.assertRaisesRegex(IndicatorsProviderError, reason):
                parse_multpl_table(series, html, start=date(2026, 10, 1), end=date(2026, 10, 15))

    def test_multpl_completeness_checks_rows_not_values(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))
        html = html.replace(
            '<table id="datatable">',
            '<table id="datatable"><tr><td>Aug 3, 2026</td><td>null</td></tr>',
        )
        html = html.replace("<td>20.0</td>", "<td>null</td>", 1)
        rows = parse_multpl_table(series, html, start=date(1871, 1, 1), end=date(2026, 8, 3))
        self.assertEqual(rows[date(2026, 8, 3)][0]["value_text"], "null")
        self.assertIn(date(1871, 2, 1), rows)
        missing = html.replace("<tr><td>May 1, 2020</td><td>20.0</td></tr>", "")
        with self.assertRaisesRegex(IndicatorsProviderError, "missing monthly row.*2020-05-01"):
            parse_multpl_table(series, missing, start=date(1871, 1, 1), end=date(2026, 8, 3))

    def test_multpl_retains_same_date_current_and_monthly_rows(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = (
            '<table id="datatable">'
            "<tr><td>Oct 1, 2026</td><td>41.01</td></tr>"
            "<tr><td>Oct 1, 2026</td><td>40.81</td></tr></table>"
        )
        rows = parse_multpl_table(series, html, start=date(2026, 10, 1), end=date(2026, 10, 1))
        self.assertEqual([row["value_text"] for row in rows[date(2026, 10, 1)]], ["41.01", "40.81"])
        self.assertEqual([row["table_row"] for row in rows[date(2026, 10, 1)]], [0, 1])
