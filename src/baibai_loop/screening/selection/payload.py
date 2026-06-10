"""Selection payload assembly: ranking, recommendation, diagnostics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_loop.macro_context import MacroContext

from ..regime import MarketRegime, MarketRegimeSnapshot
from ..rule_config import ScreeningRules, SelectionDiversityRules
from ..tiers import position_tier
from ._coerce import (
    _dict_sequence,
    _int_or,
    _mapping,
    _string_sequence,
    _string_value,
)
from .lenses import _candidate_lenses, _fast_lens
from .macro_fit import (
    _candidate_macro_context_result,
    _macro_context_alignment,
    _macro_context_summary,
    _macro_rank,
)
from .profiles import resolve_selection_rules
from .ranking import (
    RankingToggles,
    _best_selection_evidence,
    _lane_order_rank,
    _primary_evidence_by_lane_order,
    _sizing_eligible_evidence_hits,
)
from .records import (
    CandidateRecord,
    PreviousCandidates,
    PriorResearch,
    _evidence_metric_type_warnings,
    _numeric_metric_type_warnings,
)
from .summaries import (
    _candidate_reason_tags,
    _candidate_risk_tags,
    _long_hold_counts,
    _selection_candidate_summary,
    _sweep_candidate_summary,
    _sweep_changed_summaries,
)


def build_selection_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    macro_context: MacroContext | None,
    rules: ScreeningRules,
    top: int,
    profile: str | None,
    candidates_ref: str,
    macro_context_ref: str | None,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
    market_regime: MarketRegimeSnapshot | None = None,
    ranking_toggles: RankingToggles | None = None,
    detail: str = "summary",
) -> dict[str, object]:
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    effective_profile = profile or rules.selection.default_profile
    selection_rules = resolve_selection_rules(
        rules.selection,
        profile=effective_profile,
        profile_overrides=profile_overrides,
    )
    recommendation_limit = _research_recommendation_limit(
        top=top,
        configured_max=rules.output.research_selection_target_max,
    )
    prior_research_by_ticker = prior_research_by_ticker or {}
    previous_candidates = previous_candidates or PreviousCandidates(ref_path=None, tickers=())
    previous_tickers = set(previous_candidates.tickers)
    # The fast-dislocation boost buys falling knives at the top of the queue
    # while the whole market rallies (replay-2026-05: 4w relative consistently
    # negative). In risk_on_rally the boost is neutralized; candidates are kept
    # (lens, not gate) and other ranking components take over.
    fast_boost_active = (
        market_regime is None or market_regime.regime is not MarketRegime.RISK_ON_RALLY
    )
    toggles = ranking_toggles or RankingToggles()
    # Single source of truth for lane priority: the configured
    # research_selection_lane_order ranks both the queue and the primary
    # evidence pick. The old hard-coded _LANE_RANK put valuation-reversion
    # first, the inverse of the measured lane quality (lane-cohorts-2026-05).
    lane_order = tuple(rules.output.research_selection_lane_order)

    ranked_entries: list[tuple[tuple[object, ...], dict[str, object]]] = []
    macro_context_checked_count = 0

    for item in candidates:
        macro_context_checked_count += 1
        macro_context_result = _candidate_macro_context_result(
            item,
            macro_context=macro_context,
            asof_date=asof_date,
        )
        macro_context_alignment = _macro_context_alignment(macro_context_result)
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if not eligible_evidence_hits:
            continue
        selection_lane, selection_metrics, strength_key = _best_selection_evidence(
            eligible_evidence_hits,
            lane_order=lane_order,
        )
        prior_research = prior_research_by_ticker.get(item.ticker)
        suppress_reason = (
            prior_research.suppression_reason(asof_date) if prior_research is not None else None
        )
        lenses = _candidate_lenses(item, selection_rules)
        candidate = _selection_candidate(
            item,
            macro_context_result=macro_context_result,
            macro_context_alignment=macro_context_alignment,
            selection_lane=selection_lane,
            selection_metrics=selection_metrics,
            lenses=lenses,
            prior_research=prior_research,
            suppressed=suppress_reason is not None,
            suppression_reasons=(suppress_reason,) if suppress_reason else (),
            previous_candidate=item.ticker in previous_tickers,
        )
        fast_boosted = (
            toggles.fast_boost
            and fast_boost_active
            and _fast_lens(candidate).get("eligible") is True
        )
        sort_key = (
            _macro_rank(macro_context_alignment) if toggles.macro else 0,
            0 if fast_boosted else 1,
            _lane_order_rank(selection_lane, lane_order) if toggles.lane_rank else 0,
            *(strength_key if toggles.strength else ()),
            item.ticker,
        )
        ranked_entries.append((sort_key, candidate))

    ranked_entries.sort(key=lambda item: item[0])
    ranked_candidates = [candidate for _, candidate in ranked_entries]
    ranked_active_candidates = [
        candidate for candidate in ranked_candidates if not candidate["suppressed"]
    ]
    recommended = _recommended_research_candidates(
        ranked_candidates=ranked_active_candidates,
        lane_order=rules.output.research_selection_lane_order,
        diversity_rules=selection_rules.diversity,
        limit=recommendation_limit,
    )
    diagnostics = _diagnostics(
        recommended=recommended,
        ranked_candidates=ranked_candidates,
        previous_candidates=previous_candidates,
        diversity_warning_ratio=selection_rules.diversity.previous_overlap_warning_ratio,
        profile=effective_profile,
        market_regime=market_regime,
        fast_boost_active=fast_boost_active,
    )
    recommendations = (
        recommended
        if detail == "full"
        else [
            _selection_candidate_summary(candidate, rank=rank)
            for rank, candidate in enumerate(recommended, start=1)
        ]
    )
    return {
        "recommendations": recommendations,
        "selection": {
            "asof": asof_date.isoformat(),
            "profile": effective_profile,
            "input_refs": {
                "candidates_ref": candidates_ref,
                "macro_context_ref": macro_context_ref,
                "previous_candidates_ref": previous_candidates.ref_path,
            },
            "counts": {
                "input": len(candidates),
                "after_macro_context_check": macro_context_checked_count,
                "after_evidence_filter": len(ranked_candidates),
            },
            "research_selection_target_max": rules.output.research_selection_target_max,
            "research_selection_lane_order": list(rules.output.research_selection_lane_order),
            "macro_context_summary": _macro_context_summary(macro_context),
            "diagnostics": diagnostics,
            "detail": detail,
        },
    }


def build_selection_sweep_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    macro_context: MacroContext | None,
    rules: ScreeningRules,
    top: int,
    profiles: Sequence[str],
    candidates_ref: str,
    macro_context_ref: str | None,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
    market_regime: MarketRegimeSnapshot | None = None,
) -> dict[str, object]:
    profile_results: list[dict[str, object]] = []
    for profile in profiles:
        payload = build_selection_payload(
            asof_date=asof_date,
            candidates=candidates,
            macro_context=macro_context,
            rules=rules,
            top=top,
            profile=profile,
            candidates_ref=candidates_ref,
            macro_context_ref=macro_context_ref,
            previous_candidates=previous_candidates,
            prior_research_by_ticker=prior_research_by_ticker,
            profile_overrides=profile_overrides,
            market_regime=market_regime,
            detail="full",
        )
        selection = _mapping(payload.get("selection"))
        diagnostics = _mapping(selection.get("diagnostics"))
        recommended = _dict_sequence(payload.get("recommendations"))
        profile_results.append(
            {
                "profile": profile,
                "recommended": [
                    _sweep_candidate_summary(item, rank=index)
                    for index, item in enumerate(recommended, start=1)
                ],
                "recommended_tickers": [_string_value(item.get("ticker")) for item in recommended],
                "recommended_count": len(recommended),
                "fast_dislocation_count": _int_or(diagnostics.get("fast_dislocation_count"), 0),
                "long_hold_counts": dict(_mapping(diagnostics.get("long_hold_counts"))),
                "suppressed_count": _int_or(diagnostics.get("suppressed_count"), 0),
                "previous_overlap": diagnostics.get("previous_overlap"),
                "concentration": diagnostics.get("concentration"),
                "warnings": diagnostics.get("warnings"),
            }
        )
    if profile_results:
        base_tickers = set(_string_sequence(profile_results[0].get("recommended_tickers")))
        base_by_ticker = {
            ticker: item
            for item in _dict_sequence(profile_results[0].get("recommended"))
            if (ticker := _string_value(item.get("ticker"))) is not None
        }
        for result in profile_results:
            tickers = set(_string_sequence(result.get("recommended_tickers")))
            current_by_ticker = {
                ticker: item
                for item in _dict_sequence(result.get("recommended"))
                if (ticker := _string_value(item.get("ticker"))) is not None
            }
            result["recommended_diff_vs_first_profile"] = {
                "added": sorted(tickers - base_tickers),
                "removed": sorted(base_tickers - tickers),
                "changed": _sweep_changed_summaries(base_by_ticker, current_by_ticker),
            }
    return {
        "asof": asof_date.isoformat(),
        "input_refs": {
            "candidates_ref": candidates_ref,
            "macro_context_ref": macro_context_ref,
            "previous_candidates_ref": previous_candidates.ref_path
            if previous_candidates is not None
            else None,
        },
        "macro_context_summary": _macro_context_summary(macro_context),
        "market_regime": market_regime.to_dict() if market_regime is not None else None,
        "profiles": profile_results,
    }


def _selection_candidate(
    item: CandidateRecord,
    *,
    macro_context_result: Mapping[str, object],
    macro_context_alignment: str,
    selection_lane: str | None,
    selection_metrics: Mapping[str, object],
    lenses: Mapping[str, object],
    prior_research: PriorResearch | None,
    suppressed: bool,
    suppression_reasons: Sequence[str],
    previous_candidate: bool,
) -> dict[str, object]:
    output: dict[str, object] = {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": item.sector_33,
        "macro_context_alignment": macro_context_alignment,
        "macro_context": dict(macro_context_result),
        "market_cap_oku": item.market_cap_oku,
        "price_change_1d": item.price_change_1d,
        "price_change_5d": item.price_change_5d,
        "price_change_20d": item.price_change_20d,
        "price_change_60d": item.price_change_60d,
        "gap_from_52w_low": item.gap_from_52w_low,
        "turnover_spike_5d": item.turnover_spike_5d,
        "evidence_hits": list(item.evidence_hits),
        "freshness_warnings": list(item.freshness_warnings),
        "selection_lane": selection_lane,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(item.market_cap_oku),
        "lenses": dict(lenses),
        "prior_research": prior_research.to_dict() if prior_research is not None else None,
        "previous_candidate": previous_candidate,
        "suppressed": suppressed,
        "suppression_reasons": list(suppression_reasons),
    }
    metric_type_warnings = _numeric_metric_type_warnings(item.metrics, source="metrics")
    metric_type_warnings.extend(_evidence_metric_type_warnings(item.evidence_hits))
    if metric_type_warnings:
        output["metric_type_warnings"] = metric_type_warnings
    output["reason_tags"] = _candidate_reason_tags(output)
    output["risk_tags"] = _candidate_risk_tags(output)
    return output


def _recommended_research_candidates(
    *,
    ranked_candidates: Sequence[dict[str, object]],
    lane_order: Sequence[str],
    diversity_rules: SelectionDiversityRules,
    limit: int,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()
    sector_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    previous_candidate_count = 0

    def normalized_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
        selection_lane, selection_metrics = _primary_evidence_by_lane_order(
            candidate.get("evidence_hits"), lane_order
        )
        output = dict(candidate)
        if selection_lane is not None:
            output["selection_lane"] = selection_lane
            output["selection_metrics"] = selection_metrics
        return output

    def can_add(candidate: Mapping[str, object], *, enforce_diversity: bool) -> bool:
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            return False
        output = normalized_candidate(candidate)
        if not enforce_diversity:
            return True
        sector = _string_value(candidate.get("sector_33")) or ""
        lane = _string_value(output.get("selection_lane")) or ""
        max_sector = diversity_rules.max_recommended_per_sector
        max_lane = diversity_rules.max_recommended_per_lane
        max_previous = diversity_rules.max_previous_candidates_in_recommended
        if (
            max_previous is not None
            and candidate.get("previous_candidate") is True
            and previous_candidate_count >= max_previous
        ):
            return False
        return sector_counts[sector] < max_sector and lane_counts[lane] < max_lane

    def add(candidate: Mapping[str, object]) -> None:
        nonlocal previous_candidate_count
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None:
            return
        output = normalized_candidate(candidate)
        selected.append(output)
        selected_tickers.add(ticker)
        sector_counts[_string_value(candidate.get("sector_33")) or ""] += 1
        lane_counts[_string_value(output.get("selection_lane")) or ""] += 1
        if candidate.get("previous_candidate") is True:
            previous_candidate_count += 1

    for candidate in ranked_candidates:
        if len(selected) >= limit:
            break
        if can_add(candidate, enforce_diversity=True):
            add(candidate)
    return selected


def _diagnostics(
    *,
    recommended: Sequence[dict[str, object]],
    ranked_candidates: Sequence[dict[str, object]],
    previous_candidates: PreviousCandidates,
    diversity_warning_ratio: float,
    profile: str,
    market_regime: MarketRegimeSnapshot | None = None,
    fast_boost_active: bool = True,
) -> dict[str, object]:
    recommended_tickers = {
        ticker for item in recommended if (ticker := _string_value(item.get("ticker"))) is not None
    }
    previous_tickers = set(previous_candidates.tickers)
    overlap_tickers = sorted(recommended_tickers & previous_tickers)
    overlap_ratio = len(overlap_tickers) / len(recommended_tickers) if recommended_tickers else 0.0
    warnings = []
    if overlap_ratio >= diversity_warning_ratio and recommended_tickers:
        warnings.append("recommendations_high_previous_overlap")
    metric_type_warning_count = sum(
        len(_dict_sequence(candidate.get("metric_type_warnings")))
        for candidate in ranked_candidates
    )
    if metric_type_warning_count:
        warnings.append("invalid_numeric_metric_values")
    fast_data_status_counts = Counter(
        _string_value(_fast_lens(candidate).get("data_status")) or "unknown"
        for candidate in ranked_candidates
    )
    short_return_missing_count = sum(
        fast_data_status_counts[status]
        for status in ("missing_price_history", "short_return_pipeline_missing")
    )
    if short_return_missing_count:
        warnings.append("short_return_price_history_missing")
    if not fast_boost_active:
        warnings.append("fast_dislocation_boost_neutralized_risk_on_rally")
    return {
        "profile": profile,
        "warnings": warnings,
        "market_regime": market_regime.to_dict() if market_regime is not None else None,
        "fast_dislocation_boost": "active" if fast_boost_active else "neutralized",
        "previous_overlap": {
            "previous_candidates_ref": previous_candidates.ref_path,
            "overlap_count": len(overlap_tickers),
            "overlap_ratio": round(overlap_ratio, 4),
            "overlap_tickers": overlap_tickers,
        },
        "concentration": {
            "recommended_by_sector": dict(
                Counter(_string_value(item.get("sector_33")) or "" for item in recommended)
            ),
            "recommended_by_selection_lane": dict(
                Counter(_string_value(item.get("selection_lane")) or "" for item in recommended)
            ),
        },
        "suppressed_count": sum(1 for candidate in ranked_candidates if candidate["suppressed"]),
        "fast_dislocation_count": sum(
            1 for candidate in ranked_candidates if _fast_lens(candidate).get("eligible") is True
        ),
        "long_hold_counts": _long_hold_counts(ranked_candidates),
        "short_return_missing_candidate_count": short_return_missing_count,
        "fast_dislocation_data_status_counts": dict(fast_data_status_counts),
        "invalid_numeric_metric_value_count": metric_type_warning_count,
    }


def _research_recommendation_limit(*, top: int, configured_max: int) -> int:
    return min(top, configured_max) if configured_max > 0 else top
