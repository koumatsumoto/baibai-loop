from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.providers.edinet import EdinetMetricRecord
from baibai_engine.screening.schema import TTMQuality

ROOT = Path(__file__).resolve().parents[2]


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
        ticker=code,
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


def _split_bar(code: str, traded_at: date, factor: float) -> JQuantsDailyBar:
    return JQuantsDailyBar(
        ticker=code,
        traded_at=traded_at,
        close=100.0,
        turnover_value=300_000_000.0,
        adjustment_factor=factor,
    )


if __name__ == "__main__":
    unittest.main()
