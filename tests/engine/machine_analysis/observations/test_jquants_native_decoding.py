"""現行J-Quants native正規化の回帰契約。"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQuantsProviderError,
    normalize_adjustment_factor_event,
    normalize_daily_bar,
    normalize_market_calendar,
)


class JQuantsNativeDecodingTests(unittest.TestCase):
    def test_normalize_market_calendar_business_day(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_reads_native_timestamp(self) -> None:
        calendar_day = normalize_market_calendar({"Date": "2026-04-24T00:00:00", "HolDiv": "1"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_market_calendar_treats_half_day_as_business(self) -> None:
        # HolDiv="2" は半日営業 (大納会など取引あり) で、business_day 扱いすべき。
        calendar_day = normalize_market_calendar({"Date": "2026-12-30", "HolDiv": "2"})
        self.assertTrue(calendar_day.is_business_day)

    def test_normalize_daily_bar_preserves_zero_turnover(self) -> None:
        # `or` チェーンで 0.0 を欠損誤判定しないことを固定化する。
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 0.5,
                "Va": 0.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.turnover_value, 0.0)

    def test_normalize_financial_summary_preserves_zero_fields(self) -> None:
        # EPS=0 / OP=0 / Sales=0 が None に潰れないこと (損益分岐点ちょうど、赤字転換など)。
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-88910-2026-04-01",
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "10.0",
                "EPS": "0.0",
                "BPS": "1200.0",
                "Sales": "0",
                "OP": "0",
                "ShOutFY": "1000000",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.cumulative_eps, 0.0)
        self.assertEqual(summary.sales, 0.0)
        self.assertEqual(summary.operating_profit, 0.0)

    def test_normalize_daily_bar_reads_native_fields(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 5070.0,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.ticker, "1301")
        self.assertEqual(bar.close, 5070.0)
        self.assertEqual(bar.turnover_value, 526318500.0)

    def test_normalize_daily_bar_skips_non_common_suffix(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "25935",
                "Date": "2026-04-24T00:00:00",
                "C": 100.0,
            }
        )
        self.assertIsNone(bar)

    def test_normalize_daily_bar_keeps_raw_close_despite_provider_adjustment(self) -> None:
        bar = normalize_daily_bar(
            {
                "Code": "13010",
                "Date": "2026-04-24T00:00:00",
                "C": 200.0,
                "AdjC": 100.0,
                "AdjFactor": 0.5,
                "Va": 526318500.0,
            }
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.close, 200.0)
        self.assertEqual(bar.adjustment_factor, 0.5)

    def test_adjusted_only_record_does_not_invent_raw_close_or_lose_split_event(self) -> None:
        record = {
            "Code": "67310",
            "Date": "2023-12-27",
            "C": None,
            "AdjC": 500.0,
            "AdjFactor": 100.0,
        }
        self.assertIsNone(normalize_daily_bar(record))
        event = normalize_adjustment_factor_event(record)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.adjustment_factor, 100.0)

    def test_reported_action_without_factor_is_not_silently_discarded(self) -> None:
        for kind in ("1", "2", "3", "unknown-future-kind"):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(
                    JQuantsProviderError, "corporate action has no usable adjustment factor"
                ),
            ):
                normalize_adjustment_factor_event(
                    {
                        "Code": "13010",
                        "Date": "2026-10-01",
                        "C": None,
                        "AdjFactor": None,
                        "ExRT": kind,
                    }
                )
        self.assertIsNone(
            normalize_adjustment_factor_event(
                {"Code": "13010", "Date": "2026-10-01", "C": None, "AdjFactor": None}
            )
        )

    def test_normalize_adjustment_event_does_not_require_close(self) -> None:
        event = normalize_adjustment_factor_event(
            {
                "Code": "67310",
                "Date": "2023-12-27T00:00:00",
                "C": None,
                "AdjFactor": 100.0,
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.ticker, "6731")
        self.assertEqual(event.adjustment_factor, 100.0)

    def test_normalize_financial_summary_preserves_native_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-88910-2026-04-01",
                "Code": "88910",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "377.75",
                "EPS": "300.50",
                "BPS": "1200.0",
                "ShOutFY": "1000000",
                "Sales": "31300000000",
                "OP": "1840000000",
                "OdP": "1720000000",
                "NP": "1070000000",
                "CurPerType": "1Q",
                "CurFYEn": "2027-03-31T00:00:00",
                "CurPerSt": "2026-04-01T00:00:00",
                "CurPerEn": "2026-06-30T00:00:00",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.ticker, "8891")
        self.assertIsNone(summary.forecast_eps)
        self.assertIn('"FEPS": "377.75"', summary.document_payload or "")
        self.assertEqual(summary.cumulative_eps, 300.50)
        self.assertEqual(summary.shares_outstanding, 1000000.0)
        self.assertEqual(summary.operating_profit, 1840000000.0)
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_projection_selects_next_fiscal_year_by_dates(self) -> None:
        fy = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": "",
                "NxFEPS": "360.26",
                "EPS": "349.18",
            }
        )
        assert fy is not None
        self.assertIsNone(fy.forecast_eps)
        fy = project_financial_documents([fy], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(fy.forecast_eps, 360.26)
        self.assertEqual(fy.cumulative_eps, 349.18)

    def test_non_finite_current_forecasts_do_not_cross_into_next_period(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": float("nan"),
                "NxFEPS": "120.0",
                "FNP": float("inf"),
                "NxFNp": "2000000000",
                "FOdP": float("nan"),
                "NxFOdP": "1800000000",
                "FDivAnn": float("-inf"),
                "NxFDivAnn": "12.0",
                "Sales": float("inf"),
                "CFO": float("-inf"),
                "TA": float("nan"),
                "ShOutFY": float("inf"),
                "TrShFY": float("-inf"),
                "EqAR": float("nan"),
            }
        )

        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)
        self.assertIsNone(summary.sales)
        self.assertIsNone(summary.cfo)
        self.assertIsNone(summary.total_assets)
        self.assertIsNone(summary.shares_outstanding)
        self.assertIsNone(summary.treasury_shares)
        self.assertIsNone(summary.equity_to_asset_ratio)

    def test_pandas_missing_current_forecasts_do_not_cross_periods(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": pd.NA,
                "NxFEPS": "120.0",
                "FNP": pd.NA,
                "NxFNp": "2000000000",
                "FOdP": pd.NA,
                "NxFOdP": "1800000000",
                "FDivAnn": pd.NA,
                "NxFDivAnn": "12.0",
            }
        )

        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertIsNone(summary.forecast_eps)
        self.assertIsNone(summary.forecast_profit)
        self.assertIsNone(summary.forecast_ordinary_profit)
        self.assertIsNone(summary.dps_forecast_annual)

    def test_projection_keeps_zero_current_forecasts(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
                "Code": "97150",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": 0,
                "NxFEPS": "120.0",
                "FNP": "1000000000",
                "NxFNp": "2000000000",
                "FOdP": "900000000",
                "NxFOdP": "1800000000",
                "FDivAnn": 0,
                "NxFDivAnn": "12.0",
            }
        )

        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(summary.forecast_eps, 0.0)
        self.assertEqual(summary.forecast_profit, 1_000_000_000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 900_000_000.0)
        self.assertEqual(summary.dps_forecast_annual, 0.0)

    def test_normalize_financial_summary_keeps_average_shares_separate(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-41670-2024-08-14",
                "Code": "41670",
                "DiscDate": "2024-08-14T00:00:00",
                "AvgSh": "7563857",
                "TrShFY": "352373",
            }
        )

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertIsNone(summary.shares_outstanding)
        self.assertEqual(summary.average_shares, 7_563_857.0)
        self.assertEqual(summary.treasury_shares, 352_373.0)

    def test_projection_selects_current_fiscal_year_by_dates(self) -> None:
        q3 = normalize_financial_summary(
            {
                "DiscNo": "synthetic-97150-2026-02-13",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
                "Code": "97150",
                "DiscDate": "2026-02-13T00:00:00",
                "FEPS": "300.0",
                "NxFEPS": "145.0",
            }
        )
        assert q3 is not None
        self.assertIsNone(q3.forecast_eps)
        q3 = project_financial_documents([q3], date.fromisoformat("2026-02-13"))[0]
        self.assertEqual(q3.forecast_eps, 300.0)

    def test_projection_keeps_current_forecast_profit_pair(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-48490-2026-05-13",
                "CurFYSt": "2026-04-01",
                "CurFYEn": "2027-03-31",
                "NxtFYSt": "2027-04-01",
                "NxtFYEn": "2028-03-31",
                "Code": "48490",
                "DiscDate": "2026-05-13T00:00:00",
                "FEPS": "138.72",
                "NxFEPS": "120.0",
                "FNP": "5464000000",
                "FOdP": "3406000000",
                "NxFNp": "900000000",
                "NxFOdP": "1200000000",
            }
        )
        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-05-13"))[0]
        self.assertEqual(summary.forecast_profit, 5464000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 3406000000.0)

    def test_projection_keeps_next_forecast_profit_pair(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-48490-2026-04-01",
                "CurFYSt": "2025-04-01",
                "CurFYEn": "2026-03-31",
                "NxtFYSt": "2026-04-01",
                "NxtFYEn": "2027-03-31",
                "Code": "48490",
                "DiscDate": "2026-04-01T00:00:00",
                "FEPS": "",
                "NxFEPS": "120.0",
                "FNP": "5464000000",
                "FOdP": "3406000000",
                "NxFNp": "2000000000",
                "NxFOdP": "2500000000",
            }
        )
        assert summary is not None
        self.assertIsNone(summary.forecast_eps)
        summary = project_financial_documents([summary], date.fromisoformat("2026-04-01"))[0]
        self.assertEqual(summary.forecast_eps, 120.0)
        self.assertEqual(summary.forecast_profit, 2000000000.0)
        self.assertEqual(summary.forecast_ordinary_profit, 2500000000.0)

    def test_normalize_financial_summary_preserves_fiscal_period_fields(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-88910-2026-04-01",
                "Code": "88910",
                "DiscDate": "2026-04-01",
                "CurPerType": "1Q",
                "CurFYEn": "2027-03-31",
                "CurPerSt": "2026-04-01",
                "CurPerEn": "2026-06-30",
                "EPS": "300.50",
            }
        )
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.fiscal_period, "1Q")
        self.assertEqual(summary.fiscal_year_end, date(2027, 3, 31))
        self.assertEqual(summary.period_start, date(2026, 4, 1))
        self.assertEqual(summary.period_end, date(2026, 6, 30))

    def test_normalize_financial_summary_skips_non_common_suffix(self) -> None:
        summary = normalize_financial_summary(
            {
                "DiscNo": "synthetic-25935-2026-04-24",
                "Code": "25935",
                "DiscDate": "2026-04-24T00:00:00",
                "EPS": "10.0",
            }
        )
        self.assertIsNone(summary)
