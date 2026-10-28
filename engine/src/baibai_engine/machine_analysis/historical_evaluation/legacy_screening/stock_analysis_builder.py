"""旧run契約のStock Analysisを較正replayへ組み立てる。新候補の生成には使わない。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date

from baibai_engine.machine_analysis.historical_evaluation.estimates import (
    ExpectedReturnEstimate,
    ExpectedReturnInputs,
    estimate_expected_return,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    UNRESOLVED_ACTUAL_DIVIDEND_BASIS,
    UNRESOLVED_DIVIDEND_BASIS,
    FinancialSnapshot,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.schema import (
    DerivedMetrics,
    StockAnalysis,
    UniverseSnapshot,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster


def build_stock_analysis(
    *,
    ticker: str,
    security: SecurityMaster,
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    universe_snapshot: UniverseSnapshot,
    normalized_per_3fy: float | None = None,
) -> StockAnalysis:
    return StockAnalysis(
        ticker=ticker,
        name=security.name,
        per_forward=financial.per_forward,
        per_trailing=financial.per_trailing,
        pbr=financial.pbr,
        ev_ebitda=financial.ev_ebitda,
        p_s=financial.p_s,
        pcfr=financial.pcfr,
        sector_33=security.sector_33,
        ttm_quality={
            key: quality
            for key, quality in {
                "ev_ebitda": financial.ttm_quality_ev_ebitda,
                "per_trailing": financial.ttm_quality_per_trailing,
                "p_s": financial.ttm_quality_p_s,
                "pcfr": financial.ttm_quality_pcfr,
                "ocf_yield": financial.ttm_quality_ocf_yield,
                "sales": financial.ttm_quality_sales,
                "fcf_yield": financial.ttm_quality_fcf_yield,
                "net_cash": financial.ttm_quality_net_cash,
            }.items()
            if quality is not None
        },
        market_cap_oku=universe_snapshot.market_cap_oku,
        avg_turnover_oku=universe_snapshot.avg_turnover_oku,
        price_history_span_days=universe_snapshot.price_history_span_days,
        jpx_flags=universe_snapshot.jpx_flags,
        price_change_1d=derived.price_change_1d,
        price_change_5d=derived.price_change_5d,
        price_change_20d=derived.price_change_20d,
        price_change_60d=derived.price_change_60d,
        gap_from_52w_low=derived.gap_from_52w_low,
        turnover_spike_5d=derived.turnover_spike_5d,
        sector_relative_strength_percentile=derived.sector_relative_strength_percentile,
        price_history_sessions_750d=derived.price_history_sessions_750d,
        price_history_coverage_750d=derived.price_history_coverage_750d,
        metrics=build_stock_analysis_metrics(
            financial,
            derived=derived,
            estimate=estimate_expected_return(expected_return_inputs(financial, derived)),
            normalized_per_3fy=normalized_per_3fy,
        ),
    )


def build_stock_analysis_metrics(
    financial: FinancialSnapshot,
    *,
    derived: DerivedMetrics,
    estimate: ExpectedReturnEstimate | None = None,
    normalized_per_3fy: float | None = None,
) -> Mapping[str, float | int | bool | str | None]:
    return {
        # Approach-native relative valuation coordinates. These are L2 derived
        # measurements; candidate valuation approaches read them directly.
        "per_forward_sector_gap": derived.sector_median_gap.get("per_forward"),
        "per_trailing_sector_gap": derived.sector_median_gap.get("per_trailing"),
        "pbr_sector_gap": derived.sector_median_gap.get("pbr"),
        "p_s_sector_gap": derived.sector_median_gap.get("p_s"),
        "per_forward_sector_median_basis": derived.sector_median_basis.get("per_forward"),
        "per_trailing_sector_median_basis": derived.sector_median_basis.get("per_trailing"),
        "pbr_sector_median_basis": derived.sector_median_basis.get("pbr"),
        "p_s_sector_median_basis": derived.sector_median_basis.get("p_s"),
        "sales_ttm": financial.sales_ttm,
        "ocf_ttm": financial.ocf_ttm,
        "edinet_ocf_ttm": financial.edinet_ocf_ttm,
        "cash_eq": financial.cash_eq,
        "total_assets": financial.total_assets,
        "same_state_equity_yen": financial.same_state_equity_yen,
        "market_price_yen": financial.market_price_yen,
        "capital_basis_failure_reason": financial.capital_basis_failure_reason,
        "cash_to_market_cap": financial.cash_to_market_cap,
        "equity_ratio": financial.equity_ratio,
        "ocf_yield": financial.ocf_yield,
        "net_cash": financial.net_cash,
        "net_cash_to_market_cap": financial.net_cash_to_market_cap,
        "investment_securities": financial.investment_securities,
        "asset_backed_ratio": financial.asset_backed_ratio,
        "debt": financial.debt,
        "cash": financial.cash,
        "fcf_ttm": financial.fcf_ttm,
        "fcf_yield": financial.fcf_yield,
        "capex_ttm": financial.capex_ttm,
        "depreciation_and_amortization_ttm": financial.depreciation_and_amortization_ttm,
        "edinet_snapshot_asof": _date_iso(financial.edinet_snapshot_asof),
        "edinet_snapshot_recorded_at_utc": financial.edinet_snapshot_recorded_at_utc,
        "edinet_source_doc_id": financial.edinet_source_doc_id,
        "edinet_document_type": financial.edinet_document_type,
        "edinet_source_submit_datetime": financial.edinet_source_submit_datetime,
        "edinet_source_period_start": _date_iso(financial.edinet_source_period_start),
        "edinet_source_period_end": _date_iso(financial.edinet_source_period_end),
        "margin_balance_date": _date_iso(derived.margin_balance_date),
        "margin_issue_type": derived.margin_issue_type,
        "margin_long_to_adv": derived.margin_long_to_adv,
        "margin_short_to_adv": derived.margin_short_to_adv,
        "margin_long_share": derived.margin_long_share,
        "margin_long_delta_26w": derived.margin_long_delta_26w,
        "margin_std_long_share": derived.margin_std_long_share,
        "edinet_capex_source": financial.edinet_capex_source,
        "edinet_failure_reasons": financial.edinet_failure_reasons,
        "bs_carry_forward_fields": financial.bs_carry_forward_fields,
        "bs_carry_forward_lag_days": financial.bs_carry_forward_lag_days,
        "forecast_special_gain_flag": financial.forecast_special_gain_flag,
        "forecast_full_year_loss_flag": financial.forecast_full_year_loss_flag,
        "dps_actual_annual": financial.dps_actual_annual,
        "dps_forecast_annual": financial.dps_forecast_annual,
        "earnings_forecast_source": json.dumps(
            financial.earnings_forecast_source, ensure_ascii=False
        )
        if financial.earnings_forecast_source
        else None,
        "dividend_forecast_source": json.dumps(
            financial.dividend_forecast_source, ensure_ascii=False
        )
        if financial.dividend_forecast_source
        else None,
        "dividend_yield": financial.dividend_yield,
        "dividend_basis": financial.dividend_basis,
        "dividend_split_factor": financial.dividend_split_factor,
        "sales_yoy": financial.sales_yoy,
        "cfo_yoy": financial.cfo_yoy,
        "operating_profit": financial.operating_profit,
        "operating_profit_source": financial.operating_profit_source.value,
        "operating_profit_ttm": financial.operating_profit_ttm,
        "operating_profit_yoy": financial.operating_profit_yoy,
        "operating_profit_loss_narrowing": financial.operating_profit_loss_narrowing,
        "shares_outstanding": financial.shares_outstanding,
        # Accrual and net-share-change signals surface in Stock Analysis so the
        # research layer can read them without a second cache fetch.
        "accruals_to_assets": financial.accruals_to_assets,
        "net_share_change_yoy": financial.net_share_change_yoy,
        # 3 FY平均EPSに対する現在株価の倍率。正常利益や安全性の判定ではなく、
        # Normalized Earnings Powerのeligibility/orderと利益cycle比較に使い、FV/E[r]には入れない。
        "normalized_per_3fy": normalized_per_3fy,
        "fin_latest_disclosed_date": _date_iso(financial.latest_financial_disclosure_date),
        # 機械 E[r] はannual_ratio (0.08 = 8%/年)。成分と前提は valuation/estimates.py。
        "er_annual": estimate.er_annual if estimate else None,
        "er_reversion_annual": estimate.reversion_annual if estimate else None,
        "er_carry_annual": estimate.carry_annual if estimate else None,
        "er_dividend_yield": estimate.dividend_yield if estimate else None,
        "er_upside_capped": estimate.upside_capped if estimate else None,
        "er_anchor_metrics": estimate.anchor_metrics if estimate else None,
        "fv_sector_median_yen": estimate.fv_sector_median_yen if estimate else None,
        "fv_self_range_yen": estimate.fv_self_range_yen if estimate else None,
        "er_origin": estimate.origin if estimate else None,
        "er_model_version": estimate.model_version if estimate else None,
        "er_unit": estimate.unit if estimate else None,
        "er_assumptions": estimate.assumptions if estimate else None,
    }


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def expected_return_inputs(
    financial: FinancialSnapshot, derived: DerivedMetrics
) -> ExpectedReturnInputs:
    """Screening の大きな snapshot から Valuation の計算入力へ変換する。"""
    return ExpectedReturnInputs(
        pbr=financial.pbr,
        per_forward=financial.per_forward,
        per_trailing=financial.per_trailing,
        sector_median_value=derived.sector_median_value,
        self_range_median=derived.self_range_median,
        dividend_yield=financial.dividend_yield,
        net_share_change_yoy=financial.net_share_change_yoy,
        dividend_basis_resolved=financial.dividend_basis
        not in {UNRESOLVED_DIVIDEND_BASIS, UNRESOLVED_ACTUAL_DIVIDEND_BASIS},
        close=financial.market_price_yen,
    )
