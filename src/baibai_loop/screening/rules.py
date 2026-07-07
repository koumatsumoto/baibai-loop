from __future__ import annotations

from .rule_config import (
    CashflowYieldPlaybook,
    CashRichPlaybook,
    SalesDiscountGrowthPlaybook,
    ScreeningRules,
    ValuationReversionPlaybook,
)
from .schema import DerivedMetrics, EvidenceHit, FinancialSnapshot, ScreeningResult, TTMQuality

PLAYBOOK_VALUATION_REVERSION = "valuation-reversion"
PLAYBOOK_CASH_RICH = "cash-rich-asset-discount"
PLAYBOOK_CASHFLOW_YIELD = "cashflow-yield-discount"
PLAYBOOK_SALES_DISCOUNT = "sales-discount-growth"

REASON_SECTOR_SELF_RANGE = "sector_median_discount_and_self_range_bottom"
REASON_VALUATION_SIGMA = "valuation_sigma_down"
REASON_CASH_RICH = "cash_to_market_cap_price_to_equity_and_equity_ratio"
REASON_CASHFLOW_YIELD = "ocf_yield_discount"
REASON_SALES_DISCOUNT = "ps_discount_with_sales_growth"


def evaluate_screening(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    rules: ScreeningRules,
    *,
    sector_33: str = "",
) -> ScreeningResult:
    evidence_hits: list[EvidenceHit] = []
    null_reasons: list[str] = []

    for name, playbook in rules.screening_playbooks.items():
        match name:
            case "valuation-reversion":
                if not isinstance(playbook, ValuationReversionPlaybook):
                    continue
                if _is_excluded_sector(sector_33, playbook.excluded_sectors):
                    null_reasons.append("valuation_reversion_excluded_sector")
                    continue
                hit = _valuation_reversion(
                    financial,
                    derived,
                    playbook,
                    rules.quality.yoy_deterioration_threshold,
                    null_reasons,
                )
            case "cash-rich-asset-discount":
                if not isinstance(playbook, CashRichPlaybook):
                    continue
                if _is_excluded_sector(sector_33, playbook.excluded_sectors):
                    null_reasons.append("cash_rich_excluded_sector")
                    continue
                hit = _cash_rich_asset_discount(financial, playbook, null_reasons)
            case "cashflow-yield-discount":
                if not isinstance(playbook, CashflowYieldPlaybook):
                    continue
                if _is_excluded_sector(sector_33, playbook.excluded_sectors):
                    null_reasons.append("cashflow_yield_excluded_sector")
                    continue
                hit = _cashflow_yield_discount(financial, playbook, null_reasons)
            case "sales-discount-growth":
                if not isinstance(playbook, SalesDiscountGrowthPlaybook):
                    continue
                if _is_excluded_sector(sector_33, playbook.excluded_sectors):
                    null_reasons.append("sales_discount_excluded_sector")
                    continue
                hit = _sales_discount_growth(financial, derived, playbook, null_reasons)
            case _:  # pragma: no cover - config validator rejects this.
                hit = None
        if hit is not None:
            evidence_hits.append(hit)

    if not evidence_hits:
        return ScreeningResult(
            pass_fail=False,
            failure_reasons=("no_evidence_hit",),
            null_reasons=tuple(dict.fromkeys(null_reasons)),
        )
    return ScreeningResult(
        pass_fail=True,
        evidence_hits=tuple(evidence_hits),
        null_reasons=tuple(dict.fromkeys(null_reasons)),
    )


def _is_excluded_sector(sector_33: str, excluded_sectors: tuple[str, ...]) -> bool:
    return sector_33 in excluded_sectors


def _valuation_reversion(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    playbook: ValuationReversionPlaybook,
    deterioration_threshold: float,
    null_reasons: list[str],
) -> EvidenceHit | None:
    reasons: list[str] = []
    metrics: dict[str, float | int | bool | str | None] = {}
    hit_metric_a = _condition_a_metric(financial, derived, playbook, null_reasons)
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
        playbook,
        deterioration_threshold,
        null_reasons,
    )
    if hit_metric_b is not None:
        reasons.append(REASON_VALUATION_SIGMA)
        metrics["condition_b_metric"] = hit_metric_b
        metrics["price_change_60d"] = derived.price_change_60d
        metrics["condition_b_sigma_gap"] = derived.sigma_gap.get(hit_metric_b)
    if not reasons:
        return None
    return EvidenceHit(
        name=PLAYBOOK_VALUATION_REVERSION,
        playbook_id=playbook.playbook_id,
        reasons=tuple(reasons),
        metrics=metrics,
    )


