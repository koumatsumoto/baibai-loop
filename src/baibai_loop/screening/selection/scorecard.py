"""Candidate scorecard: a multi-axis L3 triage over weekly screen output.

The screen records facts and ``select`` ranks a small forward-measured queue.
The scorecard is a separate analysis-layer (L3) aid: it narrows the screen
output to a liquid, structurally-tilted shortlist and presents each survivor's
risk-reward **axes** as coordinates — valuation discount, cashflow durability,
balance-sheet strength, price dislocation, long-hold survivability, and
structural outlook. It deliberately never collapses them into one fused score
(scores are coordinates, not verdicts — README / design-principles §9).

A transparent lexicographic display order is offered so a long shortlist is
tractable, but it is a triage convenience, not a forward-measured ranking: the
canonical mechanical screen and ``select`` queue are unchanged, and the adoption
judgment stays with research and the human. The order reuses the existing
ranking primitives (lane order + evidence strength keys) and only prepends the
structural / long-hold tilt the research frame asks for.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_loop.coerce import optional_float

from ..rule_config import ScreeningRules
from .lenses import _candidate_lenses
from .profiles import resolve_selection_rules
from .ranking import (
    _best_selection_evidence,
    _lane_order_rank,
    _sizing_eligible_evidence_hits,
)
from .records import CandidateRecord
from .structural import (
    StructuralOutlook,
    StructuralOutlookConfig,
    StructuralOutlookResult,
    classify_structural_outlook,
)

_STRUCTURAL_RANK: Mapping[StructuralOutlook, int] = {
    StructuralOutlook.AI_TAILWIND: 0,
    StructuralOutlook.NEUTRAL: 1,
    StructuralOutlook.STRUCTURAL_DECLINE: 2,
}

_LONG_HOLD_RANK: Mapping[str, int] = {"high": 0, "medium": 1, "low": 2, "unknown": 3}

DEFAULT_INCLUDE_OUTLOOKS: frozenset[StructuralOutlook] = frozenset(
    {StructuralOutlook.AI_TAILWIND, StructuralOutlook.NEUTRAL}
)


def build_scorecard_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    rules: ScreeningRules,
    structural_config: StructuralOutlookConfig,
    top: int,
    candidates_ref: str,
    include_outlooks: frozenset[StructuralOutlook] = DEFAULT_INCLUDE_OUTLOOKS,
    exclude_tickers: frozenset[str] = frozenset(),
    profile: str | None = None,
) -> dict[str, object]:
    if top < 1:
        raise ValueError("top must be greater than zero")
    effective_profile = profile or rules.selection.default_profile
    selection_rules = resolve_selection_rules(rules.selection, profile=effective_profile)
    liquidity = selection_rules.liquidity
    required_jpx_flags = frozenset(rules.universe.required_jpx_flags)
    lane_order = tuple(rules.output.research_selection_lane_order)

    counts = Counter[str]()
    counts["input"] = len(candidates)
    excluded_by_outlook = Counter[str]()
    entries: list[tuple[tuple[object, ...], dict[str, object]]] = []

    for item in candidates:
        if not liquidity.matches(
            market_cap_oku=item.market_cap_oku,
            avg_turnover_oku=item.avg_turnover_oku,
            listing_span_days=item.listing_span_days,
            jpx_flags=item.jpx_flags,
            required_jpx_flags=required_jpx_flags,
            require_facts=False,
        ):
            counts["excluded_liquidity"] += 1
            continue
        structural = classify_structural_outlook(
            ticker=item.ticker,
            sector_33=item.sector_33,
            config=structural_config,
        )
        if structural.outlook not in include_outlooks:
            counts["excluded_outlook"] += 1
            excluded_by_outlook[structural.outlook.value] += 1
            continue
        if item.ticker in exclude_tickers:
            counts["excluded_held"] += 1
            continue
        eligible_evidence = _sizing_eligible_evidence_hits(item.evidence_hits)
        if not eligible_evidence:
            counts["excluded_no_evidence"] += 1
            continue
        lane, _lane_metrics, strength_key = _best_selection_evidence(
            eligible_evidence, lane_order=lane_order
        )
        lenses = _candidate_lenses(item, selection_rules)
        entry = _scorecard_entry(item, structural=structural, lenses=lenses, lane=lane)
        sort_key = (
            _STRUCTURAL_RANK[structural.outlook],
            _LONG_HOLD_RANK.get(_long_hold_rating(lenses), 3),
            _lane_order_rank(lane, lane_order),
            *strength_key,
            item.ticker,
        )
        entries.append((sort_key, entry))

    entries.sort(key=lambda pair: pair[0])
    shortlist = [entry for _, entry in entries[:top]]
    counts["eligible"] = len(entries)
    counts["shortlist"] = len(shortlist)

    return {
        "scorecard": {
            "asof": asof_date.isoformat(),
            "profile": effective_profile,
            "candidates_ref": candidates_ref,
            "structural_config_version": structural_config.version,
            "include_outlooks": sorted(outlook.value for outlook in include_outlooks),
            "excluded_tickers": sorted(exclude_tickers),
            "top": top,
            "counts": dict(counts),
            "excluded_by_outlook": dict(excluded_by_outlook),
            "by_outlook": dict(Counter(str(entry["structural_outlook"]) for entry in shortlist)),
            "by_sector": dict(Counter(str(entry["sector_33"]) for entry in shortlist)),
            "display_order_note": (
                "lexicographic triage (structural tilt, long-hold rating, lane order, "
                "evidence strength); an L3 convenience, not a forward-measured ranking"
            ),
        },
        "shortlist": shortlist,
    }


def _scorecard_entry(
    item: CandidateRecord,
    *,
    structural: StructuralOutlookResult,
    lenses: Mapping[str, object],
    lane: str | None,
) -> dict[str, object]:
    metrics = item.metrics
    fast = _lens_mapping(lenses, "fast_dislocation")
    long_hold = _lens_mapping(lenses, "long_hold_survivability")
    return {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": item.sector_33,
        "market_cap_oku": item.market_cap_oku,
        "avg_turnover_oku": item.avg_turnover_oku,
        "structural_outlook": structural.outlook.value,
        "structural_basis": structural.basis,
        "structural_note": structural.note,
        "primary_lane": lane,
        "evidence_lanes": [
            name for hit in item.evidence_hits if (name := hit.get("name")) is not None
        ],
        "axes": {
            "valuation_discount": {
                "per_trailing": item.per_trailing,
                "per_forward": item.per_forward,
                "pbr": item.pbr,
                "p_s": item.p_s,
                "ev_ebitda": item.ev_ebitda,
                "pcfr": item.pcfr,
            },
            "cashflow_durability": {
                "ocf_yield": optional_float(metrics.get("ocf_yield")),
                "fcf_yield": optional_float(metrics.get("fcf_yield")),
                "cfo_yoy": optional_float(metrics.get("cfo_yoy")),
            },
            "balance_sheet": {
                "net_cash_to_market_cap": optional_float(metrics.get("net_cash_to_market_cap")),
                "equity_ratio": optional_float(metrics.get("equity_ratio")),
                "price_to_equity": optional_float(metrics.get("price_to_equity")),
                "cash_to_market_cap": optional_float(metrics.get("cash_to_market_cap")),
            },
            "dislocation": {
                "price_change_20d": item.price_change_20d,
                "price_change_60d": item.price_change_60d,
                "gap_from_52w_low": item.gap_from_52w_low,
                "fast_eligible": fast.get("eligible"),
                "fast_confidence": fast.get("confidence"),
                "fast_stabilized": fast.get("stabilized"),
            },
            "long_hold": {
                "rating": long_hold.get("rating"),
                "support_count": long_hold.get("support_count"),
            },
            "structural": structural.to_dict(),
        },
        "next_earnings_date": item.next_earnings_date,
    }


def _lens_mapping(lenses: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = lenses.get(key)
    return value if isinstance(value, Mapping) else {}


def _long_hold_rating(lenses: Mapping[str, object]) -> str:
    rating = _lens_mapping(lenses, "long_hold_survivability").get("rating")
    return rating if isinstance(rating, str) else "unknown"
