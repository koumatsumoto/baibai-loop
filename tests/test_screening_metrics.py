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
    cfo: float | None = 100_000_000.0,
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
        cfo=cfo,
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


def _edinet_metric_record(
    code: str = "130A",
    *,
    sales_ttm: float = 1_000.0,
    ocf_ttm: float = 100.0,
    debt: float = 300.0,
    cash: float = 100.0,
    ebitda_ttm: float = 200.0,
    consolidation_basis: str = "consolidated",
    ttm_quality: TTMQuality = TTMQuality.EXACT,
    source_doc_id: str | None = "S100TEST",
    document_type: str | None = "120",
    source_submit_datetime: str | None = "2026-04-01 12:00",
    source_period_start: date | None = date(2025, 4, 1),
    source_period_end: date | None = date(2026, 3, 31),
) -> EdinetMetricRecord:
    return EdinetMetricRecord(
        ticker=code,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        debt=debt,
        cash=cash,
        ebitda_ttm=ebitda_ttm,
        consolidation_basis=consolidation_basis,
        ttm_quality_ev_ebitda=ttm_quality,
        ttm_quality_p_s=ttm_quality,
        ttm_quality_pcfr=ttm_quality,
        source_doc_id=source_doc_id,
        document_type=document_type,
        source_submit_datetime=source_submit_datetime,
        source_period_start=source_period_start,
        source_period_end=source_period_end,
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

    def test_build_metrics_records_price_history_coverage_for_gappy_ticker(self) -> None:
        """An old listing whose recent bars resume after a long gap gets low coverage."""
        asof = date(2026, 4, 24)
        dense_bars = _daily_bars("1111", asof, 800)
        # Old bars exist (listing span >= 750d) but only the last 200 days have bars.
        gappy_bars = [
            bar
            for bar in _daily_bars("130A", asof, 800)
            if bar.traded_at <= asof - timedelta(days=780)
            or bar.traded_at > asof - timedelta(days=200)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security(), "1111": _security("1111")},
            bars_by_ticker={"130A": gappy_bars, "1111": dense_bars},
            summaries_by_ticker={
                "130A": [_summary("130A", asof - timedelta(days=30))],
                "1111": [_summary("1111", asof - timedelta(days=30))],
            },
            edinet_by_ticker={},
        )
        gappy = result.derived["130A"]
        dense = result.derived["1111"]
        self.assertEqual(dense.price_history_coverage_750d, 1.0)
        self.assertFalse(gappy.short_history_flag)
        assert gappy.price_history_sessions_750d is not None
        self.assertEqual(gappy.price_history_sessions_750d, 200)
        assert gappy.price_history_coverage_750d is not None
        self.assertLess(gappy.price_history_coverage_750d, 0.8)

    def test_build_metrics_adds_short_dislocation_fields(self) -> None:
        asof = date(2026, 4, 24)
        start = asof - timedelta(days=29)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=start + timedelta(days=index),
                close=90.0 if index >= 25 else 100.0,
                adjustment_close=90.0 if index >= 25 else 100.0,
                turnover_value=300_000_000.0 if index >= 25 else 100_000_000.0,
            )
            for index in range(30)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [_summary("130A", asof - timedelta(days=30))]},
            edinet_by_ticker={},
        )

        derived = result.derived["130A"]
        self.assertEqual(derived.price_change_1d, 0.0)
        self.assertAlmostEqual(derived.price_change_5d or 0.0, -0.1)
        self.assertAlmostEqual(derived.price_change_20d or 0.0, -0.1)
        self.assertIsNone(derived.price_change_60d)
        self.assertEqual(derived.gap_from_52w_low, 0.0)
        self.assertAlmostEqual(derived.turnover_spike_5d or 0.0, 3.0)

    def test_build_metrics_compares_yoy_with_prior_fiscal_period(self) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                cfo=20.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=40.0,
                sales=400.0,
                operating_profit=80.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=50.0,
                sales=500.0,
                operating_profit=100.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=60.0,
                sales=600.0,
                operating_profit=120.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                sales=125.0,
                cfo=25.0,
                operating_profit=25.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
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
        self.assertIsNotNone(snapshot.cfo_yoy)
        assert snapshot.eps_yoy is not None
        assert snapshot.sales_yoy is not None
        assert snapshot.operating_profit_yoy is not None
        assert snapshot.cfo_yoy is not None
        # Old QoQ logic would compare EPS with the previous disclosure (60.0)
        # and produce -0.75. Fiscal-period matching locks YoY at +0.50.
        self.assertAlmostEqual(snapshot.eps_yoy, 0.5)
        self.assertAlmostEqual(snapshot.sales_yoy, 0.25)
        self.assertAlmostEqual(snapshot.operating_profit_yoy, 0.25)
        self.assertAlmostEqual(snapshot.cfo_yoy, 0.25)

    def test_build_metrics_sets_yoy_to_none_when_prior_year_summary_is_missing(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        date(2025, 7, 24),
                        fiscal_period="2Q",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2025, 10, 24),
                        fiscal_period="3Q",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2026, 1, 24),
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                    ),
                    _summary(
                        "130A",
                        date(2026, 4, 24),
                        fiscal_period="1Q",
                        fiscal_year_end=date(2027, 3, 31),
                    ),
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
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                sales=200.0,
                operating_profit=40.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                sales=300.0,
                operating_profit=60.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=80.0,
                sales=800.0,
                operating_profit=160.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=10.0,
                sales=100.0,
                operating_profit=20.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
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

    def test_build_metrics_uses_fiscal_period_match_when_extra_disclosure_shifts_index(
        self,
    ) -> None:
        asof = date(2026, 4, 24)
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 6, 1),
                eps_ttm=999.0,
                fiscal_period="OTHER",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=40.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 3, 31),
            ),
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
            _summary(
                "130A",
                date(2025, 4, 24),
                eps_ttm=10.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 7, 24),
                eps_ttm=20.0,
                fiscal_period="2Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2025, 10, 24),
                eps_ttm=30.0,
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 1, 24),
                eps_ttm=40.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 4, 24),
                eps_ttm=15.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2027, 5, 31),
            ),
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
            edinet_by_ticker={"130A": _edinet_metric_record()},
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
            ((price * 10.0) + 300.0 - 100.0) / 200.0 for price in (80.0, 100.0, 120.0)
        ]
        avg = sum(history_values) / len(history_values)
        variance = sum((value - avg) ** 2 for value in history_values) / len(history_values)
        expected_sigma_gap = (history_values[-1] - avg) / (variance**0.5)
        self.assertAlmostEqual(derived.sigma_gap["ev_ebitda"], expected_sigma_gap)

    def test_build_metrics_drops_ev_ebitda_when_ev_or_ebitda_is_non_positive(self) -> None:
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar("130A", asof - timedelta(days=2), 80.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof - timedelta(days=1), 100.0, 300_000_000.0),
            JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0),
        ]
        summaries = {
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
        }

        negative_ebitda = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker=summaries,
            edinet_by_ticker={"130A": _edinet_metric_record(ebitda_ttm=-10.0)},
        )
        self.assertIsNone(negative_ebitda.financials["130A"].ev_ebitda)
        self.assertIsNone(negative_ebitda.derived["130A"].self_range_percentile["ev_ebitda"])
        self.assertIsNone(negative_ebitda.derived["130A"].sigma_gap["ev_ebitda"])

        negative_ev = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker=summaries,
            edinet_by_ticker={
                "130A": _edinet_metric_record(cash=2_000.0, debt=0.0, ebitda_ttm=200.0)
            },
        )
        self.assertIsNone(negative_ev.financials["130A"].ev_ebitda)
        self.assertIsNone(negative_ev.derived["130A"].self_range_percentile["ev_ebitda"])
        self.assertIsNone(negative_ev.derived["130A"].sigma_gap["ev_ebitda"])

    def test_build_metrics_preserves_edinet_source_metadata(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 800)},
            summaries_by_ticker={"130A": [_summary("130A", asof)]},
            edinet_by_ticker={"130A": _edinet_metric_record()},
        )

        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.edinet_source_doc_id, "S100TEST")
        self.assertEqual(snapshot.edinet_document_type, "120")
        self.assertEqual(snapshot.edinet_source_submit_datetime, "2026-04-01 12:00")
        self.assertEqual(snapshot.edinet_source_period_start, date(2025, 4, 1))
        self.assertEqual(snapshot.edinet_source_period_end, date(2026, 3, 31))

    def test_build_metrics_price_change_uses_adjusted_close_across_splits(self) -> None:
        # 2-for-1 split between the 60d-prior date and today:
        # raw close drops 100 -> 50, but adjustment_close stays 50 on both
        # ends, so price_change_60d should not register a synthetic decline.
        asof = date(2026, 4, 24)
        bars = []
        # 70 sessions of pre-split history at raw=100, adj=50
        for i in range(70):
            bars.append(
                JQuantsDailyBar(
                    "130A",
                    asof - timedelta(days=70 - i),
                    close=100.0,
                    turnover_value=300_000_000.0,
                    adjustment_close=50.0,
                )
            )
        # Latest bar at raw=adj=50 (post-split day == today)
        bars.append(
            JQuantsDailyBar(
                "130A",
                asof,
                close=50.0,
                turnover_value=300_000_000.0,
                adjustment_close=50.0,
            )
        )
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
            edinet_by_ticker={},
        )
        derived = result.derived["130A"]
        # Without adjustment-aware change calc, this would be (50-100)/100 = -0.5
        # which would falsely trip the -15% screening threshold.
        self.assertEqual(derived.price_change_60d, 0.0)

    def test_build_metrics_historical_ev_ebitda_uses_adjusted_close_when_available(self) -> None:
        # Simulate a 2-for-1 stock split between asof-2 and asof-1 by giving
        # raw close a discontinuous jump while adjustment_close stays smooth.
        # Latest close (asof) is the unadjusted post-split price; historical
        # series should use adjustment_close so the split does not propagate
        # into self_range_percentile / sigma_gap.
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=2),
                close=160.0,
                turnover_value=300_000_000.0,
                adjustment_close=80.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=1),
                close=200.0,
                turnover_value=300_000_000.0,
                adjustment_close=100.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof,
                close=120.0,
                turnover_value=300_000_000.0,
                adjustment_close=120.0,
            ),
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
            edinet_by_ticker={"130A": _edinet_metric_record()},
        )

        derived = result.derived["130A"]
        # adjusted close history [80, 100, 120] -> EV/EBITDA history [5.0, 6.0, 7.0]
        expected_history = [
            ((price * 10.0) + 300.0 - 100.0) / 200.0 for price in (80.0, 100.0, 120.0)
        ]
        avg = sum(expected_history) / len(expected_history)
        variance = sum((value - avg) ** 2 for value in expected_history) / len(expected_history)
        expected_sigma_gap = (expected_history[-1] - avg) / (variance**0.5)
        self.assertAlmostEqual(derived.sigma_gap["ev_ebitda"], expected_sigma_gap)
        # If raw close were used the second sample (200.0 -> 21/2 = 10.5) would
        # dominate the percentile and push latest off the upper boundary.
        self.assertAlmostEqual(derived.self_range_percentile["ev_ebitda"], 1.0)

    def test_build_metrics_flags_split_adjustment_within_price_change_sessions(self) -> None:
        """split_adjustment_flag uses the same 60-session window as price_change_60d."""
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=(60 - index) * 2),
                close=100.0,
                turnover_value=300_000_000.0,
                adjustment_factor=0.5 if index == 20 else 1.0,
            )
            for index in range(61)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={
                "130A": [_summary("130A", asof)],
            },
            edinet_by_ticker={},
        )
        self.assertTrue(result.derived["130A"].split_adjustment_flag)

    def test_build_metrics_does_not_flag_split_adjustment_when_factor_is_one(self) -> None:
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=60 - index),
                close=100.0,
                turnover_value=300_000_000.0,
                adjustment_factor=1.0,
            )
            for index in range(61)
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={
                "130A": [_summary("130A", asof)],
            },
            edinet_by_ticker={},
        )
        self.assertFalse(result.derived["130A"].split_adjustment_flag)

    def test_build_metrics_does_not_flag_split_adjustment_when_factor_is_missing(self) -> None:
        asof = date(2026, 4, 24)
        bars = _daily_bars("130A", asof, 100)
        # all bars from _daily_bars have adjustment_factor=None by default,
        # which should not trip the flag.
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={
                "130A": [_summary("130A", asof)],
            },
            edinet_by_ticker={},
        )
        self.assertFalse(result.derived["130A"].split_adjustment_flag)