def _condition_a_metric(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    playbook: ValuationReversionPlaybook,
    null_reasons: list[str],
) -> str | None:
    if derived.short_history_flag:
        null_reasons.append("valuation_reversion_condition_a_short_history")
        return None
    for metric in _rule_metrics(financial, playbook):
        sector_gap = derived.sector_median_gap.get(metric)
        self_percentile = derived.self_range_percentile.get(metric)
        if sector_gap is None or self_percentile is None:
            continue
        if (
            sector_gap <= playbook.sector_median_gap_max
            and self_percentile <= playbook.self_range_percentile_max
        ):
            return metric
    null_reasons.append("valuation_reversion_condition_a_no_metric")
    return None


def _condition_b_metric(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    playbook: ValuationReversionPlaybook,
    deterioration_threshold: float,
    null_reasons: list[str],
) -> str | None:
    # 条件 B は σギャップ判定かつ悪化ゲート。60 日下落は要件にしない。price_change_60d
    # は evidence hit に事実として記録するが判定には使わない
    # (metrics["price_change_60d"] は None を許容する)。
    if _has_deterioration(financial, deterioration_threshold):
        null_reasons.append("valuation_reversion_condition_b_deterioration")
        return None
    for metric in _rule_metrics(financial, playbook):
        sigma_gap = derived.sigma_gap.get(metric)
        if sigma_gap is not None and sigma_gap <= playbook.sigma_gap_max:
            return metric
    null_reasons.append("valuation_reversion_condition_b_no_sigma_gap")
    return None


def _cash_rich_asset_discount(
    financial: FinancialSnapshot,
    playbook: CashRichPlaybook,
    null_reasons: list[str],
) -> EvidenceHit | None:
    if financial.cash_to_market_cap is None:
        null_reasons.append("cash_rich_missing_cash_to_market_cap")
        return None
    if financial.price_to_equity is None:
        null_reasons.append("cash_rich_missing_price_to_equity")
        return None
    if financial.equity_ratio is None:
        null_reasons.append("cash_rich_missing_equity_ratio")
        return None
    if (
        playbook.edinet_net_cash_to_market_cap_min_if_available is not None
        and financial.net_cash_to_market_cap is not None
        and financial.net_cash_to_market_cap
        < playbook.edinet_net_cash_to_market_cap_min_if_available
    ):
        null_reasons.append("cash_rich_edinet_net_cash_contradiction")
        return None
    if playbook.operating_profit_positive_required and (
        financial.operating_profit is None or financial.operating_profit <= 0
    ):
        return None
    # C1: deterioration gate — block when operating_profit_yoy drops past the
    # configured threshold. Mirrors the valuation-reversion B/C conditions so
    # an OR-passing playbook structure does not let a -50% earnings company sneak
    # through cash-rich just because its BS still looks rich.
    if (
        playbook.operating_profit_yoy_deterioration_threshold is not None
        and financial.operating_profit_yoy is not None
        and financial.operating_profit_yoy <= playbook.operating_profit_yoy_deterioration_threshold
    ):
        null_reasons.append("cash_rich_operating_profit_deterioration")
        return None
    if (
        financial.cash_to_market_cap < playbook.cash_to_market_cap_min
        or financial.price_to_equity > playbook.price_to_equity_max
        or financial.equity_ratio < playbook.equity_ratio_min
    ):
        return None
    return EvidenceHit(
        name=PLAYBOOK_CASH_RICH,
        playbook_id=playbook.playbook_id,
        reasons=(REASON_CASH_RICH,),
        metrics={
            "cash_to_market_cap": financial.cash_to_market_cap,
            "net_cash_to_market_cap": financial.net_cash_to_market_cap,
            "debt": financial.debt,
            "cash": financial.cash,
            "price_to_equity": financial.price_to_equity,
            "equity_ratio": financial.equity_ratio,
            "operating_profit": financial.operating_profit,
        },
    )


