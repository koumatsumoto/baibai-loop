from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.metrics import build_metrics
from baibai_loop.screening.providers.edinet import EdinetMetricRecord
from baibai_loop.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from baibai_loop.screening.schema import SecurityMaster, TTMQuality


def _daily_bars(code: str, end: date, total_days: int) -> list[JQuantsDailyBar]:
    start = end - timedelta(days=total_days - 1)
    return [
        JQuantsDailyBar(
            ticker=code,
            traded_at=start + timedelta(days=index),
            close=100.0 + index,
            turnover_value=300_000_000.0,
        )
        for index in range(total_days)
    ]


def _summary(
    code: str,
    disclosed_at: date,
    *,
    eps_ttm: float = 18.0,
    sales: float = 1_000_000_000.0,
    operating_profit: float = 100_000_000.0,
    fiscal_period: str | None = "1Q",
    fiscal_year_end: date | None = date(2026, 3, 31),
    shares_outstanding: float = 400_000_000.0,
) -> JQuantsFinancialSummary:
    return JQuantsFinancialSummary(
        ticker=code,
        disclosed_at=disclosed_at,
        forecast_eps=20.0,
        eps_ttm=eps_ttm,
        bps=120.0,
        shares_outstanding=shares_outstanding,
        sales=sales,
        operating_profit=operating_profit,
        ordinary_profit=None,
        profit=None,
        fiscal_period=fiscal_period,
        fiscal_year_end=fiscal_year_end,
    )


def _security(code: str = "130A") -> SecurityMaster:
    return SecurityMaster(
        code=code,
        name="Alpha",
        market_segment="Prime",
        sector_33="情報・通信業",
        is_common_stock=True,
    )


