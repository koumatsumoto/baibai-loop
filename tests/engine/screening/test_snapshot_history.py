"""旧較正リプレイの合成と共有計算の回帰。現行五モデルの候補gateではない。"""

from __future__ import annotations

from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _edinet_metric_record,
    _security,
    _summary,
)
from tests.engine.screening.metric_snapshot_fixtures import SnapshotFixtures

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.metric_builder import build_metrics
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar


class ScreeningMetricsTests(SnapshotFixtures):
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
        inputs = dict(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof - timedelta(days=20),
                        fiscal_period="FY",
                        period_start=date(2025, 4, 1),
                        period_end=date(2026, 3, 31),
                    )
                ]
            },
            edinet_by_ticker={},
        )
        baseline = build_metrics(**inputs, bars_by_ticker={"130A": base_bars})
        result = build_metrics(**inputs, bars_by_ticker={"130A": base_bars + future_bars})
        self.assertEqual(result.financials, baseline.financials)
        self.assertEqual(result.derived, baseline.derived)
        self.assertIn("130A", result.financials)
        # latest close picked from bars <= asof, not from the polluted 99999.0 future bars.
        per_trailing = result.financials["130A"].per_trailing
        self.assertIsNotNone(per_trailing)
        assert per_trailing is not None
        self.assertAlmostEqual(per_trailing, (100.0 + 399) / 18.0, places=5)
        # short_history_flag must be derived from bars <= asof only. 400d history < 750d ⇒ True.
        self.assertTrue(result.derived["130A"].short_history_flag)

    def test_valuation_history_uses_the_same_treasury_adjusted_capital_basis(self) -> None:
        """現在倍率と自己履歴の差に自己株分母の不一致を混ぜない。"""
        asof = date(2026, 7, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=119 - index),
                close=10.0,
                turnover_value=300_000_000.0,
            )
            for index in range(120)
        ]
        summary = _summary(
            "130A",
            asof - timedelta(days=20),
            fiscal_period="FY",
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
            shares_outstanding=1_000.0,
            treasury_shares=100.0,
            sales=9_000.0,
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [summary]},
            edinet_by_ticker={
                "130A": _edinet_metric_record(
                    sales_ttm=9_000.0,
                    debt=2_000.0,
                    cash=1_000.0,
                    ebitda_ttm=2_000.0,
                )
            },
            valuation_history_sessions=120,
        )
        financial = result.financials["130A"]
        self_median = result.derived["130A"].self_range_median

        assert self_median["p_s"] is not None
        assert financial.p_s is not None
        assert self_median["ev_ebitda"] is not None
        assert financial.ev_ebitda is not None
        self.assertAlmostEqual(self_median["p_s"], financial.p_s, places=6)
        self.assertAlmostEqual(self_median["ev_ebitda"], financial.ev_ebitda, places=6)

    def test_build_metrics_uses_observed_bars_span_for_short_history(self) -> None:
        """An 800-day observed price span is not short; it does not establish listing age."""
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
        """A long observed price span can still have low coverage relative to other tickers."""
        asof = date(2026, 4, 24)
        dense_bars = _daily_bars("1111", asof, 800)
        # Old bars exist (observed span >= 750d) but only the last 200 days have bars.
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

    def test_build_metrics_price_change_uses_factor_adjusted_prices_across_splits(self) -> None:
        # 2-for-1 split between the 60d-prior date and today:
        # raw close drops 100 -> 50, but the split factor normalizes both
        # ends to 50, so price_change_60d should not register a synthetic decline.
        asof = date(2026, 4, 24)
        bars = []
        # 分割前70本のraw価格100を、権利落ち日より後の価格50と比較する。
        for i in range(70):
            bars.append(
                JQuantsDailyBar(
                    "130A",
                    asof - timedelta(days=70 - i),
                    close=100.0,
                    turnover_value=300_000_000.0,
                )
            )
        # 権利落ち日 (= asof) の bar に factor 0.5。close は分割後 50。
        bars.append(
            JQuantsDailyBar(
                "130A",
                asof,
                close=50.0,
                turnover_value=300_000_000.0,
                adjustment_factor=0.5,
                ex_right_type="1",
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
                        cumulative_eps=10.0,
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
        # and would misreport an unchanged price basis as a 50% market decline.
        self.assertEqual(derived.price_change_60d, 0.0)
