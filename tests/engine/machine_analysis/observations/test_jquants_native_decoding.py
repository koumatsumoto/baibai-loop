"""現行J-Quants native正規化の回帰契約。"""

from __future__ import annotations

import json
import unittest
from datetime import date

import pandas as pd
import pytest

from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
    project_financial_documents,
)
from baibai_engine.machine_analysis.observations.prices.jquants_normalization import (
    normalize_daily_bars,
)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            {"C": 0.5, "Va": 0.0},
            ("1301", "2026-04-24", None, None, None, 0.5, None, 0.0, None, None, None, None, None),
        ),
        (
            {"C": 5070.0, "Va": 526318500.0},
            (
                "1301",
                "2026-04-24",
                None,
                None,
                None,
                5070.0,
                None,
                526318500.0,
                None,
                None,
                None,
                None,
                None,
            ),
        ),
        (
            {"C": 200.0, "AdjC": 100.0, "AdjFactor": 0.5, "Va": 526318500.0},
            (
                "1301",
                "2026-04-24",
                None,
                None,
                None,
                200.0,
                None,
                526318500.0,
                0.5,
                None,
                None,
                None,
                None,
            ),
        ),
    ],
)
def test_native_daily_prices_preserve_raw_close_and_zero_turnover(record, expected):
    normalized = normalize_daily_bars([{"Code": "13010", "Date": "2026-04-24T00:00:00", **record}])
    assert normalized.rows == [expected]
    assert normalized.rejected_count == normalized.excluded_count == 0


def test_native_daily_prices_exclude_non_common_suffix():
    normalized = normalize_daily_bars(
        [{"Code": "25935", "Date": "2026-04-24T00:00:00", "C": 100.0}]
    )
    assert normalized.rows == []
    assert normalized.excluded_count == 1
    assert normalized.rejected_count == 0


@pytest.mark.parametrize("bad", [10**400, -(10**400)])
def test_overflowing_numbers_keep_the_current_forecast_period(bad):
    summary = normalize_financial_summary(
        {
            "DiscNo": "synthetic-overflow",
            "Code": "97150",
            "DiscDate": "2026-05-13",
            "CurFYSt": "2026-04-01",
            "CurFYEn": "2027-03-31",
            "NxtFYSt": "2027-04-01",
            "NxtFYEn": "2028-03-31",
            "FEPS": bad,
            "FNP": bad,
            "FOdP": bad,
            "FDivAnn": bad,
            "NxFEPS": 120.0,
            "NxFNp": 2_000_000_000.0,
            "NxFOdP": 1_800_000_000.0,
            "NxFDivAnn": 12.0,
            "Sales": bad,
            "BPS": 1200.0,
        }
    )
    assert summary is not None
    assert summary.document_payload is not None
    assert json.loads(summary.document_payload)["Sales"] == bad
    projected = project_financial_documents([summary], date(2026, 5, 13))[0]
    assert projected.forecast_period_start == date(2026, 4, 1)
    assert projected.forecast_period_end == date(2027, 3, 31)
    assert projected.forecast_eps is None
    assert projected.forecast_profit is None
    assert projected.forecast_ordinary_profit is None
    assert projected.dps_forecast_annual is None
    assert projected.sales is None
    assert projected.bps == 1200.0


class JQuantsNativeDecodingTests(unittest.TestCase):
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
