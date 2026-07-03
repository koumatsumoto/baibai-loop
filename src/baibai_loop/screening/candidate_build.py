"""Shared ScreenedCandidate assembly for the screen run and the calibration replay.

candidates YAML (本番 run) と較正リプレイ (calibration) が同一の候補行を組み立てる
ための単一実装。ここが分岐すると「リプレイで測った select 順」と「本番の select 順」が
静かにずれるため、候補行の組み立ては本 module に集約する。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from .schema import (
    DerivedMetrics,
    EvidenceHit,
    FinancialSnapshot,
    FreshnessWarning,
    ScreenedCandidate,
    SecurityMaster,
    UniverseSnapshot,
)


def build_screened_candidate(
    *,
    ticker: str,
    security: SecurityMaster,
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    universe_snapshot: UniverseSnapshot,
    evidence_hits: tuple[EvidenceHit, ...],
    freshness_warnings: tuple[FreshnessWarning, ...] = (),
    next_earnings_date: date | None = None,
) -> ScreenedCandidate:
    return ScreenedCandidate(
        ticker=ticker,
        name=security.name,
        per_forward=financial.per_forward,
        per_trailing=financial.per_trailing,
        pbr=financial.pbr,
        ev_ebitda=financial.ev_ebitda,
        p_s=financial.p_s,
        pcfr=financial.pcfr,
        sector_33=security.sector_33,
        evidence_hits=evidence_hits,
        ttm_quality={
            "ev_ebitda": financial.ttm_quality_ev_ebitda,
            "per_trailing": financial.ttm_quality_per_trailing,
            "p_s": financial.ttm_quality_p_s,
            "pcfr": financial.ttm_quality_pcfr,
            "ocf_yield": financial.ttm_quality_ocf_yield,
            "sales": financial.ttm_quality_sales,
            "fcf_yield": financial.ttm_quality_fcf_yield,
            "net_cash": financial.ttm_quality_net_cash,
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
        metrics=candidate_metrics_map(financial, freshness_warning_count=len(freshness_warnings)),
        next_earnings_date=next_earnings_date,
        split_adjustment_flag=derived.split_adjustment_flag,
        freshness_warnings=freshness_warnings,
    )


def candidate_metrics_map(
    financial: FinancialSnapshot,
    *,
    freshness_warning_count: int,
) -> Mapping[str, float | int | bool | str | None]:
    return {
        "sales_ttm": financial.sales_ttm,
        "ocf_ttm": financial.ocf_ttm,
        "edinet_ocf_ttm": financial.edinet_ocf_ttm,
        "cash_eq": financial.cash_eq,
        "total_assets": financial.total_assets,
        "equity": financial.equity,
        "cash_to_market_cap": financial.cash_to_market_cap,
        "price_to_equity": financial.price_to_equity,
        "equity_ratio": financial.equity_ratio,
        "ocf_yield": financial.ocf_yield,
        "net_cash": financial.net_cash,
        "net_cash_to_market_cap": financial.net_cash_to_market_cap,
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
        "edinet_capex_source": financial.edinet_capex_source,
        "edinet_failure_reasons": financial.edinet_failure_reasons,
        "bs_carry_forward_fields": financial.bs_carry_forward_fields,
        "bs_carry_forward_lag_days": financial.bs_carry_forward_lag_days,
        "dps_actual_annual": financial.dps_actual_annual,
        "dps_forecast_annual": financial.dps_forecast_annual,
        "dividend_yield": financial.dividend_yield,
        "sales_yoy": financial.sales_yoy,
        "cfo_yoy": financial.cfo_yoy,
        "operating_profit": financial.operating_profit,
        "operating_profit_yoy": financial.operating_profit_yoy,
        "operating_profit_loss_narrowing": financial.operating_profit_loss_narrowing,
        "shares_outstanding": financial.shares_outstanding,
        # D2 / D3 academic signals — surface in candidate metrics so the
        # research layer can read them without a second cache fetch.
        "accruals_to_assets": financial.accruals_to_assets,
        "net_share_change_yoy": financial.net_share_change_yoy,
        "edinet_freshness_warning_count": freshness_warning_count,
    }


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
