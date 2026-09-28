from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import patch

from tests.engine.macro.indicator_fixtures import (
    _FakeResponse,
    _multpl_monthly_table,
    _series,
    _StaticSession,
)
from tests.engine.macro.provider_fixtures import SnapshotFixtures

from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    fetch_observations,
    parse_multpl_current,
    parse_multpl_history,
    parse_trades_spec,
    parse_yahoo_chart,
)
from baibai_engine.macro.indicators.providers.cftc import parse_cftc_json
from baibai_engine.macro.indicators.providers.jquants_indices import parse_index_bars
from baibai_engine.macro.indicators.providers.nikkei_indexes import parse_nikkei_valuation


class IndicatorsProviderParserTests(SnapshotFixtures):
    def test_parse_cftc_json_computes_noncomm_net(self) -> None:
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

        observations = parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 7, 14))
        self.assertEqual(observations[0].value, -122663.0)

    def test_parse_cftc_json_rejects_mismatched_contract_code(self) -> None:
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
            parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_cftc_json_rejects_implausible_position(self) -> None:
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

        with self.assertRaisesRegex(IndicatorsProviderError, "exceeds plausible"):
            parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_index_bars_extracts_close_and_filters_range(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [
            {
                "Date": datetime(2026, 6, 30, tzinfo=UTC),
                "O": 3900.0,
                "H": 3950.0,
                "L": 3890.0,
                "C": 3940.5,
            },
            {
                "Date": datetime(2026, 7, 1, tzinfo=UTC),
                "O": 3945.0,
                "H": 4000.0,
                "L": 3940.0,
                "C": 3990.2,
            },
            {
                "Date": datetime(2026, 8, 1, tzinfo=UTC),
                "O": 4010.0,
                "H": 4020.0,
                "L": 4000.0,
                "C": 4015.0,
            },
        ]

        observations = parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2026, 7, 1), 3990.2)]
        )

    def test_parse_index_bars_rejects_implausible_close(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [{"Date": datetime(2026, 7, 1, tzinfo=UTC), "C": 99999.0}]

        with self.assertRaisesRegex(IndicatorsProviderError, "outside plausible"):
            parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_index_bars_rejects_missing_close_column(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [{"Date": datetime(2026, 7, 1, tzinfo=UTC), "O": 3945.0}]

        with self.assertRaisesRegex(IndicatorsProviderError, "missing a close column"):
            parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

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

    def test_parse_nikkei_valuation_rejects_implausible_value(self) -> None:
        series = _series("nikkei_indexes", "per", unit="ratio")
        html = (
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>99.90</td><!--daily_changing--><td>120.0</td></tr>"
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "outside plausible"):
            parse_nikkei_valuation(series, html, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_yahoo_chart_filters_range_and_skips_null_close(self) -> None:
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

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 5, 5)])
        self.assertEqual(observations[0].value, 4050.5)

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

    def test_parse_multpl_current_extracts_value(self) -> None:
        html = (
            '<meta name="description" content="Current Shiller PE Ratio is 40.70, '
            'a change of -0.30 from previous market close." />'
        )
        self.assertEqual(parse_multpl_current(html, "shiller-pe"), 40.70)

    def test_parse_multpl_current_reads_percent_value(self) -> None:
        text = "Current S&P 500 Earnings Yield is 3.18%, a change of +2.36 bps."
        self.assertEqual(parse_multpl_current(text, "s-p-500-earnings-yield"), 3.18)

    def test_parse_multpl_current_rejects_unparseable(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "cannot parse current value"):
            parse_multpl_current("<html>no current sentence here</html>", "shiller-pe")

    def test_parse_multpl_history_extracts_monthly_rows_and_current_level(self) -> None:
        series = _series("multpl", "s-p-500-earnings-yield", unit="percent")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Jul 22, 2026</td><td>3.18%</td></tr>
          <tr><td>Jul 1, 2026</td><td>† 3.20%</td></tr>
          <tr><td>Jun 1, 2026</td><td>3.15%</td></tr>
        </table>
        """

        observations = parse_multpl_history(
            series,
            html,
            start=date(2026, 7, 1),
            end=date(2026, 7, 22),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 7, 22), 3.18), (date(2026, 7, 1), 3.20)],
        )

    def test_parse_multpl_history_rejects_missing_table(self) -> None:
        series = _series("multpl", "shiller-pe")

        with self.assertRaisesRegex(IndicatorsProviderError, "historical table missing"):
            parse_multpl_history(
                series,
                "<html><table></table></html>",
                start=date(1871, 1, 1),
                end=date(2026, 7, 22),
            )

    def test_multpl_long_range_uses_history_table(self) -> None:
        series = _series("multpl", "shiller-pe")
        response = _FakeResponse(
            b'<table id="datatable"><tr><td>Jul 1, 2026</td><td>37.50</td></tr></table>'
        )

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 7, 20),
        ):
            observations = fetch_observations(
                series,
                start=date(2024, 1, 1),
                end=date(2026, 7, 20),
                session=_StaticSession(response),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 7, 1), 37.5)],
        )

    def test_multpl_all_history_rejects_missing_floor(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = '<table id="datatable"><tr><td>Jul 1, 2026</td><td>40.0</td></tr></table>'

        with self.assertRaisesRegex(IndicatorsProviderError, "must start at 1871-02-01"):
            parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 7, 20),
            )

    def test_parse_multpl_history_accepts_month_start_without_current_month_row(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 3, 2026</td><td>37.80</td></tr>
          <tr><td>Jul 1, 2026</td><td>37.50</td></tr>
          <tr><td>Jun 1, 2026</td><td>37.00</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 8, 3),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 7, 20),
                end=date(2026, 8, 3),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 3), 37.80)],
        )

    def test_parse_multpl_history_accepts_prior_month_current_row_after_rollover(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 31, 2026</td><td>42.04</td></tr>
          <tr><td>Jul 1, 2026</td><td>40.73</td></tr>
          <tr><td>Jun 1, 2026</td><td>40.50</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 1),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 8, 18),
                end=date(2026, 9, 1),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 31), 42.04)],
        )

    def test_parse_multpl_history_accepts_delayed_prior_month_asof(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 31, 2026</td><td>42.04</td></tr>
          <tr><td>Jul 1, 2026</td><td>40.73</td></tr>
          <tr><td>Jun 1, 2026</td><td>40.50</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 1),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 8, 17),
                end=date(2026, 8, 31),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 31), 42.04)],
        )

    def test_parse_multpl_history_accepts_unpublished_intervening_month(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Sep 3, 2026</td><td>42.38</td></tr>
          <tr><td>Jul 1, 2026</td><td>40.73</td></tr>
          <tr><td>Jun 1, 2026</td><td>40.50</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 4),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 8, 21),
                end=date(2026, 9, 4),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 9, 3), 42.38)],
        )

    def test_multpl_all_history_accepts_month_start_without_current_month_row(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 8, 3),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 8, 3),
            )

        self.assertEqual(observations[0].observed_at, date(2026, 7, 1))
        self.assertEqual(observations[-1].observed_at, date(1871, 2, 1))

    def test_multpl_all_history_accepts_unpublished_intervening_month(self) -> None:
        series = _series("multpl", "shiller-pe")
        monthly = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))
        html = monthly.replace(
            '<table id="datatable">',
            '<table id="datatable"><tr><td>Sep 3, 2026</td><td>42.38</td></tr>',
        )

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 4),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 9, 4),
            )

        self.assertEqual(observations[0].observed_at, date(2026, 9, 3))
        self.assertEqual(observations[1].observed_at, date(2026, 7, 1))

    def test_multpl_all_history_treats_first_month_start_as_current(self) -> None:
        series = _series("multpl", "shiller-pe")
        monthly = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))
        html = monthly.replace(
            '<table id="datatable">',
            '<table id="datatable"><tr><td>Sep 1, 2026</td><td>42.20</td></tr>',
        )

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 1),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 9, 1),
            )

        self.assertEqual(observations[0].observed_at, date(2026, 9, 1))
        self.assertEqual(observations[1].observed_at, date(2026, 7, 1))

    def test_multpl_all_history_rejects_gap_before_current_month(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = _multpl_monthly_table(
            floor=date(1871, 2, 1), latest=date(2026, 7, 1), omit=date(2020, 5, 1)
        )

        with (
            patch(
                "baibai_engine.macro.indicators.providers.multpl._today_jst",
                return_value=date(2026, 8, 3),
            ),
            self.assertRaisesRegex(IndicatorsProviderError, "missing monthly value.*2020-05-01"),
        ):
            parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 8, 3),
            )

    def test_multpl_uses_japan_operation_date(self) -> None:
        series = _series("multpl", "shiller-pe")
        response = _FakeResponse(b"Current Shiller PE Ratio is 40.70")

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 7, 20),
        ):
            observations = fetch_observations(
                series,
                start=date(2026, 7, 20),
                end=date(2026, 7, 20),
                session=_StaticSession(response),
            )

        self.assertEqual([item.observed_at for item in observations], [date(2026, 7, 20)])

    def test_parse_trades_spec_filters_range_and_uses_foreign_balance(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2026-05-08",
                "EnDate": "2026-04-25",
                "FrgnBuy": 900,
                "FrgnSell": 800,
                "FrgnBal": 100,
            },
            {
                "PubDate": "2026-05-08",
                "EnDate": "2026-05-02",
                "FrgnBuy": 1500,
                "FrgnSell": 1000,
                "FrgnBal": 500,
            },
            {
                "PubDate": "2026-05-15",
                "EnDate": "2026-05-09",
                "FrgnBuy": 1200,
                "FrgnSell": 2000,
                "FrgnBal": -800,
            },
            {
                "PubDate": "2026-05-22",
                "EnDate": "2026-05-16",
                "FrgnBuy": 300,
                "FrgnSell": 100,
                "FrgnBal": 200,
            },
        ]

        observations = parse_trades_spec(
            series, rows, start=date(2026, 5, 1), end=date(2026, 5, 15)
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 2))
        self.assertEqual(observations[0].value, 500.0)
        self.assertEqual(observations[0].vintage_at, datetime(2026, 5, 8, tzinfo=UTC))
        self.assertEqual(observations[1].observed_at, date(2026, 5, 9))
        self.assertEqual(observations[1].value, -800.0)
        self.assertEqual(observations[0].unit, "jpy-thousand")

    def test_parse_trades_spec_falls_back_to_purchases_minus_sales(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {"PubDate": "2026-05-08", "EnDate": "2026-05-08", "FrgnBuy": 1500, "FrgnSell": 1000}
        ]

        observations = parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 500.0)

    def test_parse_trades_spec_keeps_each_period_for_duplicate_publication_date(
        self,
    ) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2024-09-10",
                "StDate": "2024-08-19",
                "EnDate": "2024-08-23",
                "FrgnBal": -408854431,
            },
            {
                "PubDate": "2024-09-10",
                "StDate": "2024-08-26",
                "EnDate": "2024-08-30",
                "FrgnBal": -237009201,
            },
        ]

        observations = parse_trades_spec(
            series,
            rows,
            start=date(2024, 8, 1),
            end=date(2024, 9, 30),
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].observed_at, date(2024, 8, 23))
        self.assertEqual(observations[0].period_start, date(2024, 8, 19))
        self.assertEqual(observations[0].period_end, date(2024, 8, 23))
        self.assertEqual(observations[0].value, -408854431.0)
        self.assertEqual(observations[1].observed_at, date(2024, 8, 30))
        self.assertEqual(observations[1].period_start, date(2024, 8, 26))
        self.assertEqual(observations[1].period_end, date(2024, 8, 30))
        self.assertEqual(observations[1].value, -237009201.0)
        self.assertEqual(observations[0].vintage_at, datetime(2024, 9, 10, tzinfo=UTC))
        self.assertEqual(observations[1].vintage_at, datetime(2024, 9, 10, tzinfo=UTC))

    def test_parse_trades_spec_rejects_missing_foreign_columns(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [{"PubDate": "2026-05-08", "EnDate": "2026-05-08", "Section": "TSEPrime"}]

        with self.assertRaisesRegex(IndicatorsProviderError, "missing"):
            parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_parse_trades_spec_rejects_a_row_without_an_aggregation_period_end(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [{"PubDate": "2026-05-08", "FrgnBal": 500}]

        with self.assertRaisesRegex(IndicatorsProviderError, "aggregation period end"):
            parse_trades_spec(series, rows, start=date(2026, 5, 1), end=date(2026, 5, 15))

    def test_parse_trades_spec_dates_the_week_by_its_end_not_its_publication(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2026-07-02",
                "StDate": "2026-06-22",
                "EnDate": "2026-06-26",
                "FrgnBal": -1208567543,
            }
        ]

        observations = parse_trades_spec(
            series, rows, start=date(2026, 6, 1), end=date(2026, 7, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 6, 26))
        self.assertEqual(observations[0].period_start, date(2026, 6, 22))
        self.assertEqual(observations[0].vintage_at, datetime(2026, 7, 2, tzinfo=UTC))
