from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta

from tests.engine.screening.metric_fixtures import (
    _daily_bars,
    _security,
    _summary,
)

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.providers.edinet import EdinetMetricRecord
from baibai_engine.screening.metrics import (
    build_metrics,
)
from baibai_engine.screening.schema import FinancialSnapshot


class SnapshotFixtures(unittest.TestCase):
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

    @staticmethod
    def _ttm_actual_periods() -> list[JQuantsFinancialSummary]:
        return [
            JQuantsFinancialSummary(
                ticker="4812",
                disclosed_at=disclosure,
                fiscal_period=period,
                fiscal_year_end=date(year, 12, 31),
                period_start=date(year, 1, 1),
                period_end=end,
                sales=sales * 1_000_000.0,
                profit=profit * 1_000_000.0,
                operating_profit=profit * 1_000_000.0,
                cfo=profit * 2_000_000.0,
                shares_outstanding=200_000_000.0,
                treasury_shares=0.0,
                forecast_eps=92.22,
                dps_forecast_annual=45.0,
            )
            for disclosure, year, period, end, sales, profit in [
                (date(2025, 7, 29), 2025, "2Q", date(2025, 6, 30), 80_239, 7_684),
                (date(2026, 2, 12), 2025, "FY", date(2025, 12, 31), 164_865, 16_365),
                (date(2026, 7, 29), 2026, "2Q", date(2026, 6, 30), 88_649, 8_888),
            ]
        ]

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
