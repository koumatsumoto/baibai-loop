"""Ranking components: toggles, evidence choice, lane order, strength keys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from baibai_loop.foundation.coerce import float_or, metric_map, string_or_none


@dataclass(frozen=True, slots=True)
class RankingToggles:
    """Named on/off switches for each ranking component of the selection sort key.

    All components default to on, which reproduces the production ranking
    exactly. Turning one off is used by the selection ablation replay to
    measure that component's contribution to recommended forward return; the
    production CLI never passes toggles.
    """

    macro: bool = True
    fast_boost: bool = True
    stabilization: bool = True
    lane_rank: bool = True
    strength: bool = True


def _primary_evidence_by_lane_order(
    raw_evidence_hits: object,
    lane_order: Sequence[str],
) -> tuple[str | None, dict[str, object]]:
    if not isinstance(raw_evidence_hits, Sequence) or isinstance(raw_evidence_hits, str):
        return None, {}
    evidence_by_lane: dict[str, Mapping[str, object]] = {}
    for evidence_hit in raw_evidence_hits:
        if not isinstance(evidence_hit, Mapping):
            continue
        if not _is_sizing_eligible_evidence(evidence_hit):
            continue
        name = string_or_none(evidence_hit.get("name"))
        if name is None:
            continue
        evidence_by_lane[name] = evidence_hit
    for lane in lane_order:
        evidence_hit = evidence_by_lane.get(lane)
        if evidence_hit is not None:
            return lane, metric_map(evidence_hit.get("metrics"))
    return None, {}


def _best_selection_evidence(
    evidence_hits: Sequence[Mapping[str, object]],
    *,
    lane_order: Sequence[str],
) -> tuple[str | None, dict[str, object], tuple[float, ...]]:
    entries = [
        (
            _lane_order_rank(name, lane_order),
            _evidence_strength_key(name, metrics),
            name,
            metrics,
        )
        for evidence_hit in evidence_hits
        if (name := string_or_none(evidence_hit.get("name"))) is not None
        for metrics in [metric_map(evidence_hit.get("metrics"))]
    ]
    if not entries:
        return None, {}, (0.0,)
    _, strength_key, name, metrics = min(entries, key=lambda item: (item[0], item[1]))
    return name, metrics, strength_key


def _lane_order_rank(name: str | None, lane_order: Sequence[str]) -> int:
    try:
        return lane_order.index(name or "")
    except ValueError:
        return len(lane_order)


def _sizing_eligible_evidence_hits(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(hit for hit in evidence_hits if _is_sizing_eligible_evidence(hit))


def _is_sizing_eligible_evidence(evidence_hit: Mapping[str, object]) -> bool:
    source_status = evidence_hit.get("source_status")
    if isinstance(source_status, str) and source_status != "ok":
        return False
    return evidence_hit.get("sizing_eligible") is not False


def _evidence_strength_key(name: str, metrics: Mapping[str, object]) -> tuple[float, ...]:
    match name:
        case "valuation-reversion":
            return (
                float_or(metrics.get("condition_a_sector_median_gap"), 1.0),
                float_or(metrics.get("condition_a_self_range_percentile"), 1.0),
                float_or(metrics.get("condition_b_sigma_gap"), 1.0),
                float_or(metrics.get("price_change_60d"), 1.0),
            )
        case "cash-rich-asset-discount":
            return (
                -float_or(metrics.get("cash_to_market_cap"), 0.0),
                float_or(metrics.get("price_to_equity"), 99.0),
            )
        case "cashflow-yield-discount":
            return (
                -float_or(metrics.get("ocf_yield"), 0.0),
                -float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "sales-discount-growth":
            operating_profit = float_or(metrics.get("operating_profit"), -1.0)
            return (
                float_or(metrics.get("ps_sector_gap"), 1.0),
                -float_or(metrics.get("sales_yoy"), 0.0),
                0.0 if operating_profit >= 0 else 1.0,
            )
        case _:
            return (0.0,)
