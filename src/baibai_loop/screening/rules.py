from __future__ import annotations

from .rule_config import (
    CashflowYieldLane,
    CashRichLane,
    SalesDiscountGrowthLane,
    ScreeningRules,
    ValuationReversionLane,
)
from .schema import DerivedMetrics, FinancialSnapshot, ScreeningResult, SignalHit, TTMQuality

SIGNAL_VALUATION_REVERSION = "valuation-reversion"
SIGNAL_CASH_RICH = "cash-rich-asset-discount"
SIGNAL_CASHFLOW_YIELD = "cashflow-yield-discount"
SIGNAL_SALES_DISCOUNT = "sales-discount-growth"

REASON_SECTOR_SELF_RANGE = "sector_median_discount_and_self_range_bottom"
REASON_PRICE_SIGMA = "price_down_60d_and_valuation_sigma_down"
REASON_SECTOR_ROTATION = "sector_rotation_short_sell"
REASON_CASH_RICH = "cash_to_market_cap_and_price_to_equity"
REASON_CASHFLOW_YIELD = "ocf_yield_discount"
REASON_SALES_DISCOUNT = "ps_discount_with_sales_growth"


def evaluate_screening(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    rules: ScreeningRules,
) -> ScreeningResult:
    signals: list[SignalHit] = []
    null_reasons: list[str] = []

    for name, lane in rules.signal_lanes.items():
        match name:
            case "valuation-reversion":
                if not isinstance(lane, ValuationReversionLane):
                    continue
                hit = _valuation_reversion(
                    financial,
                    derived,
                    lane,
                    rules.quality.yoy_deterioration_threshold,
                    null_reasons,
                )
            case "cash-rich-asset-discount":
                if not isinstance(lane, CashRichLane):
                    continue
                hit = _cash_rich_asset_discount(financial, lane, null_reasons)
            case "cashflow-yield-discount":
                if not isinstance(lane, CashflowYieldLane):
                    continue
                hit = _cashflow_yield_discount(financial, lane, null_reasons)
            case "sales-discount-growth":
                if not isinstance(lane, SalesDiscountGrowthLane):
                    continue
                hit = _sales_discount_growth(financial, derived, lane, null_reasons)
            case _:  # pragma: no cover - config validator rejects this.
                hit = None
        if hit is not None:
            signals.append(hit)

    if not signals:
        return ScreeningResult(
            pass_fail=False,
            failure_reasons=("no_signal_hit",),
            null_reasons=tuple(dict.fromkeys(null_reasons)),
        )
    return ScreeningResult(
        pass_fail=True,
        signals=tuple(signals),
        null_reasons=tuple(dict.fromkeys(null_reasons)),
    )


def _valuation_reversion(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    lane: ValuationReversionLane,
    deterioration_threshold: float,
    null_reasons: list[str],
) -> SignalHit | None:
    reasons: list[str] = []
    metrics: dict[str, float | int | bool | str | None] = {}
    hit_metric_a = _condition_a_metric(financial, derived, lane, null_reasons)
    if hit_metric_a is not None:
        reasons.append(REASON_SECTOR_SELF_RANGE)
        metrics["condition_a_metric"] = hit_metric_a
        metrics["condition_a_sector_median_gap"] = derived.sector_median_gap.get(hit_metric_a)
        metrics["condition_a_self_range_percentile"] = derived.self_range_percentile.get(
            hit_metric_a
        )
    hit_metric_b = _condition_b_metric(
        financial,
        derived,
        lane,
        deterioration_threshold,
        null_reasons,
    )
    if hit_metric_b is not None:
        reasons.append(REASON_PRICE_SIGMA)
        metrics["condition_b_metric"] = hit_metric_b
        metrics["price_change_60d"] = derived.price_change_60d
        metrics["condition_b_sigma_gap"] = derived.sigma_gap.get(hit_metric_b)
    if _condition_c(financial, derived, lane, deterioration_threshold, null_reasons):
        reasons.append(REASON_SECTOR_ROTATION)
        metrics["sector_relative_strength_percentile"] = derived.sector_relative_strength_percentile
        metrics["ticker_return_4w"] = derived.ticker_return_4w
        metrics["sector_return_4w"] = derived.sector_return_4w
    if not reasons:
        return None
    return SignalHit(
        name=SIGNAL_VALUATION_REVERSION,
        playbook=lane.playbook,
        reasons=tuple(reasons),
        metrics=metrics,
    )


def _condition_a_metric(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    lane: ValuationReversionLane,
    null_reasons: list[str],
) -> str | None:
    if derived.short_history_flag:
        null_reasons.append("valuation_reversion_condition_a_short_history")
        return None
    for metric in _rule_metrics(financial, lane):
        sector_gap = derived.sector_median_gap.get(metric)
        self_percentile = derived.self_range_percentile.get(metric)
        if sector_gap is None or self_percentile is None:
            continue
        if (
            sector_gap <= lane.sector_median_gap_max
            and self_percentile <= lane.self_range_percentile_max
        ):
            return metric
    null_reasons.append("valuation_reversion_condition_a_no_metric")
    return None


def _condition_b_metric(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    lane: ValuationReversionLane,
    deterioration_threshold: float,
    null_reasons: list[str],
) -> str | None:
    if derived.price_change_60d is None:
        null_reasons.append("valuation_reversion_condition_b_missing_price_change_60d")
        return None
    if derived.price_change_60d > lane.price_change_60d_max:
        return None
    if _has_deterioration(financial, deterioration_threshold):
        null_reasons.append("valuation_reversion_condition_b_deterioration")
        return None
    for metric in _rule_metrics(financial, lane):
        sigma_gap = derived.sigma_gap.get(metric)
        if sigma_gap is not None and sigma_gap <= lane.sigma_gap_max:
            return metric
    null_reasons.append("valuation_reversion_condition_b_no_sigma_gap")
    return None


