from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent
from baibai_engine.screening.calibration.forward import (
    _asof_basis_dividend as _forward_asof_basis_dividend,
)
from baibai_engine.screening.calibration.forward import (
    _FYDividendObservation,
)
from baibai_engine.screening.metrics import (
    MetricBuildResult,
    _asof_basis_dividend,
    _common_equity_yen,
    _normalize_summaries_to_asof_basis,
    _resolve_capital_basis,
    _resolve_dividend_carry,
    _shares_excluding_treasury,
    _ttm_value,
    build_metrics,
    build_normalized_profit_signals,
    build_shareholder_return_change_signals,
    build_shares_outstanding_index,
)
from baibai_engine.screening.providers.edinet import EdinetMetricRecord
from baibai_engine.screening.providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.schema import (
    FinancialSnapshot,
    OperatingProfitSource,
    SecurityMaster,
    TTMQuality,
)


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
    eps_ttm: float | None = 18.0,
    forecast_eps: float | None = 20.0,
    sales: float = 1_000_000_000.0,
    cfo: float | None = 100_000_000.0,
    operating_profit: float | None = 100_000_000.0,
    ordinary_profit: float | None = None,
    fiscal_period: str | None = "1Q",
    fiscal_year_end: date | None = date(2026, 3, 31),
    shares_outstanding: float = 400_000_000.0,
    period_start: date | None = None,
    period_end: date | None = None,
    dps_actual_annual: float | None = None,
    dps_forecast_annual: float | None = None,
    forecast_profit: float | None = None,
    forecast_ordinary_profit: float | None = None,
    # 既定は「自己株式ゼロを観測した」。欠損 (None) は時価総額を出さない別の状態なので、
    # それを試す test だけが明示的に None を渡す。
    treasury_shares: float | None = 0.0,
    dividend_q1: float | None = None,
    dividend_interim: float | None = None,
    dividend_q3: float | None = None,
    dividend_year_end: float | None = None,
    dividend_total_annual: float | None = None,
    average_shares: float | None = None,
    profit: float | None = None,
    total_assets: float | None = None,
    equity_to_asset_ratio: float | None = None,
) -> JQuantsFinancialSummary:
    # 報告純利益は 1 株当たり当期純利益 x 自己株控除後株数と一致する。trailing 倍率も
    # accruals もこの行から出るので、既定値は行の中でその恒等式を満たす値にする。恒等式を
    # 破った行を試す test だけが `profit` を明示的に渡す。
    if profit is None and eps_ttm is not None:
        profit = eps_ttm * (shares_outstanding - (treasury_shares or 0.0))
    return JQuantsFinancialSummary(
        ticker=code,
        disclosed_at=disclosed_at,
        forecast_eps=forecast_eps,
        eps_ttm=eps_ttm,
        bps=120.0,
        shares_outstanding=shares_outstanding,
        sales=sales,
        cfo=cfo,
        total_assets=total_assets,
        equity_to_asset_ratio=equity_to_asset_ratio,
        operating_profit=operating_profit,
        ordinary_profit=ordinary_profit,
        profit=profit,
        forecast_profit=forecast_profit,
        forecast_ordinary_profit=forecast_ordinary_profit,
        fiscal_period=fiscal_period,
        fiscal_year_end=fiscal_year_end,
        period_start=period_start,
        period_end=period_end,
        dps_actual_annual=dps_actual_annual,
        dps_forecast_annual=dps_forecast_annual,
        treasury_shares=treasury_shares,
        dividend_q1=dividend_q1,
        dividend_interim=dividend_interim,
        dividend_q3=dividend_q3,
        dividend_year_end=dividend_year_end,
        dividend_total_annual=dividend_total_annual,
        average_shares=average_shares,
    )


def _security(code: str = "130A") -> SecurityMaster:
    return SecurityMaster(
        code=code,
        name="Alpha",
        market_segment="Prime",
        sector_33="情報・通信業",
        is_common_stock=True,
    )


def _quality_summary(
    *,
    disclosed_at: date,
    fiscal_year_end: date,
    period_start: date,
    period_end: date,
    eps: float,
    shares: float,
    sales: float,
    cfo: float,
    operating_profit: float | None,
    ordinary_profit: float | None = None,
    profit: float | None = None,
    total_assets: float,
    equity: float,
) -> JQuantsFinancialSummary:
    return JQuantsFinancialSummary(
        ticker="130A",
        disclosed_at=disclosed_at,
        forecast_eps=eps,
        eps_ttm=eps,
        bps=equity / shares,
        shares_outstanding=shares,
        sales=sales,
        cfo=cfo,
        total_assets=total_assets,
        equity=equity,
        operating_profit=operating_profit,
        ordinary_profit=ordinary_profit,
        profit=profit,
        fiscal_period="FY",
        fiscal_year_end=fiscal_year_end,
        period_start=period_start,
        period_end=period_end,
    )


