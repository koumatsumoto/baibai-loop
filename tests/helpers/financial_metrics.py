"""財務・株式基準の共有合成fixture。現在の財務計算へ観測を与える。"""

from __future__ import annotations

from datetime import date
from enum import Enum

from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)


class _DefaultProfit(Enum):
    FROM_EPS = "from_eps"


def _summary(
    code: str,
    disclosed_at: date,
    *,
    cumulative_eps: float | None = 18.0,
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
    profit: float | _DefaultProfit | None = _DefaultProfit.FROM_EPS,
    total_assets: float | None = None,
    equity_to_asset_ratio: float | None = None,
) -> JQuantsFinancialSummary:
    # fixture では株数一定の簡略条件として EPS x 期末自己株控除後株数で利益を作る。
    # 実開示の EPS 分母は期中平均株数などであり、一般の恒等式ではない。
    # 省略時だけ生成する。明示した None は利益未観測のまま保持する。
    if isinstance(profit, _DefaultProfit):
        profit = (
            cumulative_eps * (shares_outstanding - (treasury_shares or 0.0))
            if cumulative_eps is not None
            else None
        )
    # Calculation-ready fixture; native document/period resolution has separate tests.
    return JQuantsFinancialSummary(
        ticker=code,
        disclosed_at=disclosed_at,
        forecast_eps=forecast_eps,
        forecast_status="confirmed",
        dividend_forecast_status="confirmed" if dps_forecast_annual is not None else "missing",
        cumulative_eps=cumulative_eps,
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
        document_type=(
            f"{fiscal_period}FinancialStatements_Consolidated_JP"
            if fiscal_period in {"1Q", "2Q", "3Q", "FY"}
            else None
        ),
    )
