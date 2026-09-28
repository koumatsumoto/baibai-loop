from __future__ import annotations

from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _security,
    _summary,
)
from tests.engine.screening.metric_snapshot_fixtures import SnapshotFixtures

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.screening.metrics import (
    build_metrics,
    build_shareholder_return_change_signals,
)


class ScreeningMetricsTests(SnapshotFixtures):
    def test_shareholder_return_change_builds_preregistered_components(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=2.0,
                dps_forecast_annual=2.0,
                shares_outstanding=100.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                dps_actual_annual=2.0,
                dps_forecast_annual=2.0,
                shares_outstanding=99.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                dps_actual_annual=3.0,
                dps_forecast_annual=4.0,
                shares_outstanding=98.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, (), asof)

        self.assertTrue(result.dps_streak_up)
        self.assertAlmostEqual(result.dps_yoy_latest or 0.0, 0.5)
        self.assertTrue(result.dps_guidance_up)
        self.assertFalse(result.dividend_initiation)
        self.assertEqual(result.share_count_reduction_streak, 2)
        self.assertTrue(result.shareholder_return_change)

    def test_zero_dividend_series_is_observed_across_february_fiscal_years(self) -> None:
        rows = [
            _summary(
                "130A",
                date(year, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(year, 2, 29 if year == 2024 else 28),
                dps_actual_annual=0.0,
                dps_forecast_annual=0.0,
                shares_outstanding=100.0,
            )
            for year in (2023, 2024, 2025)
        ]
        result = build_shareholder_return_change_signals(rows, (), date(2025, 5, 1))
        self.assertIs(result.dps_streak_up, True)
        self.assertEqual(result.dps_yoy_latest, 0.0)
        self.assertIs(result.dps_guidance_up, False)
        self.assertIs(result.dividend_initiation, False)

    def test_shareholder_return_change_handles_initiation_and_missing_history(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                dps_actual_annual=0.0,
                dps_forecast_annual=0.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                dps_actual_annual=2.0,
                dps_forecast_annual=3.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, (), asof)

        self.assertIsNone(result.dps_streak_up)
        self.assertIsNone(result.dps_yoy_latest)
        self.assertTrue(result.dividend_initiation)
        self.assertIsNone(result.share_count_reduction_streak)
        self.assertTrue(result.shareholder_return_change)

    def test_shareholder_return_change_does_not_fallback_from_latest_null_revision(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=1.0,
                shares_outstanding=100.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                dps_actual_annual=2.0,
                shares_outstanding=99.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                dps_actual_annual=3.0,
                shares_outstanding=98.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 20),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                dps_actual_annual=None,
                dps_forecast_annual=None,
                shares_outstanding=0.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, (), asof)

        self.assertIsNone(result.dps_streak_up)
        self.assertIsNone(result.dps_yoy_latest)
        self.assertIsNone(result.dps_guidance_up)
        self.assertIsNone(result.share_count_reduction_streak)
        self.assertIsNone(result.shareholder_return_change)

    def test_shareholder_return_change_rejects_proven_average_shares_alias(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                shares_outstanding=1_000.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                shares_outstanding=1_000.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                shares_outstanding=900.0,
                treasury_shares=100.0,
                average_shares=900.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, (), asof)

        self.assertIsNone(result.share_count_reduction_streak)
        self.assertIsNone(result.shareholder_return_change)

    def test_shareholder_return_change_normalizes_splits_across_fiscal_years(self) -> None:
        asof = date(2026, 6, 30)
        split_date = date(2024, 1, 10)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=split_date,
                close=50.0,
                turnover_value=1_000_000.0,
                adjustment_factor=0.5,
            )
        ]
        summaries = [
            _summary(
                "130A",
                date(2023, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2023, 3, 31),
                dps_actual_annual=40.0,
                shares_outstanding=100.0,
            ),
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=44.0,
                shares_outstanding=200.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                dps_actual_annual=24.0,
                shares_outstanding=190.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, bars, asof)

        # 分割は FY2024 の accrual 期間 (2023-05-10〜2024-05-10) に入る。その年度の
        # 44 円が分割前基準か後かは支払ごとの基準日を見ないと言えないので、増配とも
        # 減配とも判定しない。株数側の signal は配当と独立なので残る。
        self.assertIsNone(result.dps_streak_up)
        self.assertIsNone(result.dps_yoy_latest)
        self.assertEqual(result.share_count_reduction_streak, 1)

    def test_shareholder_return_change_normalizes_splits_after_the_last_disclosure(self) -> None:
        # 分割が最新の通期開示より後なら、どの年度の accrual 期間にも入らない。各行は
        # 正規化で asof 基準へ揃うので、増配 streak と YoY はそのまま出る。
        asof = date(2026, 6, 30)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=date(2025, 6, 10),
                close=50.0,
                turnover_value=1_000_000.0,
                adjustment_factor=0.5,
            )
        ]
        summaries = [
            _summary(
                "130A",
                date(2023, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2023, 3, 31),
                dps_actual_annual=40.0,
                shares_outstanding=100.0,
            ),
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=44.0,
                shares_outstanding=200.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                dps_actual_annual=48.0,
                shares_outstanding=190.0,
            ),
        ]

        result = build_shareholder_return_change_signals(summaries, bars, asof)

        self.assertTrue(result.dps_streak_up)
        self.assertAlmostEqual(result.dps_yoy_latest or 0.0, (24.0 / 22.0) - 1.0)

    def test_dividend_fields_split_normalization_and_carry_forward(self) -> None:
        """実績 DPS は分割跨ぎ行で x factor 換算、予想 DPS は None 化。
        実績年間 DPS は FY 行にしか載らないため、直近が四半期行でも
        直近の非 null 行 (FY) から carry-forward して dividend_yield を出す。"""
        asof = date(2026, 7, 1)
        security = _security()
        bars = []
        for index in range(30):
            traded_at = asof - timedelta(days=29 - index)
            bars.append(
                JQuantsDailyBar(
                    ticker="130A",
                    traded_at=traded_at,
                    close=100.0 if index < 27 else 50.0,
                    turnover_value=300_000_000.0,
                    adjustment_factor=0.5 if index == 27 else 1.0,
                )
            )
        summaries = [
            # FY 行 (分割前開示): DivAnn=40 円 → asof-basis で 20 円。
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                dps_actual_annual=40.0,
                dps_forecast_annual=44.0,
            ),
            # 分割後の 1Q 行: 実績年間 DPS は載らない (None)。
            _summary(
                "130A",
                asof - timedelta(days=1),
                fiscal_period="1Q",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 30),
                dps_actual_annual=None,
                dps_forecast_annual=22.0,
            ),
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        # FY 行の実績 40 円は分割 (factor 0.5) を跨ぐため 20 円へ換算され、
        # 直近 1Q 行に実績が無くても carry-forward される。
        assert financial.dps_actual_annual is not None
        self.assertAlmostEqual(financial.dps_actual_annual, 20.0, places=6)
        # carry 用配当利回りは跳ねではない予想 DPS を優先する。FY 行 (分割跨ぎ) の予想は
        # None 化されるが、分割後 1Q 行の 22 円が最新の予想として使われる。
        assert financial.dividend_yield is not None
        self.assertAlmostEqual(financial.dividend_yield, 22.0 / 50.0, places=6)
        self.assertEqual(financial.dividend_basis, "forecast_annual")
        self.assertAlmostEqual(financial.dps_forecast_annual or 0.0, 22.0, places=6)