def _edinet_metric_record(
    code: str = "130A",
    *,
    sales_ttm: float = 1_000.0,
    ocf_ttm: float = 100.0,
    debt: float = 300.0,
    cash: float = 100.0,
    investment_securities: float | None = None,
    ebitda_ttm: float = 200.0,
    consolidation_basis: str = "consolidated",
    ttm_quality: TTMQuality = TTMQuality.EXACT,
    source_doc_id: str | None = "S100TEST",
    document_type: str | None = "120",
    source_submit_datetime: str | None = "2026-04-01 12:00",
    source_period_start: date | None = date(2025, 4, 1),
    source_period_end: date | None = date(2026, 3, 31),
    total_assets: float | None = None,
) -> EdinetMetricRecord:
    return EdinetMetricRecord(
        ticker=code,
        total_assets=total_assets,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        debt=debt,
        cash=cash,
        investment_securities=investment_securities,
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
    def test_normalized_profit_does_not_fallback_from_null_revision(self) -> None:
        asof = date(2026, 6, 30)
        summaries = [
            _summary(
                "130A",
                date(year + 1, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(year + 1, 3, 31),
                eps_ttm=20.0,
            )
            for year in range(2023, 2026)
        ]
        summaries.append(
            _summary(
                "130A",
                date(2026, 5, 20),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                eps_ttm=None,
            )
        )

        result = build_normalized_profit_signals(summaries, (), asof, close=200.0)

        self.assertIsNone(result.normalized_per_3fy)

    def test_normalized_profit_uses_split_basis_and_rejects_nonpositive_mean(self) -> None:
        asof = date(2026, 6, 30)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=date(2024, 1, 10),
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
                eps_ttm=40.0,
            ),
            _summary(
                "130A",
                date(2024, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 3, 31),
                eps_ttm=20.0,
            ),
            _summary(
                "130A",
                date(2025, 5, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                eps_ttm=20.0,
            ),
        ]
        split_safe = build_normalized_profit_signals(summaries, bars, asof, close=100.0)
        loss_mean = build_normalized_profit_signals(
            [
                replace(summary, eps_ttm=value)
                for summary, value in zip(summaries, (-40.0, 0.0, 10.0), strict=True)
            ],
            (),
            asof,
            close=100.0,
        )

        self.assertAlmostEqual(split_safe.normalized_per_3fy or 0.0, 5.0)
        self.assertIsNone(loss_mean.normalized_per_3fy)

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
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof - timedelta(days=30),
                        fiscal_period="FY",
                        period_start=date(2025, 4, 1),
                        period_end=date(2026, 3, 31),
                    )
                ]
            },
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

    def _forecast_gain_snapshot(
        self, *, forecast_profit: float | None, forecast_ordinary_profit: float | None
    ) -> FinancialSnapshot:
        asof = date(2026, 7, 1)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 40)},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof - timedelta(days=30),
                        forecast_profit=forecast_profit,
                        forecast_ordinary_profit=forecast_ordinary_profit,
                    )
                ]
            },
            edinet_by_ticker={},
        )
        return result.financials["130A"]

    def test_forecast_special_gain_flag_set_when_net_income_exceeds_ordinary(self) -> None:
        # 会社予想で純利益>経常なら特別益をほぼ確定する (税負担が通常正)。flag を立てる。
        snapshot = self._forecast_gain_snapshot(
            forecast_profit=5_464_000_000.0, forecast_ordinary_profit=3_406_000_000.0
        )
        self.assertTrue(snapshot.forecast_special_gain_flag)

    def test_forecast_special_gain_flag_clear_when_net_income_not_above_ordinary(self) -> None:
        # 純利益<=経常 (通常の税負担後) では立てない。
        snapshot = self._forecast_gain_snapshot(
            forecast_profit=2_400_000_000.0, forecast_ordinary_profit=3_406_000_000.0
        )
        self.assertFalse(snapshot.forecast_special_gain_flag)

    def test_forecast_special_gain_flag_clear_when_either_forecast_missing(self) -> None:
        # 片方でも欠損なら比較不能なので立てない (誤検出回避)。
        self.assertFalse(
            self._forecast_gain_snapshot(
                forecast_profit=5_464_000_000.0, forecast_ordinary_profit=None
            ).forecast_special_gain_flag
        )
        self.assertFalse(
            self._forecast_gain_snapshot(
                forecast_profit=None, forecast_ordinary_profit=3_406_000_000.0
            ).forecast_special_gain_flag
        )

    def test_forecast_full_year_loss_flag_set_when_either_forecast_is_negative(self) -> None:
        # 2491 の 2026-07-29 開示は経常 △700 / 純利益 △800。どちらか一方が負なら立てる。
        self.assertTrue(
            self._forecast_gain_snapshot(
                forecast_profit=-800_000_000.0, forecast_ordinary_profit=-700_000_000.0
            ).forecast_full_year_loss_flag
        )
        self.assertTrue(
            self._forecast_gain_snapshot(
                forecast_profit=-800_000_000.0, forecast_ordinary_profit=None
            ).forecast_full_year_loss_flag
        )
        self.assertTrue(
            self._forecast_gain_snapshot(
                forecast_profit=None, forecast_ordinary_profit=-700_000_000.0
            ).forecast_full_year_loss_flag
        )

    def test_forecast_full_year_loss_flag_clear_when_forecasts_are_positive(self) -> None:
        snapshot = self._forecast_gain_snapshot(
            forecast_profit=480_000_000.0, forecast_ordinary_profit=1_480_000_000.0
        )
        self.assertFalse(snapshot.forecast_full_year_loss_flag)

    def test_forecast_full_year_loss_flag_clear_when_no_forecast_is_disclosed(self) -> None:
        # 予想が 1 つも無い行は「黒字予想」ではない。欠損を黒字へ畳まないことを固定する。
        snapshot = self._forecast_gain_snapshot(forecast_profit=None, forecast_ordinary_profit=None)
        self.assertFalse(snapshot.forecast_full_year_loss_flag)

    def test_market_cap_adjusts_shares_for_split_after_disclosure(self) -> None:
        """開示後の分割 (権利落ち bar の adjustment_factor) を株数へ補正する。

        開示時点の株数 100 株・1:2 分割 (factor 0.5) 後の終値 50 のとき、
        naive な 100 x 50 = 5,000 ではなく 200 x 50 = 10,000 が market cap。
        """
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
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=100.0,
            )
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        assert financial.market_cap is not None
        self.assertAlmostEqual(financial.market_cap, 50.0 * 200.0, places=3)
        self.assertAlmostEqual(financial.shares_outstanding or 0.0, 200.0, places=3)
        # 分割を跨ぐ行の forecast_eps は基準 (分割考慮前/後) を機械判別できないため
        # None に落ち、per_forward は出ない (偽値を出さない)。
        self.assertIsNone(financial.per_forward)

    def _capital_snapshot(
        self,
        *,
        shares_outstanding: float,
        treasury_shares: float | None,
        equity_to_asset_ratio: float | None,
        bps: float,
        total_assets: float,
        equity: float,
        close: float,
    ) -> FinancialSnapshot:
        asof = date(2026, 7, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=close,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        summary = replace(
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=shares_outstanding,
            ),
            bps=bps,
            total_assets=total_assets,
            equity=equity,
            treasury_shares=treasury_shares,
            equity_to_asset_ratio=equity_to_asset_ratio,
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [summary]},
            edinet_by_ticker={},
        )
        return result.financials["130A"]

    def test_market_cap_excludes_treasury_shares(self) -> None:
        """時価総額の分母は市場が値付けできる株数 (発行済 - 自己株式) である。"""
        # 自己株 100 株 (10%)、非支配株主持分 200 (自己資本 800 / 純資産 1,000)。
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=100.0,
            equity_to_asset_ratio=0.4,
            bps=800.0 / 900.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertAlmostEqual(snapshot.market_cap or 0.0, 900.0, places=6)
        self.assertAlmostEqual(snapshot.market_price_yen or 0.0, 1.0, places=6)
        self.assertAlmostEqual(snapshot.shares_ex_treasury or 0.0, 900.0, places=6)
        self.assertAlmostEqual(snapshot.equity_ratio or 0.0, 0.4, places=6)

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

        self.assertAlmostEqual(self_median["p_s"] or 0.0, financial.p_s or 0.0, places=6)
        self.assertAlmostEqual(
            self_median["ev_ebitda"] or 0.0,
            financial.ev_ebitda or 0.0,
            places=6,
        )

    def test_market_cap_is_absent_when_treasury_is_unobserved(self) -> None:
        """自己株式数が欠損する行で発行済を代用しない。

        代用すると、どれだけ過大か分からない時価総額が現金比率・利回り・流動性 gate へ
        入る。答えないことで、その銘柄は母集団から外れる。
        """
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=None,
            equity_to_asset_ratio=0.4,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.market_cap)

    def test_equity_ratio_is_absent_rather_than_the_net_asset_ratio(self) -> None:
        """自己資本比率が観測できない行で純資産比率へ代用しない。

        純資産は非支配株主持分を含むので、代用は少数株主持分の大きい銘柄で自己資本比率を
        数 pt 過大にする。過大は screening gate を通しやすくする向きなので黙って埋めない。
        """
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=0.0,
            equity_to_asset_ratio=None,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.equity_ratio)

    def test_broken_treasury_count_leaves_no_market_cap(self) -> None:
        """発行済を超える自己株式数で負や過大な時価総額を作らない。"""
        snapshot = self._capital_snapshot(
            shares_outstanding=1_000.0,
            treasury_shares=1_200.0,
            equity_to_asset_ratio=0.4,
            bps=1.0,
            total_assets=2_000.0,
            equity=1_000.0,
            close=1.0,
        )

        self.assertIsNone(snapshot.market_cap)

    def test_treasury_shares_follow_the_split_basis_of_issued_shares(self) -> None:
        """分割を跨ぐ行で自己株式数も換算する。片方だけだと差が壊れる。"""
        asof = date(2026, 7, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0 if index < 27 else 50.0,
                turnover_value=300_000_000.0,
                adjustment_factor=0.5 if index == 27 else 1.0,
            )
            for index in range(30)
        ]
        summary = replace(
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=100.0,
            ),
            treasury_shares=10.0,
            equity_to_asset_ratio=0.4,
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [summary]},
            edinet_by_ticker={},
        )

        financial = result.financials["130A"]
        # 1:2 分割後は発行済 200 株・自己株 20 株なので、時価総額は 50 x 180。
        self.assertAlmostEqual(financial.market_cap or 0.0, 50.0 * 180.0, places=3)

    def test_universe_share_index_matches_the_snapshot_market_cap_basis(self) -> None:
        """「時価総額」という同じ語が 2 つの値を指さないことを固定する。

        universe の時価総額は `build_shares_outstanding_index` から作られて流動性 gate の
        分母になり、`FinancialSnapshot.market_cap` は倍率と利回りの分母になる。別々に
        計算されているので、株数の基準が片方だけ動くと同じ語が食い違う。
        """
        asof = date(2026, 7, 1)
        close = 1_000.0
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=close,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        summary = replace(
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_000_000.0,
            ),
            treasury_shares=250_000.0,
            equity_to_asset_ratio=0.5,
        )
        summaries_by_ticker = {"130A": [summary]}
        bars_by_ticker = {"130A": bars}

        index = build_shares_outstanding_index(summaries_by_ticker, bars_by_ticker, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker=bars_by_ticker,
            summaries_by_ticker=summaries_by_ticker,
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertAlmostEqual(index["130A"] or 0.0, 750_000.0, places=3)
        assert snapshot.market_cap is not None
        self.assertAlmostEqual(snapshot.market_cap, close * (index["130A"] or 0.0), places=3)

    def test_shares_excluding_treasury_rejects_invalid_values(self) -> None:
        self.assertIsNone(_shares_excluding_treasury(1_000.0, -1.0))
        self.assertIsNone(_shares_excluding_treasury(0.0, 0.0))
        self.assertIsNone(_shares_excluding_treasury(1_000.0, 1_000.0))
        self.assertEqual(_shares_excluding_treasury(1_000.0, 100.0), 900.0)

    def test_treasury_carry_stops_after_issued_shares_decrease(self) -> None:
        """消却後の発行済から消却前の自己株をもう一度引かない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=300.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=700.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.shares_outstanding, 700.0)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_stops_after_issued_shares_increase(self) -> None:
        """増資と同時の自己株処分を観測できないため、古い自己株を再控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_100.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.shares_ex_treasury)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_known_valid_old_treasury_still_fails_closed_without_current_provenance(self) -> None:
        """一次資料で旧10株が有効でも、lossy入力だけでは無効例と区別できない。"""

        rows = [
            _summary(
                "4889",
                date(2026, 2, 13),
                shares_outstanding=12_986_700.0,
                treasury_shares=10.0,
            ),
            _summary(
                "4889",
                date(2026, 5, 13),
                shares_outstanding=13_776_900.0,
                treasury_shares=None,
                average_shares=12_878_188.0,
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertIsNone(result.shares_ex_treasury)
        self.assertEqual(
            result.failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_stops_after_same_gross_issued_is_reobserved(self) -> None:
        """自己株0が欠損になるsourceで、処分前の正値を同じgrossから再控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_000.0,
                treasury_shares=None,
                average_shares=950.0,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_treasury_carry_does_not_revive_after_decrease_then_increase(self) -> None:
        """消却前の自己株basisは、後日の増資でgrossが回復しても復活しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=200),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=1_100.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.shares_outstanding, 1_100.0)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_new_treasury_observation_resets_incompatible_prior_basis(self) -> None:
        """消却後の新しい自己株観測があれば、その新資本状態から再開する。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=200),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="FY",
                shares_outstanding=1_100.0,
                treasury_shares=50.0,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(index["130A"], 1_050.0)
        self.assertEqual(snapshot.shares_ex_treasury, 1_050.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_explicit_zero_treasury_resets_incompatible_prior_basis(self) -> None:
        """新しい0株観測は、古い正の自己株stateを安全に置き換える。"""

        rows = [
            _summary(
                "130A",
                date(2025, 12, 11),
                shares_outstanding=39_063_600.0,
                treasury_shares=1_988_126.0,
            ),
            _summary(
                "130A",
                date(2026, 3, 12),
                shares_outstanding=41_194_972.0,
                treasury_shares=None,
            ),
            _summary(
                "130A",
                date(2026, 6, 11),
                shares_outstanding=41_194_972.0,
                treasury_shares=0.0,
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertEqual(result.shares_ex_treasury, 41_194_972.0)
        self.assertIsNone(result.failure_reason)

    def test_zero_treasury_carry_continues_across_issued_shares_decrease(self) -> None:
        """自己株0なら発行済が減っても二重控除は起きないので値を維持する。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=0.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                shares_outstanding=900.0,
                treasury_shares=None,
            ),
        ]

        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(index["130A"], 900.0)
        self.assertEqual(snapshot.shares_ex_treasury, 900.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_zero_treasury_carry_does_not_require_issued_source_basis(self) -> None:
        """自己株0ならsource issuedが欠損しても差し引きは安全である。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        treasury_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=0.0,
            ),
            shares_outstanding=None,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=900.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [treasury_only, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(snapshot.shares_ex_treasury, 900.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_average_share_alias_shape_has_no_market_cap(self) -> None:
        """期中平均+自己株が過去grossへ戻る既存行から自己株を二重控除しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        prior = _summary(
            "130A",
            asof - timedelta(days=300),
            fiscal_period="FY",
            shares_outstanding=1_000.0,
            average_shares=900.0,
            treasury_shares=100.0,
        )
        contaminated = _summary(
            "130A",
            asof - timedelta(days=20),
            fiscal_period="1Q",
            shares_outstanding=900.0,
            average_shares=900.0,
            treasury_shares=100.0,
        )

        summaries = [prior, contaminated]
        index = build_shares_outstanding_index({"130A": summaries}, {"130A": bars}, asof)
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(index["130A"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "issued_matches_average_with_positive_treasury",
        )

    def test_equal_average_without_prior_gross_reconstruction_is_allowed(self) -> None:
        """ShOutFYとAvgShの同値だけでは正常な1Q行を除外しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        summaries = [
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=900.0,
                average_shares=899.0,
                treasury_shares=100.0,
            ),
            _summary(
                "130A",
                asof - timedelta(days=20),
                fiscal_period="1Q",
                shares_outstanding=900.0,
                average_shares=900.0,
                treasury_shares=100.0,
            ),
        ]

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertEqual(snapshot.shares_ex_treasury, 800.0)
        self.assertIsNone(snapshot.capital_basis_failure_reason)

    def test_invalid_capital_values_have_no_market_cap(self) -> None:
        """負の自己株や発行済以上の自己株を有効な株式数へ変換しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)

        for treasury_shares in (-1.0, 1_000.0):
            with self.subTest(treasury_shares=treasury_shares):
                summary = _summary(
                    "130A",
                    asof - timedelta(days=20),
                    fiscal_period="FY",
                    shares_outstanding=1_000.0,
                    treasury_shares=treasury_shares,
                )
                snapshot = build_metrics(
                    asof_date=asof,
                    securities_by_ticker={"130A": _security()},
                    bars_by_ticker={"130A": bars},
                    summaries_by_ticker={"130A": [summary]},
                    edinet_by_ticker={},
                ).financials["130A"]

                self.assertIsNone(snapshot.market_cap)
                self.assertEqual(
                    snapshot.capital_basis_failure_reason,
                    "invalid_issued_or_treasury_shares",
                )

    def test_treasury_carry_requires_issued_basis_at_its_source(self) -> None:
        """自己株観測行の発行済が無ければ後日の発行済との両立を推測しない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        treasury_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=300),
                fiscal_period="FY",
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            shares_outstanding=None,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=1_100.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [treasury_only, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "treasury_observation_without_issued_basis",
        )

    def test_treasury_carry_rejects_invalid_capital_at_its_source(self) -> None:
        """無効行の自己株だけを後日の正常な発行済へcarryしない。"""
        asof = date(2026, 7, 1)
        bars = _daily_bars("130A", asof, 40)
        invalid_source = _summary(
            "130A",
            asof - timedelta(days=300),
            fiscal_period="FY",
            shares_outstanding=1_000.0,
            treasury_shares=1_000.0,
        )
        latest_issued = _summary(
            "130A",
            asof - timedelta(days=20),
            shares_outstanding=1_100.0,
            treasury_shares=None,
        )

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [invalid_source, latest_issued]},
            edinet_by_ticker={},
        ).financials["130A"]

        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(
            snapshot.capital_basis_failure_reason,
            "invalid_treasury_source_capital_basis",
        )

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
        # carry 用配当利回りは予想 DPS を最優先する。FY 行 (分割跨ぎ) の予想は
        # None 化されるが、分割後 1Q 行の 22 円が最新の予想として使われる。
        assert financial.dividend_yield is not None
        self.assertAlmostEqual(financial.dividend_yield, 22.0 / 50.0, places=6)
        self.assertEqual(financial.dividend_basis, "forecast_annual")
        self.assertAlmostEqual(financial.dps_forecast_annual or 0.0, 22.0, places=6)

    def test_bs_fields_carry_forward_from_recent_disclosure(self) -> None:
        """bps 等の BS 系 fact は latest の四半期行に無くても直近 FY 行から
        carry-forward され、staleness fact が付く。"""
        asof = date(2026, 1, 30)
        security = _security()
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        fy = _summary(
            "130A",
            asof - timedelta(days=200),
            fiscal_period="FY",
            period_start=date(2024, 4, 1),
            period_end=date(2025, 3, 31),
            # 自己資本は円で 4.8e10 (= bps 120 x 自己株控除後 400M) になる値を置く。
            # PBR は円経路で組むので、この 2 つが carry-forward される側になる。
            total_assets=8e10,
            equity_to_asset_ratio=0.6,
        )
        quarterly = JQuantsFinancialSummary(
            ticker="130A",
            disclosed_at=asof - timedelta(days=10),
            forecast_eps=20.0,
            eps_ttm=5.0,
            bps=None,
            shares_outstanding=None,
            sales=250_000_000.0,
            cfo=None,
            cash_eq=None,
            total_assets=None,
            equity=None,
            operating_profit=50_000_000.0,
            ordinary_profit=None,
            profit=None,
            fiscal_period="3Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 12, 31),
        )
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [fy, quarterly]},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        # FY 行の総資産と自己資本比率が carry-forward され PBR が計算できる。値は
        # 1 株当たり純資産経由と同じで、恒等式が成り立つ限り経路は答えを変えない。
        assert financial.pbr is not None
        self.assertAlmostEqual(financial.pbr, 100.0 / 120.0, places=6)
        fields = financial.bs_carry_forward_fields or ""
        self.assertIn("total_assets", fields)
        self.assertIn("equity_to_asset_ratio", fields)
        # `bps` は基準の突き合わせに使うので carry 対象のまま残る。
        self.assertIn("bps", fields)
        assert financial.bs_carry_forward_lag_days is not None
        self.assertEqual(financial.bs_carry_forward_lag_days, 190)

    def test_common_equity_uses_assets_and_ratio_from_one_disclosure(self) -> None:
        """PBR の円経路は別々の資本状態の TA と EqAR を掛け合わせない。"""
        asof = date(2026, 6, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=100.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        same_state = _summary(
            "130A",
            asof - timedelta(days=100),
            fiscal_period="3Q",
            total_assets=80_000_000_000.0,
            equity_to_asset_ratio=0.6,
        )
        assets_only = replace(
            _summary(
                "130A",
                asof - timedelta(days=10),
                fiscal_period="4Q",
                total_assets=160_000_000_000.0,
                equity_to_asset_ratio=None,
            ),
            bps=None,
        )

        financial = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [same_state, assets_only]},
            edinet_by_ticker={},
        ).financials["130A"]

        # 40bn market cap / (80bn assets * 0.6 EqAR) = 0.8333。別日の
        # 160bn assets と古い 0.6 を掛けると 0.4167 へ半減する。
        self.assertAlmostEqual(financial.pbr or 0.0, 40_000_000_000 / 48_000_000_000)

    def test_common_equity_prefers_newer_bps_over_older_complete_yen_route(self) -> None:
        """同一行の円経路でも、より新しいBPSより古ければ資本状態を巻き戻さない。"""
        asof = date(2026, 6, 1)
        bars = [
            JQuantsDailyBar(
                ticker="130A",
                traded_at=asof - timedelta(days=29 - index),
                close=1_000.0,
                turnover_value=300_000_000.0,
            )
            for index in range(30)
        ]
        older_complete = replace(
            _summary(
                "130A",
                asof - timedelta(days=100),
                fiscal_period="3Q",
                total_assets=80_000_000_000.0,
                equity_to_asset_ratio=0.5,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            bps=40_000.0,
        )
        newer_bps = replace(
            _summary(
                "130A",
                asof - timedelta(days=10),
                fiscal_period="4Q",
                total_assets=160_000_000_000.0,
                equity_to_asset_ratio=None,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            bps=20_000.0,
            equity=None,
        )

        financial = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": [older_complete, newer_bps]},
            edinet_by_ticker={},
        ).financials["130A"]

        # 1bn market cap / (20,000 BPS * 1m shares) = 0.05。古い同一行の
        # 80bn assets * 0.5 EqAR を復活させると 0.025 へ半減する。
        self.assertAlmostEqual(financial.pbr or 0.0, 0.05)

    def test_split_crossing_composition_normalizes_per_share_basis(self) -> None:
        """分割を跨ぐ YoY・株数変化は、行を asof 基準へ正規化してから行う。

        1911 型の再現: 前年 Q1 (分割前基準 eps 98.65・株数 206M) → 1:3 分割
        (factor 1/3) → 通期・直近 Q1 は分割後基準。正規化なしだと eps_yoy が
        27.33/98.65 の偽減益、net_share_change_yoy が +200% の偽希薄化になる。

        trailing 倍率は円で合成するので分割 factor を必要としない。円の利益額は分割で
        動かず、1 株当たりへの換算は最後に 1 回だけ行うためである。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = []
        for index in range(400):
            traded_at = asof - timedelta(days=399 - index)
            bars.append(
                JQuantsDailyBar(
                    ticker="130A",
                    traded_at=traded_at,
                    close=1328.0,
                    turnover_value=300_000_000.0,
                    adjustment_factor=(1.0 / 3.0 if traded_at == date(2025, 6, 27) else 1.0),
                )
            )
        summaries = [
            _summary(
                "130A",
                date(2025, 4, 30),
                eps_ttm=98.65,
                fiscal_period="1Q",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 3, 31),
                shares_outstanding=206_068_168.0,
            ),
            _summary(
                "130A",
                date(2026, 2, 13),
                eps_ttm=174.13,
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                shares_outstanding=618_555_804.0,
            ),
            _summary(
                "130A",
                date(2026, 5, 7),
                eps_ttm=27.33,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=618_555_804.0,
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
        normalized_prior_q1 = 98.65 / 3.0
        expected_profit_ttm = 27.33 * 618_555_804.0 + 174.13 * 618_555_804.0 - 98.65 * 206_068_168.0
        assert financial.per_trailing is not None
        self.assertAlmostEqual(
            financial.per_trailing,
            (1328.0 * 618_555_804.0) / expected_profit_ttm,
            places=6,
        )
        # eps_yoy は正規化済み累計同士 (27.33 vs 32.88) の比較になる。
        assert financial.eps_yoy is not None
        self.assertAlmostEqual(financial.eps_yoy, 27.33 / normalized_prior_q1 - 1.0, places=4)
        # 分割は希薄化ではない: 正規化後の前年株数は 206.07M x 3 = 618.20M 相当で、
        # 変化は実際の微増資分 (+0.057%) だけになる (正規化なしだと +200% の偽希薄化)。
        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(
            financial.net_share_change_yoy,
            618_555_804.0 / (206_068_168.0 * 3.0) - 1.0,
            places=6,
        )

    def test_per_trailing_rolls_quarterly_cumulative_eps_into_ttm(self) -> None:
        """J-Quants の EPS は期中累計なので、四半期開示直後は rolling 合成で TTM に直す。

        直近が Q1 累計 (24.0) のとき、TTM = Q1 累計 + 前期通期 (114.0) - 前年 Q1 累計 (26.0)
        = 112.0。単一四半期 EPS (24.0) で割った偽の割高 PER を作らない。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 15),
                eps_ttm=26.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 3, 31),
            ),
            _summary(
                "130A",
                date(2026, 2, 6),
                eps_ttm=114.0,
                fiscal_period="FY",
                fiscal_year_end=date(2025, 12, 31),
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
            ),
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=24.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
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
        latest_close = 100.0 + 29
        expected_eps_ttm = 24.0 + 114.0 - 26.0
        assert financial.per_trailing is not None
        self.assertAlmostEqual(financial.per_trailing, latest_close / expected_eps_ttm, places=5)
        self.assertEqual(financial.ttm_quality_per_trailing.value, "exact")

    def test_ttm_composition_survives_a_share_count_change_between_periods(self) -> None:
        """株数が期をまたいで動いた会社でも trailing 収益は合成できる。

        4502 型の再現: 買収の新株発行で株数が 783M -> 961M (期中平均) -> 1,556M と動いた
        3 期。1 株当たりで合成すると各項の分母が違うため
        21.32 + 113.50 - 161.76 = -26.94 となり、黒字の会社が赤字に見えて収益 anchor を
        失う。円で合成すれば分母は 1 つで、実際の TTM 純利益が出る。
        """
        asof = date(2019, 11, 15)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        prior_same_shares, prior_fy_shares, latest_shares = (
            783_061_325.0,
            961_462_555.0,
            1_556_472_795.0,
        )
        summaries = [
            _summary(
                "130A",
                date(2018, 10, 31),
                eps_ttm=161.76,
                shares_outstanding=prior_same_shares,
                fiscal_period="2Q",
                fiscal_year_end=date(2019, 3, 31),
                period_start=date(2018, 4, 1),
                period_end=date(2018, 9, 30),
            ),
            _summary(
                "130A",
                date(2019, 5, 14),
                eps_ttm=113.5,
                shares_outstanding=prior_fy_shares,
                fiscal_period="FY",
                fiscal_year_end=date(2019, 3, 31),
                period_start=date(2018, 4, 1),
                period_end=date(2019, 3, 31),
            ),
            _summary(
                "130A",
                date(2019, 10, 31),
                eps_ttm=21.32,
                shares_outstanding=latest_shares,
                fiscal_period="2Q",
                fiscal_year_end=date(2020, 3, 31),
                period_start=date(2019, 4, 1),
                period_end=date(2019, 9, 30),
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
        self.assertLess(21.32 + 113.5 - 161.76, 0.0)
        expected_profit_ttm = (
            21.32 * latest_shares + 113.5 * prior_fy_shares - 161.76 * prior_same_shares
        )
        self.assertGreater(expected_profit_ttm, 0.0)
        assert financial.eps is not None
        self.assertAlmostEqual(financial.eps, expected_profit_ttm / latest_shares, places=6)
        latest_close = 100.0 + 29
        assert financial.per_trailing is not None
        self.assertAlmostEqual(
            financial.per_trailing,
            (latest_close * latest_shares) / expected_profit_ttm,
            places=6,
        )

    def test_disclosures_after_the_asof_date_do_not_reach_the_snapshot(self) -> None:
        """較正リプレイは過去の断面を作り直す。発表前の決算が 1 行混ざれば全部が偽になる。

        bar は `_latest_bar_on_or_before` が切るので、開示行だけ呼び出し側任せにすると
        「未発表の好決算で割安に見える」行ができる。
        """
        asof = date(2026, 3, 31)
        security = _security()
        bars = _daily_bars("130A", asof, 60)
        disclosed = _summary(
            "130A",
            date(2026, 3, 1),
            eps_ttm=10.0,
            shares_outstanding=100_000_000.0,
            treasury_shares=0.0,
            fiscal_period="FY",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2026, 3, 31),
        )
        undisclosed = replace(disclosed, disclosed_at=asof + timedelta(days=10), eps_ttm=99.0)

        def snapshot(summaries: list[JQuantsFinancialSummary]) -> FinancialSnapshot:
            return build_metrics(
                asof_date=asof,
                securities_by_ticker={"130A": security},
                bars_by_ticker={"130A": bars},
                summaries_by_ticker={"130A": summaries},
                edinet_by_ticker={},
            ).financials["130A"]

        without = snapshot([disclosed])
        with_future = snapshot([disclosed, undisclosed])
        self.assertEqual(with_future.latest_disclosed_at, date(2026, 3, 1))
        self.assertEqual(with_future.eps, without.eps)
        self.assertEqual(with_future.per_trailing, without.per_trailing)

    def test_ttm_composition_refuses_per_share_fields(self) -> None:
        """`直近累計 + 前期通期 - 前年同期間累計` は円の総額でしか成立しない。

        各項が自分の期の株数で割られていると和・差が成立せず、株数が動いた会社で黒字が
        赤字に見える。呼び出し側の誤りとして受け付けない。
        """
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                fiscal_period="FY",
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            )
        ]
        rules = load_screening_rules()
        for field in ("eps_ttm", "bps", "dps_actual_annual", "forecast_eps"):
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                _ttm_value(summaries, field, rules.ttm)
            self.assertIn("per share", str(caught.exception))
        # 円の総額は通る。
        value, quality = _ttm_value(summaries, "profit", rules.ttm)
        self.assertIsNotNone(value)
        self.assertEqual(quality, TTMQuality.EXACT)

    def _entity_scale_snapshot(self, edinet: EdinetMetricRecord) -> FinancialSnapshot:
        asof = date(2026, 7, 1)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", asof, 30)},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        date(2026, 5, 15),
                        eps_ttm=10.0,
                        shares_outstanding=100_000_000.0,
                        treasury_shares=0.0,
                        total_assets=10_000_000_000.0,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        period_start=date(2025, 4, 1),
                        period_end=date(2026, 3, 31),
                    )
                ]
            },
            edinet_by_ticker={"130A": edinet},
        )
        return result.financials["130A"]

    def test_edinet_values_are_refused_when_they_describe_another_entity(self) -> None:
        """8253 型の再現: 単体の貸借対照表を連結の時価総額と組み合わせない。

        抽出器が 1 つの書類を単体基準で読むと、負債・現金・EBITDA が親会社単独の値に
        なる。時価総額と TTM 系列は短信由来なので、比率の分子と分母が別の会社を指す。
        実データでは総資産が 1/1000 になる行がある。両側が総資産を持つので照合できる。
        """
        financial = self._entity_scale_snapshot(
            _edinet_metric_record(
                "130A", total_assets=10_000_000.0, consolidation_basis="non_consolidated"
            )
        )
        self.assertIsNone(financial.net_cash)
        self.assertIsNone(financial.net_cash_to_market_cap)
        self.assertIsNone(financial.debt)
        self.assertIsNone(financial.cash)
        self.assertIsNone(financial.ev_ebitda)
        self.assertEqual(financial.ttm_quality_ev_ebitda, TTMQuality.UNAVAILABLE)
        assert financial.edinet_failure_reasons is not None
        self.assertIn("entity_scale_mismatch", financial.edinet_failure_reasons)
        # 実体を判断するための出所は残す。落としたのは値であって記録ではない。
        self.assertEqual(financial.edinet_source_doc_id, "S100TEST")
        self.assertEqual(financial.consolidation_basis, "non_consolidated")
        # 短信由来の指標は残り、銘柄は母集団に留まる。
        self.assertIsNotNone(financial.market_cap)
        self.assertIsNotNone(financial.per_trailing)

    def test_edinet_values_survive_when_the_balance_sheets_agree(self) -> None:
        # 同じ実体を指す行は素通しする。単体基準でも、総資産が一致すれば連結財務諸表を
        # 持たない会社であって取り違えではない。
        for basis in ("consolidated", "non_consolidated"):
            with self.subTest(basis=basis):
                financial = self._entity_scale_snapshot(
                    _edinet_metric_record(
                        "130A", total_assets=10_100_000_000.0, consolidation_basis=basis
                    )
                )
                self.assertEqual(financial.debt, 300.0)
                self.assertIsNotNone(financial.net_cash)
                assert financial.edinet_failure_reasons is None or (
                    "entity_scale_mismatch" not in financial.edinet_failure_reasons
                )

    def test_an_unverifiable_non_consolidated_record_is_refused(self) -> None:
        # 総資産を持たない行は照合できない。単体基準の母集団は実測で 17.6% が桁でずれて
        # おり、どれがずれているかを他の field では言えないので答えない。連結基準は照合
        # できた全行が一致するため素通しする。
        refused = self._entity_scale_snapshot(
            _edinet_metric_record("130A", total_assets=None, consolidation_basis="non_consolidated")
        )
        self.assertIsNone(refused.debt)
        kept = self._entity_scale_snapshot(
            _edinet_metric_record("130A", total_assets=None, consolidation_basis="consolidated")
        )
        self.assertEqual(kept.debt, 300.0)

    def test_price_over_eps_equals_the_trailing_multiple(self) -> None:
        """同じ語が 2 つの値を指さない: `market_price_yen / eps` は `per_trailing` に一致する。

        eps は時価総額と同じ資本分母 (発行済 - 自己株) で組み直した値なので、この等式は
        自己株式を積み上げた会社でも成立する。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=40.0,
                shares_outstanding=100_000_000.0,
                treasury_shares=30_000_000.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            )
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        assert financial.eps is not None
        assert financial.per_trailing is not None
        assert financial.market_price_yen is not None
        assert financial.market_cap is not None
        self.assertAlmostEqual(
            financial.market_price_yen / financial.eps, financial.per_trailing, places=9
        )
        # 報告 1 株当たり当期純利益は期中平均株式数が分母なので、期末の資本分母で組み直した
        # eps とは一致しなくてよい。倍率の分母は時価総額と揃っている側である。
        self.assertAlmostEqual(financial.eps, 40.0, places=9)

    def test_accruals_use_the_reported_profit_line_not_a_share_count_product(self) -> None:
        """accruals の純利益は報告値。1 株当たり x 発行済株数で作らない。

        自己株式を 30% 積み上げた会社では、発行済株数を掛けた再構成が純利益を 1.43 倍に
        し、accruals が 0.04 から 0.09 へ動く。判定はこの水準で行うので、再構成の誤差だけ
        で earnings-quality の結論が変わる。
        """
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=10.0,
                shares_outstanding=100_000_000.0,
                treasury_shares=30_000_000.0,
                cfo=100_000_000.0,
                total_assets=10_000_000_000.0,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
            )
        ]
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": security},
            bars_by_ticker={"130A": bars},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        financial = result.financials["130A"]
        reported_profit = 10.0 * (100_000_000.0 - 30_000_000.0)
        assert financial.accruals_to_assets is not None
        self.assertAlmostEqual(
            financial.accruals_to_assets,
            (reported_profit - 100_000_000.0) / 10_000_000_000.0,
            places=9,
        )
        reconstructed = 10.0 * 100_000_000.0
        self.assertNotAlmostEqual(
            financial.accruals_to_assets,
            (reconstructed - 100_000_000.0) / 10_000_000_000.0,
            places=3,
        )

    def test_per_trailing_is_null_when_ttm_composition_unavailable(self) -> None:
        """前期通期・前年同期間が無く合成できないときは per_trailing を出さない。"""
        asof = date(2026, 7, 1)
        security = _security()
        bars = _daily_bars("130A", asof, 30)
        summaries = [
            _summary(
                "130A",
                date(2026, 5, 15),
                eps_ttm=24.0,
                fiscal_period="1Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
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
        self.assertIsNone(financial.per_trailing)
        self.assertEqual(financial.ttm_quality_per_trailing.value, "unavailable")

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

    def test_build_metrics_adds_investment_securities_to_net_cash_ratio(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": [JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0)]},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=10.0,
                    )
                ]
            },
            edinet_by_ticker={"130A": _edinet_metric_record(investment_securities=800.0)},
        )

        snapshot = result.financials["130A"]
        self.assertEqual(snapshot.investment_securities, 800.0)
        self.assertAlmostEqual(snapshot.net_cash_to_market_cap or 0.0, -200.0 / 1200.0)
        self.assertAlmostEqual(snapshot.asset_backed_ratio or 0.0, 600.0 / 1200.0)

    def test_asset_backed_ratio_is_null_without_positive_market_cap(self) -> None:
        asof = date(2026, 4, 24)
        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": [JQuantsDailyBar("130A", asof, 120.0, 300_000_000.0)]},
            summaries_by_ticker={
                "130A": [
                    _summary(
                        "130A",
                        asof,
                        fiscal_period="FY",
                        fiscal_year_end=date(2026, 3, 31),
                        shares_outstanding=0.0,
                    )
                ]
            },
            edinet_by_ticker={"130A": _edinet_metric_record(investment_securities=800.0)},
        )

        self.assertIsNone(result.financials["130A"].asset_backed_ratio)

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
        # 分割前 70 sessions は raw=100。adjustment_close はわざと stale
        # (raw のまま = incremental cache の未遡及 row) にして無視されることを検証。
        for i in range(70):
            bars.append(
                JQuantsDailyBar(
                    "130A",
                    asof - timedelta(days=70 - i),
                    close=100.0,
                    turnover_value=300_000_000.0,
                    adjustment_close=100.0,
                )
            )
        # 権利落ち日 (= asof) の bar に factor 0.5。close は分割後 50。
        bars.append(
            JQuantsDailyBar(
                "130A",
                asof,
                close=50.0,
                turnover_value=300_000_000.0,
                adjustment_close=50.0,
                adjustment_factor=0.5,
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

    def test_build_metrics_historical_ev_ebitda_normalizes_split_via_factor(self) -> None:
        # 1:2 分割 (権利落ち = asof、factor 0.5) を跨ぐ価格履歴。調整は不変イベントの
        # adjustment_factor から組む。adjustment_close は incremental cache で
        # 遡及の有無が混在し series にならないため、わざと stale な値 (raw close の
        # まま) を入れて「無視されること」を検証する。
        # 正規化後の履歴 = [160x0.5, 200x0.5, 120] = [80, 100, 120]。
        asof = date(2026, 4, 24)
        bars = [
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=2),
                close=160.0,
                turnover_value=300_000_000.0,
                adjustment_close=160.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof - timedelta(days=1),
                close=200.0,
                turnover_value=300_000_000.0,
                adjustment_close=200.0,
            ),
            JQuantsDailyBar(
                "130A",
                asof,
                close=120.0,
                turnover_value=300_000_000.0,
                adjustment_close=120.0,
                adjustment_factor=0.5,
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

        from baibai_engine.screening.metrics import build_metrics
        from baibai_engine.screening.providers.jquants import (
            JQuantsDailyBar,
            JQuantsFinancialSummary,
        )
        from baibai_engine.screening.schema import SecurityMaster

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
            shares = 1e8
            return JQuantsFinancialSummary(
                ticker=code,
                disclosed_at=_date(2026, 2, 1),
                eps_ttm=eps,
                profit=eps * shares,
                shares_outstanding=shares,
                treasury_shares=0.0,
                type_of_current_period="FY",
                period_start=_date(2025, 1, 1),
                period_end=_date(2025, 12, 31),
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

    def _two_sector_metrics(self) -> MetricBuildResult:
        """A thick sector at PER 10 and a thin one at PER 4, priced by the same close.

        The market median is 10 because the thick sector outnumbers the thin one, so the
        thin sector's own median (4) and the market's (10) disagree. Whichever baseline
        answers is then readable from the gap alone, and the basis label has to agree
        with it.
        """
        from datetime import date as _date
        from datetime import timedelta

        from baibai_engine.screening.metrics import build_metrics
        from baibai_engine.screening.providers.jquants import (
            JQuantsDailyBar,
            JQuantsFinancialSummary,
        )
        from baibai_engine.screening.schema import SecurityMaster

        asof = _date(2026, 4, 24)
        shares = 1e8

        def bars(code: str) -> list[JQuantsDailyBar]:
            return [
                JQuantsDailyBar(
                    ticker=code,
                    traded_at=asof - timedelta(days=30 - index),
                    close=100.0,
                    turnover_value=2.0e8,
                )
                for index in range(30)
            ]

        def summary(code: str, eps: float) -> JQuantsFinancialSummary:
            return JQuantsFinancialSummary(
                ticker=code,
                disclosed_at=_date(2026, 2, 1),
                eps_ttm=eps,
                profit=eps * shares,
                shares_outstanding=shares,
                treasury_shares=0.0,
                type_of_current_period="FY",
                period_start=_date(2025, 1, 1),
                period_end=_date(2025, 12, 31),
            )

        thick = {f"11{index:02d}": "機械" for index in range(12)}
        thin = {f"22{index:02d}": "海運業" for index in range(3)}
        securities = {
            code: SecurityMaster(
                code=code,
                name=f"name-{code}",
                market_segment="プライム",
                sector_33=sector,
                is_common_stock=True,
            )
            for code, sector in (thick | thin).items()
        }
        summaries = {
            code: [summary(code, eps=10.0 if code in thick else 25.0)] for code in securities
        }
        return build_metrics(
            asof_date=asof,
            securities_by_ticker=securities,
            bars_by_ticker={code: bars(code) for code in securities},
            summaries_by_ticker=summaries,
            edinet_by_ticker={},
            median_population=frozenset(securities),
        )

    def test_a_sector_above_the_floor_is_compared_against_itself(self) -> None:
        result = self._two_sector_metrics()
        derived = result.derived["1100"]
        self.assertEqual(derived.sector_median_basis["per_trailing"], "sector")
        self.assertAlmostEqual(derived.sector_median_value["per_trailing"], 10.0, places=6)
        # Its own sector answers, so a name at the sector's own multiple has no gap.
        self.assertAlmostEqual(derived.sector_median_gap["per_trailing"], 0.0, places=6)

    def test_an_axis_with_no_baseline_at_all_records_no_basis(self) -> None:
        # EV/EBITDA needs an EDINET figure the fixture does not supply, so neither the
        # sector nor the market can answer. Writing "market" there would put every row of
        # a market-wide blank axis alongside the rows that genuinely fell through, and the
        # two are not the same observation.
        result = self._two_sector_metrics()
        derived = result.derived["1100"]
        self.assertIsNone(derived.sector_median_value["ev_ebitda"])
        self.assertNotIn("ev_ebitda", derived.sector_median_basis)
        # The axes that did answer still carry theirs.
        self.assertIn("per_trailing", derived.sector_median_basis)

    def test_a_sector_below_the_floor_is_compared_against_the_market_and_says_so(self) -> None:
        result = self._two_sector_metrics()
        derived = result.derived["2200"]
        self.assertEqual(derived.sector_median_basis["per_trailing"], "market")
        # The market's 10, not the thin sector's own 4.
        self.assertAlmostEqual(derived.sector_median_value["per_trailing"], 10.0, places=6)
        # PER 4 against a baseline of 10 reads as 60% cheap, which is what sector
        # composition alone produces here — the label is what separates the two readings.
        self.assertAlmostEqual(derived.sector_median_gap["per_trailing"], -0.6, places=6)


def _split_bar(code: str, traded_at: date, factor: float) -> JQuantsDailyBar:
    return JQuantsDailyBar(
        ticker=code,
        traded_at=traded_at,
        close=100.0,
        turnover_value=300_000_000.0,
        adjustment_factor=factor,
    )


class DividendCarryResolverTests(unittest.TestCase):
    """carry 用配当利回りの基準解決 (予想優先・基準が確定できない年度の拒否) を検証する。"""

    def test_prefers_forecast_over_actual(self) -> None:
        # 予想 DPS があれば実績より優先し、分割後基準の予想で利回りを出す。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=100.0),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 100.0 / 1911.0, places=6)

    def test_a_withdrawn_forecast_does_not_outrank_a_newer_actual(self) -> None:
        """8798 型の再現: 無配化した会社に、取り下げ前の予想の利回りを付けない。

        2024-09 に予想 17.5 を出したあと 4 期連続赤字で無配になり、2025-11 の通期実績は
        0.0、以降の提出に予想は無い。予想を無制限に遡ると 130 円の株に 13.46%/年の carry
        が付き、reversion 上限 (5%/年) を単独で超えて E[r] 降順の最上位へ出る。
        """
        summaries = [
            _summary("8798", date(2024, 9, 18), dps_forecast_annual=17.5),
            _summary("8798", date(2025, 11, 27), dps_actual_annual=0.0),
            _summary("8798", date(2026, 2, 13)),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=130.0, asof_date=date(2026, 8, 11)
        )
        self.assertIsNone(carry.dividend_yield)
        self.assertIsNone(carry.dps_forecast_annual)
        self.assertEqual(carry.basis, "unavailable")

    def test_a_forecast_newer_than_the_actual_still_answers(self) -> None:
        # 鮮度境界は予想を弱めない。実績より後に出た予想はそのまま carry になる。
        summaries = [
            _summary("8798", date(2025, 11, 27), dps_actual_annual=0.0),
            _summary("8798", date(2026, 2, 13), dps_forecast_annual=6.0),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=130.0, asof_date=date(2026, 8, 11)
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 6.0 / 130.0, places=9)

    def test_a_forecast_answers_before_any_actual_is_disclosed(self) -> None:
        # 上書きすべき実績が 1 つも無い銘柄 (実績開示前の新規上場) は予想をそのまま使う。
        summaries = [_summary("130A", date(2026, 5, 15), dps_forecast_annual=12.0)]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=600.0, asof_date=date(2026, 8, 11)
        )
        self.assertEqual(carry.basis, "forecast_annual")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 12.0 / 600.0, places=9)

    def test_refuses_the_actual_yield_when_a_split_falls_in_the_accrual_window(self) -> None:
        # 分割が期末に重なる形。期末配当の基準日は分割の効力発生日より前なので実績 DPS は
        # 分割前基準だが、権利落ち日しか持たない store からはそれを言えない。基準が確定
        # できない年度は利回りを出さず、判別できなかった factor だけを事実として残す。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=None),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)
        self.assertIsNone(carry.dps_actual_annual)
        assert carry.split_factor is not None
        self.assertAlmostEqual(carry.split_factor, 1.0 / 3.0, places=6)

    def test_refuses_the_actual_yield_when_a_consolidation_falls_in_the_accrual_window(
        self,
    ) -> None:
        # 逆向き。併合後に開示された実績 DPS を併合 factor で膨らませると、株価に対して
        # 桁違いの利回りになる。こちらも基準を確定できないので出さない。
        summaries = [
            _summary("1491", date(2026, 5, 15), dps_actual_annual=34.0, dps_forecast_annual=None),
            _summary("1491", date(2025, 5, 15), dps_actual_annual=1.5, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1491", date(2025, 9, 29), 20.0)],
            latest_price=785.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)
        assert carry.split_factor is not None
        self.assertAlmostEqual(carry.split_factor, 20.0, places=6)

    def test_resolves_a_consolidation_that_precedes_every_payment(self) -> None:
        # 1491 の形。20:1 併合が期中に起き、当期の配当は期末 1 回だけ。基準日 2026-03-31 は
        # 併合より後なので報告値 34 は既に併合後の株式基準にある。支払ごとに換算すると
        # 掛かる調整が無く、株価と同じ基準の 34 がそのまま出る。
        summaries = [
            _summary(
                "1491",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=34.0,
                dividend_year_end=34.0,
            ),
            _summary("1491", date(2025, 5, 15), dps_actual_annual=1.5),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1491", date(2025, 9, 29), 20.0)],
            latest_price=785.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 34.0 / 785.0, places=6)
        # negative assertion: 併合 factor を掛けた 680 円で 86.6% を出さない。
        self.assertLess(carry.dividend_yield, 0.10)

    def test_resolves_a_row_the_asof_normalisation_already_rewrote(self) -> None:
        """The shape production always hands over: a row already moved to the as-of basis.

        `dps_actual_annual` is rewritten by the as-of normalisation while the payment
        details stay as disclosed, so comparing the two raw reads back the conversion
        factor rather than a missing detail. A year with any adjustment after its
        disclosure would be refused, taking its dividend growth signal with it.
        """
        summaries = [
            _summary(
                "3399",
                date(2025, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                dps_actual_annual=100.0,
                dividend_interim=40.0,
                dividend_year_end=60.0,
            ),
            _summary(
                "3399",
                date(2024, 5, 15),
                fiscal_year_end=date(2024, 3, 31),
                dps_actual_annual=80.0,
            ),
        ]
        bars = [
            # Inside the accrual window, clear of both record dates.
            _split_bar("3399", date(2024, 12, 15), 0.5),
            # After the disclosure: this is the one the normalisation folds into
            # `dps_actual_annual` and the detail sum never sees.
            _split_bar("3399", date(2025, 10, 1), 0.5),
        ]
        asof = date(2026, 8, 10)
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)
        self.assertAlmostEqual(normalized[0].dps_actual_annual or 0.0, 50.0, places=6)
        self.assertEqual(normalized[0].dividend_interim, 40.0)

        carry = _resolve_dividend_carry(normalized, bars, latest_price=1000.0, asof_date=asof)

        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        # 40 paid on the pre-2024-12-15 basis (x0.25) plus 60 on the pre-2025-10-01
        # basis (x0.5) is 40 yen at the as-of basis.
        self.assertAlmostEqual(carry.dividend_yield, 40.0 / 1000.0, places=6)

    def test_resolves_an_interim_only_payer_whose_split_came_after_the_record_date(self) -> None:
        # 4626 の形。当期の配当は中間 1 回だけで、基準日 2025-09-30 より後に 1:2 分割。
        # その支払は分割前の株数で払われたので、株価と比べるには 0.5 を掛ける。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 82.5 / 4827.0, places=6)

    def test_refuses_when_the_split_lands_beside_a_record_date(self) -> None:
        # 1909 の形。権利落ちが 2026-03-30 で期末配当の基準日 2026-03-31 の前日に並ぶ。
        # 効力発生日を持たない store からは、その配当が分割の前の株数で払われたのか後なのか
        # を決められない。中間ぶんだけ換算して足すと 63.75 になり、正の 22.5 と 3 倍近く違う。
        summaries = [
            _summary(
                "1909",
                date(2026, 5, 13),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=90.0,
                dividend_interim=35.0,
                dividend_year_end=55.0,
            ),
            _summary("1909", date(2025, 5, 13), dps_actual_annual=70.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("1909", date(2026, 3, 30), 0.25)],
            latest_price=3705.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)

    def test_refuses_when_the_paid_amount_contradicts_the_payment_detail(self) -> None:
        # 総額は円なので株式基準を持たない。支払ごとの換算と食い違うなら、どちらかが別の
        # 株式基準を見ている。どちらが正しいかを機械が決められないので答えない。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
                # 82.5 円/株になるはずのところ、総額は 165 円/株ぶんを示している。
                dividend_total_annual=165.0 * 1_000_000.0,
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
                average_shares=1_000_000.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dividend_yield)

    def test_a_broken_share_count_does_not_veto_the_payment_detail(self) -> None:
        # 8097 の形。feed が自己株式数の欄に株数そのものを入れており、自己株控除後株式数が
        # 期中平均から 2 桁ずれる。この株数で総額を割った値は照合に使えないので、支払ごとの
        # 換算をその値で否定しない。
        summaries = [
            _summary(
                "4626",
                date(2026, 4, 30),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=165.0,
                dividend_interim=165.0,
                dividend_total_annual=82.5 * 1_000_000.0,
                shares_outstanding=1_000_000.0,
                treasury_shares=990_000.0,
                average_shares=1_000_000.0,
            ),
            _summary("4626", date(2025, 4, 30), dps_actual_annual=190.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 11, 27), 0.5)],
            latest_price=4827.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "actual_record_date_resolved")
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 82.5 / 4827.0, places=6)

    def test_a_same_year_correction_does_not_collapse_the_detection_window(self) -> None:
        # 判別窓の起点は当期会計期間の開始日。前期開示日を起点にすると、同一年度の訂正開示が
        # 直前に来た行で窓が 2 日へ縮み、窓内の分割が窓の外へ出て報告値がそのまま通る。
        summaries = [
            _summary(
                "3382",
                date(2024, 4, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
                period_start=date(2023, 3, 1),
                dps_actual_annual=113.0,
                dividend_interim=56.5,
                dividend_year_end=56.5,
            ),
            _summary(
                "3382",
                date(2024, 4, 10),
                fiscal_period="FY",
                fiscal_year_end=date(2024, 2, 29),
                period_start=date(2023, 3, 1),
                dps_actual_annual=113.0,
            ),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("3382", date(2024, 2, 28), 1.0 / 3.0)],
            latest_price=2000.0,
            asof_date=date(2024, 6, 30),
        )
        self.assertNotEqual(carry.basis, "actual_reported")
        self.assertIsNone(carry.dividend_yield)

    def test_a_year_with_no_dividend_is_answered_even_across_a_split(self) -> None:
        # 0 円は何倍しても 0 円なので、無配の年度に確定すべき株式基準は無い。ここを拒否に
        # 倒すと、分割を出す無配銘柄が利回りだけでなく E[r] ごと判断面から消える。
        summaries = [
            _summary(
                "7369",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=0.0,
            ),
            _summary("7369", date(2025, 5, 15), dps_actual_annual=0.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("7369", date(2025, 10, 1), 0.5)],
            latest_price=1000.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertNotEqual(carry.basis, "unresolved_split_basis")

    def test_refuses_when_the_payment_detail_does_not_add_up_to_the_reported_year(self) -> None:
        # feed は明細を一部だけ返すことがある。調整前の合計が報告年間値と合わない行は真値の
        # 一部しか持たないので、換算しても真値の一部にしかならない。総額が無い行では総額
        # veto も効かないため、ここで止めないと過小な値が「解決済み」として出る。
        summaries = [
            _summary(
                "4626",
                date(2026, 5, 15),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=60.0,
                dividend_year_end=30.0,
            ),
            _summary("4626", date(2025, 5, 15), dps_actual_annual=50.0),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("4626", date(2025, 10, 1), 0.5)],
            latest_price=1000.0,
            asof_date=date(2026, 8, 10),
        )
        self.assertEqual(carry.basis, "unresolved_split_basis")
        self.assertIsNone(carry.dps_actual_annual)

    def test_the_share_anchor_is_normalized_with_the_share_count_it_checks(self) -> None:
        # 期中平均株式数は株数なので、asof 基準への正規化で `発行済` と同じ換算を受ける。
        # 受けないと比が factor 倍ずれ、健全性 gate が veto の最も要る
        # 母集団 (開示より後に分割がある行) でだけ静かに外れる。
        row = _summary(
            "4626",
            date(2026, 4, 30),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 3, 31),
            shares_outstanding=1_000_000.0,
            treasury_shares=0.0,
            average_shares=1_000_000.0,
        )
        (normalized,) = _normalize_summaries_to_asof_basis(
            [row],
            [_split_bar("4626", date(2026, 6, 1), 1.0 / 3.0)],
            date(2026, 8, 10),
        )
        assert normalized.shares_outstanding is not None
        assert normalized.average_shares is not None
        self.assertAlmostEqual(normalized.shares_outstanding, 3_000_000.0, places=3)
        self.assertAlmostEqual(normalized.average_shares, 3_000_000.0, places=3)
        # negative assertion: 生のまま残ると比が 3.0 になり 2.0 gate を外れる。
        self.assertAlmostEqual(
            normalized.shares_outstanding / normalized.average_shares, 1.0, places=6
        )

    def test_forecast_still_answers_when_the_actual_basis_is_unresolved(self) -> None:
        # 予想 DPS は分割を跨ぐ行で正規化が None へ落とすので、残っていれば基準が揃って
        # いる。実績側が確定できなくても carry は予想で組める。
        summaries = [
            _summary("5445", date(2026, 5, 7), dps_actual_annual=300.0, dps_forecast_annual=40.0),
            _summary("5445", date(2025, 5, 7), dps_actual_annual=375.0, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries,
            [_split_bar("5445", date(2026, 3, 30), 1.0 / 3.0)],
            latest_price=1911.0,
            asof_date=date(2026, 7, 10),
        )
        self.assertEqual(carry.basis, "forecast_annual")
        self.assertIsNone(carry.dps_actual_annual)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 40.0 / 1911.0, places=6)

    def test_uses_actual_reported_when_no_split_and_no_forecast(self) -> None:
        summaries = [
            _summary("7203", date(2026, 5, 7), dps_actual_annual=50.0, dps_forecast_annual=None),
            _summary("7203", date(2025, 5, 7), dps_actual_annual=45.0, dps_forecast_annual=None),
        ]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 7, 10)
        )
        self.assertEqual(carry.basis, "actual_reported")
        self.assertIsNone(carry.split_factor)
        assert carry.dividend_yield is not None
        self.assertAlmostEqual(carry.dividend_yield, 50.0 / 1000.0, places=6)

    def test_none_when_no_dividend_data(self) -> None:
        summaries = [_summary("9999", date(2026, 5, 7))]
        carry = _resolve_dividend_carry(
            summaries, [], latest_price=1000.0, asof_date=date(2026, 7, 10)
        )
        self.assertEqual(carry.basis, "unavailable")
        self.assertIsNone(carry.dividend_yield)


