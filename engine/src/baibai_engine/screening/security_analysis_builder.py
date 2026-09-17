"""Build the Security Analysis used by production runs and calibration replay.

Both paths must assemble the same Security Analysis; otherwise calibration would
measure a different Candidate Discovery input from the production Screening Run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from .earnings_lag import EarningsLag
from .estimates import ExpectedReturnEstimate, estimate_expected_return
from .schema import (
    DerivedMetrics,
    FinancialSnapshot,
    FreshnessWarning,
    SecurityAnalysis,
    SecurityMaster,
    UniverseSnapshot,
)
from .valuation_catalysts import ValuationCatalystContext


def build_security_analysis(
    *,
    ticker: str,
    security: SecurityMaster,
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    universe_snapshot: UniverseSnapshot,
    freshness_warnings: tuple[FreshnessWarning, ...] = (),
    next_earnings_date: date | None = None,
    earnings_lag: EarningsLag | None = None,
    normalized_per_3fy: float | None = None,
    valuation_catalyst_context: ValuationCatalystContext | None = None,
) -> SecurityAnalysis:
    return SecurityAnalysis(
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
        listing_span_days=universe_snapshot.listing_span_days,
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
        metrics=build_security_analysis_metrics(
            financial,
            derived=derived,
            freshness_warning_count=len(freshness_warnings),
            estimate=estimate_expected_return(
                financial, derived, close=_close_from_snapshot(financial)
            ),
            normalized_per_3fy=normalized_per_3fy,
            earnings_lag=earnings_lag,
            valuation_catalyst_context=valuation_catalyst_context,
        ),
        next_earnings_date=next_earnings_date,
        split_adjustment_flag=derived.split_adjustment_flag,
        freshness_warnings=freshness_warnings,
    )


def build_security_analysis_metrics(
    financial: FinancialSnapshot,
    *,
    freshness_warning_count: int,
    derived: DerivedMetrics,
    estimate: ExpectedReturnEstimate | None = None,
    normalized_per_3fy: float | None = None,
    earnings_lag: EarningsLag | None = None,
    valuation_catalyst_context: ValuationCatalystContext | None = None,
) -> Mapping[str, float | int | bool | str | None]:
    return {
        # Approach-native relative valuation coordinates. These are L2 derived
        # measurements; valuation-approach nominations read them directly.
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
        "edinet_source_doc_id": financial.edinet_source_doc_id,
        "edinet_document_type": financial.edinet_document_type,
        "edinet_source_submit_datetime": financial.edinet_source_submit_datetime,
        "edinet_source_period_start": _date_iso(financial.edinet_source_period_start),
        "edinet_source_period_end": _date_iso(financial.edinet_source_period_end),
        "margin_week_end": _date_iso(derived.margin_week_end),
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
        "dividend_yield": financial.dividend_yield,
        "dividend_basis": financial.dividend_basis,
        "dividend_split_factor": financial.dividend_split_factor,
        "sales_yoy": financial.sales_yoy,
        "cfo_yoy": financial.cfo_yoy,
        "operating_profit": financial.operating_profit,
        "operating_profit_yoy": financial.operating_profit_yoy,
        "operating_profit_loss_narrowing": financial.operating_profit_loss_narrowing,
        "shares_outstanding": financial.shares_outstanding,
        # Accrual and net-share-change signals surface in Security Analysis so the
        # research layer can read them without a second cache fetch.
        "accruals_to_assets": financial.accruals_to_assets,
        "net_share_change_yoy": financial.net_share_change_yoy,
        # 3 FY平均EPSに対する現在株価の倍率。正常利益や安全性の判定ではなく、
        # Normalized Earnings Powerのeligibility/orderと利益cycle比較に使い、FV/E[r]には入れない。
        "normalized_per_3fy": normalized_per_3fy,
        # 決算開示と as-of 財務のラグ (earnings_lag.py)。annotation であり ranking・
        # gate・E[r] へ入らない。fin_latest_disclosed_date は本行の財務が含む最後の
        # 開示、stale_fin_flag は「発表済みだが取込前」の窓に居ることを示す。
        "fin_latest_disclosed_date": _date_iso(financial.latest_financial_disclosure_date),
        "next_earnings_estimated_date": (
            None if earnings_lag is None else _date_iso(earnings_lag.next_earnings_estimated_date)
        ),
        "next_earnings_status": None if earnings_lag is None else earnings_lag.next_earnings_status,
        "stale_fin_flag": None if earnings_lag is None else earnings_lag.stale_fin_flag,
        "edinet_freshness_warning_count": freshness_warning_count,
        # 機械 E[r] はannual_ratio (0.08 = 8%/年)。成分と前提は estimates.py。
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
        # 資本配分・支配権イベントの typed fact (valuation_catalysts.py)。TSE の開示状況は
        # 月次スナップショットの point-in-time 参照で、"none" は「その月の一覧に居ない」、
        # None は「参照できる月次スナップショットが無い」。イベントは対象会社側から見た
        # 直近 6 か月の提出有無で、None は観測窓が埋まっていない状態、False は窓を観測して
        # 提出が無かった状態。いずれも annotation であり ranking・gate・E[r] へは入らない。
        "tse_capital_policy_status": (
            None
            if valuation_catalyst_context is None
            else valuation_catalyst_context.tse_capital_policy_status
        ),
        "tse_capital_policy_updated_on": (
            None
            if valuation_catalyst_context is None
            else _date_iso(valuation_catalyst_context.tse_capital_policy_updated_on)
        ),
        "large_holding_event_recent": (
            None
            if valuation_catalyst_context is None
            else valuation_catalyst_context.large_holding_filing_within_lookback
        ),
        "large_holding_event_latest_on": (
            None
            if valuation_catalyst_context is None
            else _date_iso(valuation_catalyst_context.latest_large_holding_filing_date)
        ),
        "tender_offer_event_recent": (
            None
            if valuation_catalyst_context is None
            else valuation_catalyst_context.tender_offer_filing_within_lookback
        ),
        "tender_offer_event_latest_on": (
            None
            if valuation_catalyst_context is None
            else _date_iso(valuation_catalyst_context.latest_tender_offer_filing_date)
        ),
    }


def _close_from_snapshot(financial: FinancialSnapshot) -> float | None:
    return financial.market_price_yen


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
