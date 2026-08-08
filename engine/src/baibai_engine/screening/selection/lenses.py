"""Selection lens: durability (塩漬け耐性) annotation."""

from __future__ import annotations

from collections.abc import Mapping

from baibai_engine.foundation.coerce import float_or, optional_float

from ..rule_config import SelectionRules
from .records import CandidateRecord


def _candidate_lenses(
    item: CandidateRecord,
    rules: SelectionRules,
) -> dict[str, object]:
    return {
        "durability": _durability_lens(item, rules),
    }


def _durability_lens(
    item: CandidateRecord,
    rules: SelectionRules,
) -> dict[str, object]:
    lens_rules = rules.durability
    metrics = item.metrics
    reasons: list[str] = []
    missing_reasons: list[str] = []
    weak_reasons: list[str] = []
    equity_ratio = optional_float(metrics.get("equity_ratio"))
    if equity_ratio is None:
        missing_reasons.append("equity_ratio_missing")
    elif equity_ratio >= lens_rules.equity_ratio_high_min:
        reasons.append("high_equity_ratio")
    elif equity_ratio >= lens_rules.equity_ratio_medium_min:
        reasons.append("medium_equity_ratio")
    else:
        weak_reasons.append("low_equity_ratio")

    net_cash_to_market_cap = optional_float(metrics.get("net_cash_to_market_cap"))
    if net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_high_min
    ):
        reasons.append("net_cash_buffer")
    elif net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_medium_min
    ):
        reasons.append("non_negative_net_cash")

    if float_or(metrics.get("cash_to_market_cap"), -1.0) >= (
        lens_rules.cash_to_market_cap_high_min
    ):
        reasons.append("cash_buffer")
    ocf_yield = optional_float(metrics.get("ocf_yield"))
    if ocf_yield is None:
        missing_reasons.append("ocf_yield_missing")
    elif ocf_yield > lens_rules.ocf_yield_positive_min:
        reasons.append("positive_ocf_yield")
    else:
        weak_reasons.append("weak_ocf_yield")
    if float_or(metrics.get("fcf_yield"), -1.0) > lens_rules.fcf_yield_positive_min:
        reasons.append("positive_fcf_yield")
    operating_profit = optional_float(metrics.get("operating_profit"))
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


def _durability_lens_of(candidate: Mapping[str, object]) -> Mapping[str, object]:
    lenses = candidate.get("lenses")
    if not isinstance(lenses, Mapping):
        return {}
    lens = lenses.get("durability")
    return lens if isinstance(lens, Mapping) else {}