class DividendBasisRouteEquivalenceTests(unittest.TestCase):
    """screening と較正が同じ年度に同じ配当を出すことを強制する。

    判定は 2 か所に書かれている。`metrics._asof_basis_dividend` は
    `_normalize_summaries_to_asof_basis` を通った行を受け、
    `calibration.forward._asof_basis_dividend` は SQL から読んだ as-reported の行を受ける。
    **どちらも正しいが正しさの理由が逆で、片方だけを直すと較正が測る量と本番が使う量が
    別になる。** 実際 2026-08 に metrics 側だけが基準を揃えておらず、この経路を通る配当
    年度の約 9% を捨てていた。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def _both_routes(
        self,
        *,
        interim: float,
        year_end: float,
        reported: float,
        split_on: date | None,
        split_factor: float,
        asof: date,
    ) -> tuple[float | None, float | None]:
        fiscal_year_end = date(2026, 3, 31)
        disclosed_at = date(2026, 5, 15)
        bars = [self._bar(date(2025, 4, 1) + timedelta(days=index * 7)) for index in range(70)]
        if split_on is not None:
            bars.append(self._bar(split_on, split_factor))
        bars.sort(key=lambda bar: bar.traded_at)

        summaries = [
            _summary(
                "1111",
                disclosed_at,
                fiscal_period="FY",
                fiscal_year_end=fiscal_year_end,
                period_start=date(2025, 4, 1),
                dps_actual_annual=reported,
                dividend_interim=interim,
                dividend_year_end=year_end,
            )
        ]
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)
        from_metrics = _asof_basis_dividend(normalized[0], bars, asof_date=asof)

        observation = _FYDividendObservation(
            fiscal_year_end=fiscal_year_end,
            disclosed_at=disclosed_at,
            dps_actual_annual=reported,
            period_start=date(2025, 4, 1),
            payments=(None, interim, None, year_end),
        )
        from_forward = _forward_asof_basis_dividend(observation, bars, basis_date=asof)
        return from_metrics, from_forward

    def test_a_split_inside_the_year_resolves_the_same_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            split_on=date(2025, 12, 15),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNotNone(metrics_value)
        self.assertIsNotNone(forward_value)
        assert metrics_value is not None
        assert forward_value is not None
        self.assertAlmostEqual(metrics_value, forward_value, places=6)

    def test_a_split_after_the_disclosure_resolves_the_same_on_both_routes(self) -> None:
        """metrics 側だけが正規化を受ける形。ここがずれていた実際の欠陥である。"""
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            split_on=date(2026, 7, 1),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        assert metrics_value is not None
        assert forward_value is not None
        self.assertAlmostEqual(metrics_value, forward_value, places=6)

    def test_a_payment_beside_a_split_is_refused_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=60.0,
            reported=100.0,
            # 期末配当の基準日 (2026-03-31) の 1 日前。どちらの経路も答えない。
            split_on=date(2026, 3, 30),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNone(metrics_value)
        self.assertIsNone(forward_value)

    def test_incomplete_details_are_refused_on_both_routes(self) -> None:
        metrics_value, forward_value = self._both_routes(
            interim=40.0,
            year_end=10.0,
            reported=100.0,
            split_on=date(2025, 12, 15),
            split_factor=0.5,
            asof=date(2026, 8, 10),
        )
        self.assertIsNone(metrics_value)
        self.assertIsNone(forward_value)


class ShareBasisOnTheDisclosureDateTests(unittest.TestCase):
    """開示日当日に権利落ちがある行の株式基準。

    財務開示行が申告する株数の基準は期末であって開示日ではない。権利落ちが開示日当日に
    来た行は分割前基準のまま公表されるので、当日の adjustment_factor を数えないと株数だけ
    分割前・価格だけ分割後になり、時価総額が分割比のぶん過少になる。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def test_a_split_going_ex_on_the_disclosure_date_normalizes_that_row(self) -> None:
        disclosed_at = date(2026, 7, 30)
        bars = [self._bar(date(2026, 7, 29)), self._bar(disclosed_at, 0.25)]
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                shares_outstanding=700_000_000.0,
                treasury_shares=0.0,
            )
        ]

        normalized = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 7, 31))

        self.assertAlmostEqual(normalized[0].shares_outstanding or 0.0, 2_800_000_000.0, places=0)
        self.assertAlmostEqual(normalized[0].bps or 0.0, 30.0, places=6)

    def test_a_split_going_ex_after_the_disclosure_date_still_normalizes(self) -> None:
        """当日を数える変更が、翌日以降の権利落ちの扱いを変えていないこと。"""

        disclosed_at = date(2026, 7, 30)
        bars = [self._bar(disclosed_at), self._bar(date(2026, 7, 31), 0.25)]
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                shares_outstanding=700_000_000.0,
                treasury_shares=0.0,
            )
        ]

        normalized = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 7, 31))

        self.assertAlmostEqual(normalized[0].shares_outstanding or 0.0, 2_800_000_000.0, places=0)