def _cashflow_yield_discount(
    financial: FinancialSnapshot,
    playbook: CashflowYieldPlaybook,
    null_reasons: list[str],
) -> EvidenceHit | None:
    if financial.ttm_quality_ocf_yield == TTMQuality.UNAVAILABLE:
        null_reasons.append("cashflow_yield_ttm_cfo_unavailable")
        return None
    if playbook.ttm_cfo_required and financial.ocf_ttm is None:
        null_reasons.append("cashflow_yield_missing_ttm_cfo")
        return None
    if playbook.cfo_yoy_required and financial.cfo_yoy is None:
        null_reasons.append("cashflow_yield_missing_cfo_yoy")
        return None
    if financial.cfo_yoy is not None and financial.cfo_yoy < playbook.cfo_yoy_min:
        return None
    if financial.ocf_yield is None or financial.ocf_yield < playbook.ocf_yield_min:
        return None
    # C1: deterioration gate — same rationale as cash-rich. OCF can stay high
    # while the operating engine is decaying year over year; this stops that.
    if (
        playbook.operating_profit_yoy_deterioration_threshold is not None
        and financial.operating_profit_yoy is not None
        and financial.operating_profit_yoy <= playbook.operating_profit_yoy_deterioration_threshold
    ):
        null_reasons.append("cashflow_yield_operating_profit_deterioration")
        return None
    # C4: require FCF > 0. OCF+/FCF- means heavy capex is consuming the cash
    # the OCF yield advertises; that profile mean-reverts slowly and
    # underperforms 4w cohorts. fcf_yield is None if the EDINET feed is missing
    # — only enforce the gate when the data is present.
    if playbook.fcf_yield_required_positive and (
        financial.fcf_yield is not None and financial.fcf_yield <= 0
    ):
        null_reasons.append("cashflow_yield_fcf_negative")
        return None
    return EvidenceHit(
        name=PLAYBOOK_CASHFLOW_YIELD,
        playbook_id=playbook.playbook_id,
        reasons=(REASON_CASHFLOW_YIELD,),
        metrics={
            "ocf_yield": financial.ocf_yield,
            "ocf_ttm": financial.ocf_ttm,
            "cfo_yoy": financial.cfo_yoy,
            "fcf_yield": financial.fcf_yield,
            "ttm_quality": financial.ttm_quality_ocf_yield.value,
        },
    )


def _sales_discount_growth(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    playbook: SalesDiscountGrowthPlaybook,
    null_reasons: list[str],
) -> EvidenceHit | None:
    ps_gap = derived.sector_median_gap.get("p_s")
    if ps_gap is None:
        null_reasons.append("sales_discount_missing_ps_sector_gap")
        return None
    if financial.sales_yoy is None:
        null_reasons.append("sales_discount_missing_sales_yoy")
        return None
    if ps_gap > playbook.ps_sector_gap_max or financial.sales_yoy < playbook.sales_yoy_min:
        return None
    if not _sales_operating_profit_gate(financial, playbook):
        return None
    return EvidenceHit(
        name=PLAYBOOK_SALES_DISCOUNT,
        playbook_id=playbook.playbook_id,
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
    playbook: SalesDiscountGrowthPlaybook,
) -> bool:
    # C2: operating margin floor — kills the "loss narrowing" escape hatch
    # that admitted chronic losers (e.g. -100B -> -50B counts as "narrowing"
    # but the absolute margin is still catastrophic). Only enforce when both
    # operating_profit and sales are known.
    if (
        playbook.operating_margin_min is not None
        and financial.operating_profit is not None
        and financial.sales is not None
        and financial.sales > 0
        and (financial.operating_profit / financial.sales) < playbook.operating_margin_min
    ):
        return False
    if financial.operating_profit is not None and financial.operating_profit >= 0:
        return True
    if not playbook.allow_operating_loss_if_cfo_positive_or_loss_narrowing:
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


def _rule_metrics(
    financial: FinancialSnapshot, playbook: ValuationReversionPlaybook
) -> tuple[str, ...]:
    metrics: list[str] = []
    for metric in playbook.metrics:
        if metric == "ev_ebitda" and financial.ttm_quality_ev_ebitda != TTMQuality.EXACT:
            continue
        if metric == "p_s" and financial.ttm_quality_p_s == TTMQuality.UNAVAILABLE:
            continue
        value = getattr(financial, metric, None)
        if value is None:
            continue
        if metric == "ev_ebitda" and value <= 0:
            continue
        metrics.append(metric)
    return tuple(metrics)
