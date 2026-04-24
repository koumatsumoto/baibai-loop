from __future__ import annotations

from .config import YOY_DETERIORATION_THRESHOLD
from .schema import DerivedMetrics, FinancialSnapshot, ScreeningResult, TTMQuality

THRESHOLD_A = "sector_median_under_20pct_and_self_range_bottom_20pct"
THRESHOLD_B = "price_down_60d_and_valuation_sigma_down"
THRESHOLD_C = "sector_rotation_short_sell"


def evaluate_screening(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
) -> ScreeningResult:
    threshold_hit: list[str] = []
    failure_reasons: list[str] = []
    null_reasons: list[str] = []

    if _condition_a(financial, derived, null_reasons):
        threshold_hit.append(THRESHOLD_A)
    if _condition_b(financial, derived, null_reasons):
        threshold_hit.append(THRESHOLD_B)
    if _condition_c(financial, derived, null_reasons):
        threshold_hit.append(THRESHOLD_C)

    if not threshold_hit:
        failure_reasons.append("no_threshold_hit")

    return ScreeningResult(
        pass_fail=bool(threshold_hit),
        threshold_hit=tuple(threshold_hit),
        failure_reasons=tuple(failure_reasons),
        null_reasons=tuple(dict.fromkeys(null_reasons)),
    )


def _condition_a(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    null_reasons: list[str],
) -> bool:
    if derived.short_history_flag:
        null_reasons.append("condition_a_short_history")
        return False
    for metric in _rule_metrics(financial):
        sector_gap = derived.sector_median_gap.get(metric)
        self_percentile = derived.self_range_percentile.get(metric)
        if sector_gap is None or self_percentile is None:
            continue
        if sector_gap <= -0.20 and self_percentile <= 0.20:
            return True
    null_reasons.append("condition_a_no_metric")
    return False


def _condition_b(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    null_reasons: list[str],
) -> bool:
    if derived.price_change_60d is None:
        null_reasons.append("condition_b_missing_price_change_60d")
        return False
    if derived.price_change_60d > -0.15:
        return False
    if _has_deterioration(financial):
        null_reasons.append("condition_b_deterioration")
        return False
    for metric in _rule_metrics(financial):
        sigma_gap = derived.sigma_gap.get(metric)
        if sigma_gap is not None and sigma_gap <= -1.0:
            return True
    null_reasons.append("condition_b_no_sigma_gap")
    return False


def _condition_c(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    null_reasons: list[str],
) -> bool:
    if _has_deterioration(financial):
        null_reasons.append("condition_c_deterioration")
        return False
    if derived.sector_relative_strength_percentile is None:
        null_reasons.append("condition_c_missing_sector_percentile")
        return False
    if derived.ticker_return_4w is None or derived.sector_return_4w is None:
        null_reasons.append("condition_c_missing_4w_returns")
        return False
    return (
        derived.sector_relative_strength_percentile <= 0.20
        and derived.ticker_return_4w < derived.sector_return_4w
    )


def _has_deterioration(financial: FinancialSnapshot) -> bool:
    for value in (
        financial.eps_yoy,
        financial.sales_yoy,
        financial.operating_profit_yoy,
    ):
        if value is not None and value <= YOY_DETERIORATION_THRESHOLD:
            return True
    return False


def _rule_metrics(financial: FinancialSnapshot) -> tuple[str, ...]:
    # issue #15: _valuation_history の ev_ebitda 系が代数的に price / snapshot.ev_ebitda
    # に縮退しているため、self_range_percentile と sigma_gap が本質的に price 判定に
    # なってしまう。issue #15 解決までは ttm_quality_ev_ebitda == EXACT であっても
    # 閾値 A/B には参加させない。
    del financial
    return ("per_trailing", "pbr")