def _condition_c(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    lane: ValuationReversionLane,
    deterioration_threshold: float,
    null_reasons: list[str],
) -> bool:
    if _has_deterioration(financial, deterioration_threshold):
        null_reasons.append("valuation_reversion_condition_c_deterioration")
        return False
    if derived.sector_relative_strength_percentile is None:
        null_reasons.append("valuation_reversion_condition_c_missing_sector_percentile")
        return False
    if derived.ticker_return_4w is None or derived.sector_return_4w is None:
        null_reasons.append("valuation_reversion_condition_c_missing_4w_returns")
        return False
    return (
        derived.sector_relative_strength_percentile <= lane.sector_relative_strength_percentile_max
        and derived.ticker_return_4w < derived.sector_return_4w
    )


def _cash_rich_asset_discount(
    financial: FinancialSnapshot,
    lane: CashRichLane,
    null_reasons: list[str],
) -> SignalHit | None:
    if financial.cash_to_market_cap is None:
        null_reasons.append("cash_rich_missing_cash_to_market_cap")
        return None
    if financial.price_to_equity is None:
        null_reasons.append("cash_rich_missing_price_to_equity")
        return None
    if lane.operating_profit_positive_required and (
        financial.operating_profit is None or financial.operating_profit <= 0
    ):
        return None
    if (
        financial.cash_to_market_cap < lane.cash_to_market_cap_min
        or financial.price_to_equity > lane.price_to_equity_max
    ):
        return None
    return SignalHit(
        name=SIGNAL_CASH_RICH,
        playbook=lane.playbook,
        reasons=(REASON_CASH_RICH,),
        metrics={
            "cash_to_market_cap": financial.cash_to_market_cap,
            "price_to_equity": financial.price_to_equity,
            "operating_profit": financial.operating_profit,
        },
    )


def _cashflow_yield_discount(
    financial: FinancialSnapshot,
    lane: CashflowYieldLane,
    null_reasons: list[str],
) -> SignalHit | None:
    if financial.ttm_quality_ocf_yield == TTMQuality.UNAVAILABLE:
        null_reasons.append("cashflow_yield_ttm_cfo_unavailable")
        return None
    if lane.ttm_cfo_required and financial.ocf_ttm is None:
        null_reasons.append("cashflow_yield_missing_ttm_cfo")
        return None
    if financial.ocf_yield is None or financial.ocf_yield < lane.ocf_yield_min:
        return None
    return SignalHit(
        name=SIGNAL_CASHFLOW_YIELD,
        playbook=lane.playbook,
        reasons=(REASON_CASHFLOW_YIELD,),
        metrics={
            "ocf_yield": financial.ocf_yield,
            "ocf_ttm": financial.ocf_ttm,
            "ttm_quality": financial.ttm_quality_ocf_yield.value,
        },
    )


def _sales_discount_growth(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    lane: SalesDiscountGrowthLane,
    null_reasons: list[str],
) -> SignalHit | None:
    ps_gap = derived.sector_median_gap.get("p_s")
    if ps_gap is None:
        null_reasons.append("sales_discount_missing_ps_sector_gap")
        return None
    if financial.sales_yoy is None:
        null_reasons.append("sales_discount_missing_sales_yoy")
        return None
    if ps_gap > lane.ps_sector_gap_max or financial.sales_yoy < lane.sales_yoy_min:
        return None
    if not _sales_operating_profit_gate(financial, lane):
        return None
    return SignalHit(
        name=SIGNAL_SALES_DISCOUNT,
        playbook=lane.playbook,
        reasons=(REASON_SALES_DISCOUNT,),
        metrics={
            "p_s": financial.p_s,
            "ps_sector_gap": ps_gap,
            "sales_yoy": financial.sales_yoy,
            "operating_profit": financial.operating_profit,
            "ocf_ttm": financial.ocf_ttm,
            "operating_profit_loss_narrowing": financial.operating_profit_loss_narrowing,
        },
    )


def _sales_operating_profit_gate(
    financial: FinancialSnapshot,
    lane: SalesDiscountGrowthLane,
) -> bool:
    if financial.operating_profit is not None and financial.operating_profit >= 0:
        return True
    if not lane.allow_operating_loss_if_cfo_positive_or_loss_narrowing:
        return False
    return bool(
        (financial.ocf_ttm is not None and financial.ocf_ttm > 0)
        or financial.operating_profit_loss_narrowing
    )


def _has_deterioration(financial: FinancialSnapshot, deterioration_threshold: float) -> bool:
    for value in (
        financial.eps_yoy,
        financial.sales_yoy,
        financial.operating_profit_yoy,
    ):
        if value is not None and value <= deterioration_threshold:
            return True
    return False


def _rule_metrics(financial: FinancialSnapshot, lane: ValuationReversionLane) -> tuple[str, ...]:
    metrics: list[str] = []
    for metric in lane.metrics:
        if metric == "ev_ebitda" and financial.ttm_quality_ev_ebitda != TTMQuality.EXACT:
            continue
        if metric == "p_s" and financial.ttm_quality_p_s == TTMQuality.UNAVAILABLE:
            continue
        if getattr(financial, metric, None) is None:
            continue
        metrics.append(metric)
    return tuple(metrics)
