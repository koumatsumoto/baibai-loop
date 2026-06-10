"""Selection lenses: fast dislocation and long-hold survivability."""

from __future__ import annotations

from collections.abc import Mapping

from ..rule_config import SelectionRules
from ._coerce import _float_or, _number, _string_value
from .records import CandidateRecord


def _candidate_lenses(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    return {
        "fast_dislocation": _fast_dislocation_lens(item, rules),
        "long_hold_survivability": _long_hold_survivability_lens(item, rules),
    }


def _fast_dislocation_lens(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    lane_rules = rules.fast_dislocation
    price_triggers: list[dict[str, object]] = []
    auxiliary_triggers: list[dict[str, object]] = []
    trigger_specs = (
        ("price_change_1d", item.price_change_1d, lane_rules.price_change_1d_max),
        ("price_change_5d", item.price_change_5d, lane_rules.price_change_5d_max),
        ("price_change_20d", item.price_change_20d, lane_rules.price_change_20d_max),
        ("price_change_60d", item.price_change_60d, lane_rules.price_change_60d_max),
    )
    for metric, value, threshold in trigger_specs:
        if threshold is not None and value is not None and value <= threshold:
            price_triggers.append({"metric": metric, "value": value, "threshold": threshold})
    if (
        lane_rules.gap_from_52w_low_max is not None
        and item.gap_from_52w_low is not None
        and item.gap_from_52w_low <= lane_rules.gap_from_52w_low_max
    ):
        auxiliary_triggers.append(
            {
                "metric": "gap_from_52w_low",
                "value": item.gap_from_52w_low,
                "threshold": lane_rules.gap_from_52w_low_max,
            }
        )
    if (
        lane_rules.turnover_spike_5d_min is not None
        and item.turnover_spike_5d is not None
        and item.turnover_spike_5d >= lane_rules.turnover_spike_5d_min
    ):
        auxiliary_triggers.append(
            {
                "metric": "turnover_spike_5d",
                "value": item.turnover_spike_5d,
                "threshold": lane_rules.turnover_spike_5d_min,
            }
        )

    guard_reasons = _fundamental_guard_reasons(item, rules)
    guard_count = len(guard_reasons)
    guard_families = sorted({_fundamental_guard_family(reason) for reason in guard_reasons})
    stale_fundamental_metrics = _has_edinet_freshness_warning(item)
    eligible = (
        lane_rules.enabled
        and bool(price_triggers)
        and guard_count >= lane_rules.min_fundamental_guard_count
        and len(guard_families) >= lane_rules.min_fundamental_guard_family_count
    )
    confidence = "none"
    if eligible:
        high_confidence = (
            guard_count >= lane_rules.high_confidence_guard_count
            and len(guard_families) >= lane_rules.high_confidence_guard_family_count
            and not stale_fundamental_metrics
        )
        confidence = "high" if high_confidence else "medium"
    data_status = _fast_dislocation_data_status(item)
    if stale_fundamental_metrics:
        data_status = "stale_fundamental_metrics"
    # Stabilization: the fast triggers fire on 5-60 day declines; a name whose
    # latest session is still a down day is a falling knife, one that closed
    # flat-or-up shows the minimum evidence of a halt. Fixed zero threshold on
    # the recorded 1-day return — no fitted parameter.
    stabilized = item.price_change_1d is not None and item.price_change_1d >= 0
    return {
        "eligible": eligible,
        "confidence": confidence,
        "stabilized": stabilized,
        "price_triggers": price_triggers,
        "auxiliary_triggers": auxiliary_triggers,
        "fundamental_guard_count": guard_count,
        "fundamental_guard_family_count": len(guard_families),
        "fundamental_guard_families": guard_families,
        "fundamental_guard_reasons": guard_reasons,
        "stale_fundamental_metrics": stale_fundamental_metrics,
        "data_status": data_status,
    }


def _fundamental_guard_reasons(item: CandidateRecord, rules: SelectionRules) -> list[str]:
    lane_rules = rules.fast_dislocation
    metrics = item.metrics
    reasons: list[str] = []
    if _float_or(metrics.get("ocf_yield"), -1.0) >= lane_rules.ocf_yield_min:
        reasons.append("ocf_yield")
    if _float_or(metrics.get("fcf_yield"), -1.0) >= lane_rules.fcf_yield_min:
        reasons.append("fcf_yield")
    if (
        _float_or(metrics.get("price_to_equity"), 99.0) <= lane_rules.price_to_equity_max
        and _float_or(metrics.get("equity_ratio"), -1.0) >= lane_rules.equity_ratio_min
    ):
        reasons.append("asset_discount_with_equity_buffer")
    if _float_or(metrics.get("net_cash_to_market_cap"), -99.0) >= (
        lane_rules.net_cash_to_market_cap_min
    ):
        reasons.append("net_cash_buffer")
    operating_profit_ok = not lane_rules.operating_profit_positive_required or (
        _float_or(metrics.get("operating_profit"), -1.0) > 0
    )
    sales_yoy = _number(metrics.get("sales_yoy"))
    if sales_yoy is not None and sales_yoy >= lane_rules.sales_yoy_min and operating_profit_ok:
        reasons.append("sales_growth_with_profit")
    return reasons


def _fundamental_guard_family(reason: str) -> str:
    if reason in {"fcf_yield", "ocf_yield"}:
        return "cash_flow"
    if reason in {"asset_discount_with_equity_buffer", "net_cash_buffer"}:
        return "balance_sheet"
    if reason == "sales_growth_with_profit":
        return "profitability"
    return "other"


def _has_edinet_freshness_warning(item: CandidateRecord) -> bool:
    return any(
        _string_value(warning.get("stale_metric")) == "edinet_metrics"
        for warning in item.freshness_warnings
    )


def _fast_dislocation_data_status(item: CandidateRecord) -> str:
    if item.price_change_5d is None and item.price_change_20d is None:
        return (
            "short_return_pipeline_missing"
            if item.price_change_60d is not None
            else "missing_price_history"
        )
    if item.price_change_20d is None:
        return "short_return_pipeline_missing"
    return "ok"


def _long_hold_survivability_lens(
    item: CandidateRecord,
    rules: SelectionRules,
) -> dict[str, object]:
    lens_rules = rules.long_hold_survivability
    metrics = item.metrics
    reasons: list[str] = []
    missing_reasons: list[str] = []
    weak_reasons: list[str] = []
    equity_ratio = _number(metrics.get("equity_ratio"))
    if equity_ratio is None:
        missing_reasons.append("equity_ratio_missing")
    elif equity_ratio >= lens_rules.equity_ratio_high_min:
        reasons.append("high_equity_ratio")
    elif equity_ratio >= lens_rules.equity_ratio_medium_min:
        reasons.append("medium_equity_ratio")
    else:
        weak_reasons.append("low_equity_ratio")

    net_cash_to_market_cap = _number(metrics.get("net_cash_to_market_cap"))
    if net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_high_min
    ):
        reasons.append("net_cash_buffer")
    elif net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_medium_min
    ):
        reasons.append("non_negative_net_cash")

    if _float_or(metrics.get("cash_to_market_cap"), -1.0) >= (
        lens_rules.cash_to_market_cap_high_min
    ):
        reasons.append("cash_buffer")
    ocf_yield = _number(metrics.get("ocf_yield"))
    if ocf_yield is None:
        missing_reasons.append("ocf_yield_missing")
    elif ocf_yield > lens_rules.ocf_yield_positive_min:
        reasons.append("positive_ocf_yield")
    else:
        weak_reasons.append("weak_ocf_yield")
    if _float_or(metrics.get("fcf_yield"), -1.0) > lens_rules.fcf_yield_positive_min:
        reasons.append("positive_fcf_yield")
    operating_profit = _number(metrics.get("operating_profit"))
    if operating_profit is None:
        missing_reasons.append("operating_profit_missing")
    elif operating_profit > 0:
        reasons.append("positive_operating_profit")
    else:
        weak_reasons.append("operating_profit_not_positive")
    if item.avg_turnover_oku is None:
        missing_reasons.append("liquidity_missing")
    elif item.avg_turnover_oku >= lens_rules.min_avg_turnover_oku:
        reasons.append("liquidity_pass")
    else:
        weak_reasons.append("liquidity_low")

    support_count = len(set(reasons))
    caution_reasons = sorted({*missing_reasons, *weak_reasons})
    if support_count >= lens_rules.high_min_support_count and len(caution_reasons) <= 1:
        rating = "high"
    elif support_count >= lens_rules.medium_min_support_count:
        rating = "medium"
    elif support_count == 0 and len(missing_reasons) >= 3:
        rating = "unknown"
    else:
        rating = "low"
    return {
        "rating": rating,
        "support_count": support_count,
        "reasons": sorted(set(reasons)),
        "missing_reasons": sorted(set(missing_reasons)),
        "weak_reasons": sorted(set(weak_reasons)),
        "caution_reasons": caution_reasons,
    }


def _fast_lens(candidate: Mapping[str, object]) -> Mapping[str, object]:
    lenses = candidate.get("lenses")
    if not isinstance(lenses, Mapping):
        return {}
    lens = lenses.get("fast_dislocation")
    return lens if isinstance(lens, Mapping) else {}


def _long_hold_lens(candidate: Mapping[str, object]) -> Mapping[str, object]:
    lenses = candidate.get("lenses")
    if not isinstance(lenses, Mapping):
        return {}
    lens = lenses.get("long_hold_survivability")
    return lens if isinstance(lens, Mapping) else {}


def _fast_guard_count(candidate: Mapping[str, object]) -> int:
    count = _fast_lens(candidate).get("fundamental_guard_count")
    return int(count) if isinstance(count, int) else 0