class ScreeningMetricsTests(unittest.TestCase):
    def test_build_metrics_excludes_future_bars_from_history(self) -> None:
        """look-ahead bias regression guard: bars after asof must not influence derived metrics."""
        asof = date(2026, 4, 24)
        security = _security()
        base_bars = _daily_bars("130A", asof, 400)
        future_bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof + timedelta(days=index + 1),
                close=99999.0,
                turnover_value=300_000_000.0,
            )
            for index in range(20)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": base_bars + future_bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )
        self.assertIn("130A", result.financials)
        # latest close picked from bars <= asof, not from the polluted 99999.0 future bars.
        per_trailing = result.financials["130A"].per_trailing
        self.assertIsNotNone(per_trailing)
        assert per_trailing is not None
        self.assertAlmostEqual(per_trailing, (100.0 + 399) / 18.0, places=5)
        # short_history_flag must be derived from bars <= asof only. 400d history < 750d ⇒ True.
        self.assertTrue(result.derived["130A"].short_history_flag)

    def test_build_metrics_uses_bars_span_for_short_history_when_established(self) -> None:
        """An established listing (800 days of bars) must not be flagged as short_history."""
        asof = date(2026, 4, 24)
        security = _security()
        bars = _daily_bars("130A", asof, 800)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )
        self.assertFalse(result.derived["130A"].short_history_flag)

    def test_build_metrics_compares_yoy_with_prior_fiscal_period(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary("130A", date(2025, 4, 24), eps_ttm=10.0, sales=100.0, operating_profit=20.0, fiscal_period="1Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 7, 24), eps_ttm=40.0, sales=400.0, operating_profit=80.0, fiscal_period="2Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 10, 24), eps_ttm=50.0, sales=500.0, operating_profit=100.0, fiscal_period="3Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 1, 24), eps_ttm=60.0, sales=600.0, operating_profit=120.0, fiscal_period="FY", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 4, 24), eps_ttm=15.0, sales=125.0, operating_profit=25.0, fiscal_period="1Q", fiscal_year_end=date(2027, 3, 31)),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertIsNotNone(snapshot.eps_yoy)
        self.assertIsNotNone(snapshot.sales_yoy)
        self.assertIsNotNone(snapshot.operating_profit_yoy)
        assert snapshot.eps_yoy is not None
        assert snapshot.sales_yoy is not None
        assert snapshot.operating_profit_yoy is not None
        # Old QoQ logic would compare EPS with the previous disclosure (60.0)
        # and produce -0.75. Fiscal-period matching locks YoY at +0.50.
        self.assertAlmostEqual(snapshot.eps_yoy, 0.5)
        self.assertAlmostEqual(snapshot.sales_yoy, 0.25)
        self.assertAlmostEqual(snapshot.operating_profit_yoy, 0.25)

    def test_build_metrics_sets_yoy_to_none_when_prior_year_summary_is_missing(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={
                "130A": [
                    _summary("130A", date(2025, 7, 24), fiscal_period="2Q", fiscal_year_end=date(2026, 3, 31)),
                    _summary("130A", date(2025, 10, 24), fiscal_period="3Q", fiscal_year_end=date(2026, 3, 31)),
                    _summary("130A", date(2026, 1, 24), fiscal_period="FY", fiscal_year_end=date(2026, 3, 31)),
                    _summary("130A", date(2026, 4, 24), fiscal_period="1Q", fiscal_year_end=date(2027, 3, 31)),
                ]
            },
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertIsNone(snapshot.eps_yoy)
        self.assertIsNone(snapshot.sales_yoy)
        self.assertIsNone(snapshot.operating_profit_yoy)

    def test_build_metrics_ignores_qoq_seasonality_for_yoy_deterioration(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary("130A", date(2025, 4, 24), eps_ttm=10.0, sales=100.0, operating_profit=20.0, fiscal_period="1Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 7, 24), eps_ttm=20.0, sales=200.0, operating_profit=40.0, fiscal_period="2Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 10, 24), eps_ttm=30.0, sales=300.0, operating_profit=60.0, fiscal_period="3Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 1, 24), eps_ttm=80.0, sales=800.0, operating_profit=160.0, fiscal_period="FY", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 4, 24), eps_ttm=10.0, sales=100.0, operating_profit=20.0, fiscal_period="1Q", fiscal_year_end=date(2027, 3, 31)),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.eps_yoy, 0.0)
        self.assertEqual(snapshot.sales_yoy, 0.0)
        self.assertEqual(snapshot.operating_profit_yoy, 0.0)

    def test_build_metrics_uses_fiscal_period_match_when_extra_disclosure_shifts_index(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary("130A", date(2025, 4, 24), eps_ttm=10.0, fiscal_period="1Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 6, 1), eps_ttm=999.0, fiscal_period="OTHER", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 7, 24), eps_ttm=20.0, fiscal_period="2Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 10, 24), eps_ttm=30.0, fiscal_period="3Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 1, 24), eps_ttm=40.0, fiscal_period="FY", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 4, 24), eps_ttm=15.0, fiscal_period="1Q", fiscal_year_end=date(2027, 3, 31)),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        self.assertEqual(result.financials["130A"].eps_yoy, 0.5)

    def test_build_metrics_requires_same_prior_fiscal_year_end_for_yoy(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary("130A", date(2025, 4, 24), eps_ttm=10.0, fiscal_period="1Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 7, 24), eps_ttm=20.0, fiscal_period="2Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2025, 10, 24), eps_ttm=30.0, fiscal_period="3Q", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 1, 24), eps_ttm=40.0, fiscal_period="FY", fiscal_year_end=date(2026, 3, 31)),
            _summary("130A", date(2026, 4, 24), eps_ttm=15.0, fiscal_period="1Q", fiscal_year_end=date(2027, 5, 31)),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        self.assertIsNone(result.financials["130A"].eps_yoy)

    def test_build_metrics_historical_ev_ebitda_uses_market_cap_plus_net_debt(self) -> None:
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar("130A", asof - timedelta(days=2), 80.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof - timedelta(days=1), 100.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        eps_ttm=10.0,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=10.0,
                    )
                ]
            },
            edinet_by_ticker={
                "130A": EdinetMetricRecord(
                    ticker="130A",
                    sales_ttm=1_000.0,
                    ocf_ttm=100.0,
                    debt=300.0,
                    cash=100.0,
                    ebitda_ttm=200.0,
                    consolidation_basis="consolidated",
                    ttm_quality_ev_ebitda=TTMQuality.EXACT,
                    ttm_quality_p_s=TTMQuality.EXACT,
                    ttm_quality_pcfr=TTMQuality.EXACT,
                )
            },
        )

        snapshot = result.financials["130A"]
        derived = result.derived["130A"]
        # Historical EV/EBITDA should be
        # (price * shares + debt - cash) / ebitda, not a collapsed price ratio.
        self.assertIsNotNone(snapshot.ev_ebitda)
        self.assertIsNotNone(derived.self_range_percentile["ev_ebitda"])
        self.assertIsNotNone(derived.sigma_gap["ev_ebitda"])
        self.assertAlmostEqual(snapshot.ev_ebitda, ((120.0 * 10.0) + 300.0 - 100.0) / 200.0)
        self.assertAlmostEqual(derived.self_range_percentile["ev_ebitda"], 1.0)
        history_values = [
            ((price * 10.0) + 300.0 - 100.0) / 200.0
            for price in (80.0, 100.0, 120.0)
        ]
        avg = sum(history_values) / len(history_values)
        variance = sum((value - avg) ** 2 for value in history_values) / len(history_values)
        expected_sigma_gap = (history_values[-1] - avg) / (variance**0.5)
        self.assertAlmostEqual(derived.sigma_gap["ev_ebitda"], expected_sigma_gap)


if __name__ == "__main__":
    unittest.main()