if __name__ == "__main__":
    unittest.main()


class MedianPopulationTests(unittest.TestCase):
    def test_sector_median_uses_only_population_tickers(self) -> None:
        from datetime import date as _date

        from baibai_loop.screening.metrics import build_metrics
        from baibai_loop.screening.providers.jquants import (
            JQuantsDailyBar,
            JQuantsFinancialSummary,
        )
        from baibai_loop.screening.schema import SecurityMaster

        asof = _date(2026, 4, 24)

        def bars(code: str, close: float) -> list[JQuantsDailyBar]:
            from datetime import timedelta

            return [
                JQuantsDailyBar(
                    ticker=code,
                    traded_at=asof - timedelta(days=30 - i),
                    close=close,
                    turnover_value=2.0e8,
                )
                for i in range(30)
            ]

        def summary(code: str, eps: float) -> JQuantsFinancialSummary:
            return JQuantsFinancialSummary(
                ticker=code,
                disclosed_at=_date(2026, 2, 1),
                eps_ttm=eps,
                type_of_current_period="FY",
            )

        securities = {
            code: SecurityMaster(
                code=code,
                name=f"name-{code}",
                market_segment="プライム",
                sector_33="機械",
                is_common_stock=True,
            )
            # 12 tickers so the sector clears the n>=10 sector-median threshold.
            for code in [f"11{i:02d}" for i in range(12)]
        }
        bars_by = {code: bars(code, close=100.0) for code in securities}
        # Liquid population PERs are all 10 (eps 10); the out-of-population
        # ticker has PER 100 (eps 1) and would distort the median if included.
        summaries = {code: [summary(code, eps=10.0)] for code in securities}
        summaries["1111"] = [summary("1111", eps=1.0)]
        population = frozenset(code for code in securities if code != "1111")

        result = build_metrics(
            asof_date=asof,
            securities_by_ticker=securities,
            bars_by_ticker=bars_by,
            summaries_by_ticker=summaries,
            edinet_by_ticker={},
            median_population=population,
        )

        gap = result.derived["1111"].sector_median_gap.get("per_trailing")
        assert gap is not None
        # PER 100 vs liquid-population median 10 -> gap = 9.0; a full-population
        # median would shift the baseline and lower the gap.
        self.assertAlmostEqual(gap, 9.0, places=6)