class CarriedCommonEquityTests(unittest.TestCase):
    """円経路を採るかは突き合わせ行の中で決め、carry 後の乖離では決めない。

    乖離はほとんどが実際の資本変動なので、倍率で切ると減損や大幅増資で自己資本が動いた
    会社の新しい値を捨てて古い `bps` を採ることになり、割安側へ大きくずれる。
    """

    @staticmethod
    def _row(
        *, bps: float, shares: float, total_assets: float, ratio: float
    ) -> JQuantsFinancialSummary:
        """行内で 2 経路が一致する突き合わせ行。判定はこの行だけを見る。"""

        return replace(
            _summary(
                "1111",
                date(2026, 5, 14),
                shares_outstanding=shares,
                treasury_shares=0.0,
                total_assets=total_assets,
                equity_to_asset_ratio=ratio,
            ),
            bps=bps,
        )

    def test_the_fresher_yen_route_is_kept_when_the_row_agrees(self) -> None:
        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=2e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=2.4e8,
            equity_to_asset_ratio=0.5,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1.2e8, places=0)

    def test_a_collapsed_equity_ratio_keeps_the_fresh_value(self) -> None:
        """自己資本が実際に崩れた会社で古い `bps` へ退避すると、割安側へ大きくずれる。"""

        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=2e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=2e8,
            equity_to_asset_ratio=0.05,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1e7, places=0)

    def test_a_row_that_disagrees_uses_the_common_share_basis(self) -> None:
        """行内で 2 経路が食い違う会社は資本構成の違いなので、普通株基準の `bps` を採る。"""

        summaries = [self._row(bps=100.0, shares=1_000_000.0, total_assets=4e8, ratio=0.5)]

        equity = _common_equity_yen(
            summaries,
            total_assets=4e8,
            equity_to_asset_ratio=0.5,
            bps=100.0,
            shares_ex_treasury=1_000_000.0,
        )

        self.assertAlmostEqual(equity or 0.0, 1e8, places=0)

    def test_near_zero_ratio_uses_the_reported_rounding_interval(self) -> None:
        """EqARの3桁丸めが相対誤差を膨らませても、整合する普通株basisを拒否しない。"""

        summaries = [
            self._row(
                bps=5.34,
                shares=22_145_052.0,
                total_assets=17_860_000_000.0,
                ratio=0.007,
            )
        ]

        equity = _common_equity_yen(
            summaries,
            total_assets=17_860_000_000.0,
            equity_to_asset_ratio=-0.069,
            bps=5.34,
            shares_ex_treasury=22_145_052.0,
        )

        self.assertAlmostEqual(equity or 0.0, -1_232_340_000.0, places=0)


class DividendCrossCheckBoundaryTests(unittest.TestCase):
    """明細合計の検算は、行の正規化と同じ境界で換算しなければならない。

    片方だけが開示日当日の権利落ちを数えると、比が必ず換算係数の逆数になり、その年度を
    「明細が欠けている」として捨てる。捨てた年度は増配判定ごと消える。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def test_a_split_going_ex_on_the_disclosure_date_keeps_the_year(self) -> None:
        disclosed_at = date(2026, 5, 15)
        asof = date(2026, 5, 29)
        bars = [self._bar(date(2025, 4, 1) + timedelta(days=index * 7)) for index in range(60)]
        bars.append(self._bar(disclosed_at, 0.5))
        bars.sort(key=lambda bar: bar.traded_at)
        summaries = [
            _summary(
                "1111",
                disclosed_at,
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                dps_actual_annual=100.0,
                dividend_interim=40.0,
                dividend_year_end=60.0,
            )
        ]
        normalized = _normalize_summaries_to_asof_basis(summaries, bars, asof)

        resolved = _asof_basis_dividend(normalized[0], bars, asof_date=asof)

        self.assertIsNotNone(resolved)
        self.assertAlmostEqual(resolved or 0.0, 50.0, places=6)


class InterimSplitShareBasisTests(unittest.TestCase):
    """期末と開示日の間に権利落ちがある行の株式基準を、行ごとに決める。

    提出者によって期末基準のまま出す行と分割を遡及適用した行に割れる。取り違えると
    株数が分割比だけずれ、時価総額・倍率・株数変化がまとめて壊れる。
    """

    @staticmethod
    def _bar(traded_at: date, factor: float | None = None) -> JQuantsDailyBar:
        return JQuantsDailyBar(
            ticker="1111",
            traded_at=traded_at,
            close=1000.0,
            turnover_value=3e8,
            adjustment_factor=factor,
        )

    def _normalized_shares(self, *, reported: float, reference: float) -> float | None:
        """権利落ちを挟んで 2 行を並べ、後の行の正規化後株数を返す。"""

        bars = [
            self._bar(date(2026, 1, 30)),
            self._bar(date(2026, 4, 20), 0.5),
            self._bar(date(2026, 5, 15)),
        ]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=reference,
                treasury_shares=0.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=reported,
                treasury_shares=0.0,
            ),
        ]
        return _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 5, 29))[
            1
        ].shares_outstanding

    def test_a_row_still_on_the_period_end_basis_is_converted(self) -> None:
        """分割前の株数のまま出た行。価格だけ分割後になるので換算しないと時価総額が半分。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=1_000_000.0, reference=1_000_000.0),
            2_000_000.0,
            places=0,
        )

    def test_a_row_that_already_applied_the_split_is_left_alone(self) -> None:
        """提出者が遡及適用済みの行。もう一度掛けると株数が 2 倍になる。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=2_000_000.0, reference=1_000_000.0),
            2_000_000.0,
            places=0,
        )

    def test_a_small_issue_alongside_the_split_does_not_block_the_call(self) -> None:
        """分割と同時の数 % の増資は極を動かさない。固定幅の帯だとここで答えられなくなる。"""

        self.assertAlmostEqual(
            self._normalized_shares(reported=2_048_000.0, reference=1_000_000.0),
            2_048_000.0,
            places=0,
        )

    def test_an_earlier_action_is_applied_to_the_reference_before_classification(self) -> None:
        """連続したactionでは、比較元を現在の期末基準へ揃えてから次の基準を判定する。"""

        summaries = [
            _summary(
                "3936",
                date(2021, 8, 13),
                period_end=date(2021, 6, 30),
                shares_outstanding=1_166_592.0,
                treasury_shares=102.0,
            ),
            _summary(
                "3936",
                date(2021, 11, 10),
                period_end=date(2021, 9, 30),
                shares_outstanding=17_541_915.0,
                treasury_shares=2_130.0,
            ),
        ]
        events = [
            JQuantsAdjustmentFactorEvent("3936", date(2021, 9, 15), 0.2),
            JQuantsAdjustmentFactorEvent("3936", date(2021, 11, 1), 1.0 / 3.0),
        ]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2021, 11, 10),
        )
        resolution = _resolve_capital_basis(normalized)

        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 17_541_915.0, places=0)
        self.assertAlmostEqual(resolution.shares_ex_treasury or 0.0, 17_539_785.0, places=0)

    def test_average_shares_resolves_only_the_split_basis_of_a_large_capital_change(self) -> None:
        """AvgShはgrossの代替にせず、ShOut残差が大きい行のbasisだけを確定する。"""

        summaries = [
            _summary(
                "3350",
                date(2024, 5, 15),
                period_end=date(2024, 3, 31),
                shares_outstanding=114_692_187.0,
                treasury_shares=21_945.0,
                average_shares=114_670_334.0,
            ),
            _summary(
                "3350",
                date(2024, 8, 14),
                period_end=date(2024, 6, 30),
                shares_outstanding=181_692_187.0,
                treasury_shares=22_885.0,
                average_shares=138_793_927.0,
            ),
        ]
        events = [JQuantsAdjustmentFactorEvent("3350", date(2024, 7, 30), 10.0)]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2024, 8, 14),
        )
        resolution = _resolve_capital_basis(normalized)

        # 期末basisのShOutFYは10:1併合後へ換算するが、AvgSh自身をcapitalには使わない。
        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 18_169_218.7, places=1)
        self.assertAlmostEqual(resolution.shares_ex_treasury or 0.0, 18_166_930.2, places=1)

    def test_average_shares_can_confirm_an_already_adjusted_disclosure_basis(self) -> None:
        """AvgSh anchorが開示basisを示す行は、ShOutFYを二重換算しない。"""

        summaries = [
            _summary(
                "6628",
                date(2020, 2, 14),
                period_end=date(2019, 12, 31),
                shares_outstanding=189_869_995.0,
                treasury_shares=408_187.0,
                average_shares=150_421_187.0,
            ),
            _summary(
                "6628",
                date(2020, 7, 31),
                period_end=date(2020, 3, 31),
                shares_outstanding=54_866_334.0,
                treasury_shares=81_639.0,
                average_shares=33_700_601.0,
            ),
        ]
        events = [JQuantsAdjustmentFactorEvent("6628", date(2020, 7, 20), 5.0)]

        normalized = _normalize_summaries_to_asof_basis(
            summaries,
            events,
            date(2020, 7, 31),
        )

        self.assertAlmostEqual(normalized[1].shares_outstanding or 0.0, 54_866_334.0, places=0)

    def test_a_large_capital_change_alongside_the_split_is_refused(self) -> None:
        """どちらの仮説でも残差が大きい行は答えない。時価総額が出ないので母集団に入らない。"""

        self.assertIsNone(self._normalized_shares(reported=1_500_000.0, reference=1_000_000.0))

    def test_a_refused_row_keeps_the_yen_quantities(self) -> None:
        """円の総額は株式基準に依存しない。落とすのは株数と per-share だけ。"""

        bars = [self._bar(date(2026, 4, 20), 0.5), self._bar(date(2026, 5, 15))]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=0.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=0.0,
                total_assets=5e8,
                equity_to_asset_ratio=0.5,
            ),
        ]

        row = _normalize_summaries_to_asof_basis(summaries, bars, date(2026, 5, 29))[1]

        self.assertIsNone(row.shares_outstanding)
        self.assertIsNone(row.bps)
        self.assertEqual(row.total_assets, 5e8)
        self.assertEqual(row.equity_to_asset_ratio, 0.5)

    def test_refused_capital_row_does_not_revive_older_share_basis(self) -> None:
        """判定不能な新capital stateより前の株数はmarket capへcarryしない。"""

        asof = date(2026, 5, 29)
        bars = [self._bar(date(2026, 5, 15))]
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5)]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=100_000.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=150_000.0,
            ),
        ]

        index = build_shares_outstanding_index(
            {"1111": summaries},
            {"1111": bars},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )
        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"1111": _security("1111")},
            bars_by_ticker={"1111": bars},
            summaries_by_ticker={"1111": summaries},
            edinet_by_ticker={},
            adjustment_events_by_ticker={"1111": events},
        ).financials["1111"]

        self.assertIsNone(index["1111"])
        self.assertIsNone(snapshot.market_cap)
        self.assertEqual(snapshot.capital_basis_failure_reason, "indeterminate_share_basis")

    def test_per_share_only_refusal_does_not_block_safe_capital_carry(self) -> None:
        """株数を観測しない曖昧行は、確定済みcapital stateと競合しない。"""

        asof = date(2026, 5, 29)
        bars = [self._bar(date(2026, 5, 15))]
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5)]
        capital = _summary(
            "1111",
            date(2026, 2, 10),
            period_end=date(2025, 12, 31),
            shares_outstanding=1_000_000.0,
            treasury_shares=100_000.0,
        )
        per_share_only = replace(
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_000_000.0,
            ),
            shares_outstanding=None,
            treasury_shares=None,
            average_shares=None,
        )

        index = build_shares_outstanding_index(
            {"1111": [capital, per_share_only]},
            {"1111": bars},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )

        self.assertEqual(index["1111"], 1_800_000.0)

    def test_new_complete_capital_row_resets_indeterminate_barrier(self) -> None:
        """曖昧行後にissuedとtreasuryを再観測すれば、その状態から再開する。"""

        asof = date(2026, 6, 30)
        events = [JQuantsAdjustmentFactorEvent("1111", date(2026, 4, 20), 0.5)]
        summaries = [
            _summary(
                "1111",
                date(2026, 2, 10),
                period_end=date(2025, 12, 31),
                shares_outstanding=1_000_000.0,
                treasury_shares=100_000.0,
            ),
            _summary(
                "1111",
                date(2026, 5, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=1_500_000.0,
                treasury_shares=150_000.0,
            ),
            _summary(
                "1111",
                date(2026, 6, 15),
                period_end=date(2026, 3, 31),
                shares_outstanding=2_100_000.0,
                treasury_shares=100_000.0,
            ),
        ]

        index = build_shares_outstanding_index(
            {"1111": summaries},
            {"1111": [self._bar(asof)]},
            asof,
            adjustment_events_by_ticker={"1111": events},
        )

        self.assertEqual(index["1111"], 2_000_000.0)


class CapitalBasisStateMachineTests(unittest.TestCase):
    def test_increase_then_decrease_invalidates_old_positive_treasury(self) -> None:
        asof = date(2026, 7, 1)
        rows = [
            _summary(
                "1111",
                asof - timedelta(days=300),
                shares_outstanding=1_000.0,
                treasury_shares=100.0,
            ),
            _summary(
                "1111", asof - timedelta(days=200), shares_outstanding=1_200.0, treasury_shares=None
            ),
            _summary(
                "1111", asof - timedelta(days=20), shares_outstanding=1_100.0, treasury_shares=None
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertIsNone(result.shares_ex_treasury)
        self.assertEqual(
            result.failure_reason,
            "indeterminate_positive_treasury_after_later_issued_observation",
        )

    def test_future_summary_cannot_change_asof_share_index(self) -> None:
        asof = date(2026, 7, 1)
        current = _summary(
            "1111", asof - timedelta(days=20), shares_outstanding=1_000.0, treasury_shares=100.0
        )
        future = _summary(
            "1111", asof + timedelta(days=20), shares_outstanding=2_000.0, treasury_shares=100.0
        )

        index = build_shares_outstanding_index({"1111": [current, future]}, {"1111": []}, asof)

        self.assertEqual(index["1111"], 900.0)

    def test_same_day_average_share_revision_keeps_capital_state(self) -> None:
        day = date(2026, 7, 1)
        rows = [
            _summary(
                "1111", day, shares_outstanding=1_000.0, treasury_shares=100.0, average_shares=900.0
            ),
            _summary(
                "1111", day, shares_outstanding=1_000.0, treasury_shares=100.0, average_shares=850.0
            ),
        ]

        result = _resolve_capital_basis(rows)

        self.assertEqual(result.shares_ex_treasury, 900.0)
        self.assertIsNone(result.failure_reason)


class MissingCloseAdjustmentEventTests(unittest.TestCase):
    def test_event_without_price_normalizes_capital_and_price_series(self) -> None:
        """100:1併合日のclose欠損でも、eventを株数・価格の両経路へ適用する。"""

        asof = date(2024, 3, 29)
        event_day = date(2024, 2, 28)
        bars = [
            JQuantsDailyBar(
                "1111",
                asof - timedelta(days=89 - index),
                1.0 if asof - timedelta(days=89 - index) < event_day else 100.0,
                300_000_000.0,
            )
            for index in range(90)
            if asof - timedelta(days=89 - index) != event_day
        ]
        events = [JQuantsAdjustmentFactorEvent("1111", event_day, 100.0)]
        summary = _summary(
            "1111",
            date(2024, 1, 31),
            shares_outstanding=100_000_000.0,
            treasury_shares=0.0,
            period_end=date(2023, 12, 31),
        )

        result = build_metrics(
            asof_date=asof,
            securities_by_ticker={"1111": _security("1111")},
            bars_by_ticker={"1111": bars},
            summaries_by_ticker={"1111": [summary]},
            edinet_by_ticker={},
            adjustment_events_by_ticker={"1111": events},
        )
        financial = result.financials["1111"]
        derived = result.derived["1111"]

        self.assertEqual(financial.shares_ex_treasury, 1_000_000.0)
        self.assertEqual(financial.market_cap, 100_000_000.0)
        self.assertAlmostEqual(derived.price_change_60d or 0.0, 0.0, places=12)
        self.assertAlmostEqual(derived.gap_from_52w_low or 0.0, 0.0, places=12)
        self.assertTrue(derived.split_adjustment_flag)


class FinancialSummaryPeriodResolutionTests(unittest.TestCase):
    def test_completed_short_fiscal_year_outranks_earlier_quarter(self) -> None:
        earlier_quarter = _summary(
            "3823",
            date(2026, 1, 14),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 8, 31),
            period_start=date(2025, 9, 1),
            period_end=date(2025, 11, 30),
            sales=806_000_000.0,
            shares_outstanding=131_420_693.0,
        )
        completed_short_year = _summary(
            "3823",
            date(2026, 7, 24),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 4, 30),
            period_start=date(2025, 9, 1),
            period_end=date(2026, 4, 30),
            sales=2_332_000_000.0,
            shares_outstanding=145_416_193.0,
        )
        asof = date(2026, 8, 12)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"3823": _security("3823")},
            bars_by_ticker={"3823": _daily_bars("3823", asof, 40)},
            summaries_by_ticker={"3823": [earlier_quarter, completed_short_year]},
            edinet_by_ticker={},
        ).financials["3823"]

        self.assertEqual(snapshot.latest_disclosed_at, completed_short_year.disclosed_at)
        self.assertEqual(snapshot.sales, completed_short_year.sales)
        self.assertEqual(snapshot.shares_outstanding, completed_short_year.shares_outstanding)

    def test_partial_correction_keeps_specific_profit_from_the_same_period(self) -> None:
        statement = _summary(
            "9433",
            date(2025, 11, 6),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            operating_profit=100.0,
            profit=80.0,
        )
        partial_correction = _summary(
            "9433",
            date(2025, 11, 7),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            operating_profit=None,
            profit=70.0,
        )
        asof = date(2025, 11, 28)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9433": _security("9433")},
            bars_by_ticker={"9433": _daily_bars("9433", asof, 40)},
            summaries_by_ticker={"9433": [statement, partial_correction]},
            edinet_by_ticker={},
        ).financials["9433"]

        self.assertEqual(snapshot.operating_profit, 100.0)
        self.assertEqual(snapshot.operating_profit_source, OperatingProfitSource.OPERATING_PROFIT)

    def test_future_period_metadata_cannot_outrank_a_later_valid_quarter(self) -> None:
        malformed_q1 = _summary(
            "9565",
            date(2026, 3, 12),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 6, 30),
            period_start=date(2025, 11, 1),
            period_end=date(2026, 6, 30),
            sales=781_000_000.0,
            operating_profit=49_000_000.0,
            shares_outstanding=2_775_933.0,
        )
        valid_q2 = _summary(
            "9565",
            date(2026, 6, 12),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 10, 31),
            period_start=date(2025, 11, 1),
            period_end=date(2026, 4, 30),
            sales=1_544_000_000.0,
            operating_profit=63_000_000.0,
            shares_outstanding=2_775_933.0,
        )
        asof = date(2026, 6, 30)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9565": _security("9565")},
            bars_by_ticker={"9565": _daily_bars("9565", asof, 40)},
            summaries_by_ticker={"9565": [malformed_q1, valid_q2]},
            edinet_by_ticker={},
        ).financials["9565"]

        self.assertEqual(snapshot.latest_disclosed_at, valid_q2.disclosed_at)
        self.assertEqual(snapshot.sales, valid_q2.sales)

    def test_latest_current_period_blank_withdraws_earnings_forecast(self) -> None:
        prior = _summary(
            "9433",
            date(2025, 8, 1),
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 6, 30),
            forecast_eps=100.0,
            forecast_profit=1_000.0,
            forecast_ordinary_profit=900.0,
        )
        withdrawal = _summary(
            "9433",
            date(2025, 11, 6),
            fiscal_period="2Q",
            fiscal_year_end=date(2026, 3, 31),
            period_start=date(2025, 4, 1),
            period_end=date(2025, 9, 30),
            forecast_eps=None,
            forecast_profit=None,
            forecast_ordinary_profit=None,
        )
        asof = date(2025, 11, 28)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"9433": _security("9433")},
            bars_by_ticker={"9433": _daily_bars("9433", asof, 40)},
            summaries_by_ticker={"9433": [prior, withdrawal]},
            edinet_by_ticker={},
        ).financials["9433"]

        self.assertIsNone(snapshot.per_forward)
        self.assertFalse(snapshot.forecast_special_gain_flag)
        self.assertFalse(snapshot.forecast_full_year_loss_flag)

    def test_future_period_actual_remains_newer_than_prior_valid_year(self) -> None:
        prior = _summary(
            "3281",
            date(2025, 10, 14),
            fiscal_period="FY",
            fiscal_year_end=date(2025, 8, 31),
            period_start=date(2025, 3, 1),
            period_end=date(2025, 8, 31),
            sales=30_505_000_000.0,
            shares_outstanding=4_797_731.0,
        )
        current = _summary(
            "3281",
            date(2026, 4, 13),
            fiscal_period="FY",
            fiscal_year_end=date(2026, 8, 31),
            period_start=date(2026, 3, 1),
            period_end=date(2026, 8, 31),
            sales=28_821_000_000.0,
            shares_outstanding=4_797_731.0,
        )
        asof = date(2026, 4, 30)

        snapshot = build_metrics(
            asof_date=asof,
            securities_by_ticker={"3281": _security("3281")},
            bars_by_ticker={"3281": _daily_bars("3281", asof, 40)},
            summaries_by_ticker={"3281": [prior, current]},
            edinet_by_ticker={},
        ).financials["3281"]

        self.assertEqual(snapshot.latest_disclosed_at, current.disclosed_at)
        self.assertEqual(snapshot.sales, current.sales)


class TradableShareChangeTest(unittest.TestCase):
    """取得年と消却年で 2 つの株数軸が別々に動くことを固定する。

    日本の自社株買いは取得した株式を自己株式へ入れるだけで、発行済株式総数は消却するまで
    減らない。carry の株数項が発行済で測られている限り、還元が起きた年は 0 で、現金の動かない
    消却年に符号が付く。どちらで測るかは事前登録した比較で決めるので
    (reports/studies/2026-08-17-tradable-share-change/)、ここでは 2 軸が別物であることだけを
    固定する。
    """

    ASOF = date(2026, 6, 30)

    def _snapshot(
        self,
        *,
        prior_issued: float,
        prior_treasury: float | None,
        issued: float,
        treasury: float | None,
    ) -> FinancialSnapshot:
        summaries = [
            _summary(
                "130A",
                date(2025, 5, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
                shares_outstanding=prior_issued,
                treasury_shares=prior_treasury,
            ),
            _summary(
                "130A",
                date(2026, 5, 12),
                fiscal_period="FY",
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2026, 3, 31),
                shares_outstanding=issued,
                treasury_shares=treasury,
            ),
        ]
        result = build_metrics(
            asof_date=self.ASOF,
            securities_by_ticker={"130A": _security()},
            bars_by_ticker={"130A": _daily_bars("130A", self.ASOF, 400)},
            summaries_by_ticker={"130A": summaries},
            edinet_by_ticker={},
        )
        return result.financials["130A"]

    def test_a_buyback_year_moves_only_the_treasury_excluded_count(self) -> None:
        """自己株を 10% 積んだ年。発行済は動かないので現行の carry 項は 0 になる。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=0.0,
            issued=400_000_000.0,
            treasury=40_000_000.0,
        )

        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, 0.0, places=9)
        assert financial.tradable_share_change_yoy is not None
        self.assertAlmostEqual(financial.tradable_share_change_yoy, -0.10, places=9)

    def test_a_cancellation_year_moves_only_the_issued_count(self) -> None:
        """積んだ自己株を消却した年。現金は動かないのに現行の carry 項だけが符号を持つ。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=40_000_000.0,
            issued=360_000_000.0,
            treasury=0.0,
        )

        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, -0.10, places=9)
        assert financial.tradable_share_change_yoy is not None
        self.assertAlmostEqual(financial.tradable_share_change_yoy, 0.0, places=9)

    def test_an_unobservable_treasury_count_leaves_the_new_axis_unanswered(self) -> None:
        """自己株式数の欠損を 0 で埋めると自己株ゼロを捏造する。答えない側に倒す。"""

        financial = self._snapshot(
            prior_issued=400_000_000.0,
            prior_treasury=None,
            issued=400_000_000.0,
            treasury=40_000_000.0,
        )

        self.assertIsNone(financial.tradable_share_change_yoy)
        # 発行済側は前年行から答えられるので、旧軸は残る。
        assert financial.net_share_change_yoy is not None
        self.assertAlmostEqual(financial.net_share_change_yoy, 0.0, places=9)
