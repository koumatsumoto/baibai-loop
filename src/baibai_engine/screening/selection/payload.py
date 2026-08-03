"""Selection payload assembly: ranking, recommendation, diagnostics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.foundation.coerce import (
    dict_sequence,
    mapping_or_empty,
    optional_float,
    string_or_none,
    string_sequence,
)
from baibai_engine.macro.context import MacroContext

from ..regime import MarketRegimeSnapshot
from ..rule_config import (
    ScreeningRules,
    SelectionDiversityRules,
    SelectionLiquidityRules,
    SelectionRules,
)
from ..tiers import position_tier
from .lenses import _candidate_lenses
from .macro_fit import (
    macro_context_summary,
)
from .profiles import resolve_selection_rules
from .ranking import (
    _best_selection_evidence,
    _playbook_order_rank,
    _sizing_eligible_evidence_hits,
)
from .records import (
    CandidateRecord,
    PreviousCandidates,
    _evidence_metric_type_warnings,
    _numeric_metric_type_warnings,
)
from .summaries import (
    _candidate_reason_tags,
    _candidate_risk_tags,
    _decision_input_seed,
    _durability_counts,
    _longlist_summary,
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
    market_regime: MarketRegimeSnapshot | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
    detail: str = "summary",
    longlist_top: int = 0,
) -> dict[str, object]:
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    if longlist_top < 0:
        raise ValueError("longlist_top must be zero or greater")
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
    previous_candidates = previous_candidates or PreviousCandidates(ref_path=None, tickers=())
    previous_tickers = set(previous_candidates.tickers)
    # Entry preflight は候補の対 benchmark 20 日相対リターンを情報として使うため、
    # market regime snapshot が持つ benchmark return を候補へ機械転記する。
    # snapshot が無ければ null に degrade する。
    benchmark_return_20d = market_regime.benchmark_return_20d if market_regime else None
    # Single source of truth for playbook priority: the configured
    # research_selection_playbook_order ranks both the queue and the primary
    # evidence pick.
    playbook_order = tuple(rules.output.research_selection_playbook_order)

    liquidity = selection_rules.liquidity
    required_jpx_flags = frozenset(rules.universe.required_jpx_flags)
    liquidity_excluded_count = 0
    liquidity_fact_missing_count = 0
    evidence_annotated_count = 0
    er_missing_count = 0

    ranked_entries: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for item in candidates:
        passes, facts_missing = _passes_liquidity(item, liquidity, required_jpx_flags)
        if facts_missing:
            liquidity_fact_missing_count += 1
        if not passes:
            liquidity_excluded_count += 1
            continue
        er_annual = optional_float(item.metrics.get("er_annual"))
        if er_annual is None:
            er_missing_count += 1
            continue
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if eligible_evidence_hits:
            evidence_annotated_count += 1
        selection_playbook, selection_metrics, strength_key = _best_selection_evidence(
            eligible_evidence_hits,
            playbook_order=playbook_order,
        )
        lenses = _candidate_lenses(item, selection_rules)
        candidate = _selection_candidate(
            item,
            selection_playbook=selection_playbook,
            selection_metrics=selection_metrics,
            lenses=lenses,
            previous_candidate=item.ticker in previous_tickers,
            benchmark_return_20d=benchmark_return_20d,
        )
        candidate["decision_input_seed"] = _decision_input_seed(candidate, asof_date=asof_date)
        # 主キーは機械 E[r] (成分分解付き見積り) の降順:「どれくらいお買い得か」の
        # 見積りが着手順位を決める。E[r] 欠損の
        # 候補は ranking 対象外とし、従キーとして playbook 優先順 + 各 screen の
        # 強度キーを残す。macro context は診断 annotation であり順位には使わない。
        sort_key = (
            -er_annual,
            _playbook_order_rank(selection_playbook, playbook_order),
            *strength_key,
            item.ticker,
        )
        ranked_entries.append((sort_key, candidate))

    ranked_entries.sort(key=lambda item: item[0])
    ranked_candidates = [candidate for _, candidate in ranked_entries]
    recommendation_candidates = [
        candidate
        for candidate in ranked_candidates
        if _passes_supply_demand(candidate, selection_rules)
    ]
    recommended = _recommended_research_candidates(
        ranked_candidates=recommendation_candidates,
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
        liquidity_excluded_count=liquidity_excluded_count,
        liquidity_fact_missing_count=liquidity_fact_missing_count,
        supply_demand_excluded_count=len(ranked_candidates) - len(recommendation_candidates),
    )
    recommendations = (
        recommended
        if detail == "full"
        else [
            _selection_candidate_summary(candidate, rank=rank)
            for rank, candidate in enumerate(recommended, start=1)
        ]
    )
    payload: dict[str, object] = {
        "recommendations": recommendations,
    }
    # longlist は監査用の追加 view。--longlist-top 省略 (0) では既存 output 互換のため
    # key 自体を出さない。出す場合は同じ rank 済み集合 (diversity/cap 切断前) の先頭
    # N 件で、recommendation の production cap とは独立に監査できるようにする。
    if longlist_top > 0:
        payload["longlist"] = [
            _longlist_summary(candidate, rank=rank)
            for rank, candidate in enumerate(ranked_candidates[:longlist_top], start=1)
        ]
    payload["selection"] = {
        "asof": asof_date.isoformat(),
        "profile": effective_profile,
        "input_refs": {
            "candidates_ref": candidates_ref,
            "macro_context_ref": macro_context_ref,
            "previous_candidates_ref": previous_candidates.ref_path,
        },
        "counts": {
            "input": len(candidates),
            "after_liquidity_filter": len(candidates) - liquidity_excluded_count,
            "after_er_filter": len(ranked_candidates),
            "evidence_annotated": evidence_annotated_count,
            "er_missing": er_missing_count,
        },
        "research_selection_target_max": rules.output.research_selection_target_max,
        "research_selection_playbook_order": list(rules.output.research_selection_playbook_order),
        "macro_context_summary": macro_context_summary(macro_context, asof_date=asof_date),
        "diagnostics": diagnostics,
        "detail": detail,
    }
    return payload


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
            market_regime=market_regime,
            detail="full",
        )
        selection = mapping_or_empty(payload.get("selection"))
        diagnostics = mapping_or_empty(selection.get("diagnostics"))
        recommended = dict_sequence(payload.get("recommendations"))
        profile_results.append(
            {
                "profile": profile,
                "recommended": [
                    _sweep_candidate_summary(item, rank=index)
                    for index, item in enumerate(recommended, start=1)
                ],
                "recommended_tickers": [string_or_none(item.get("ticker")) for item in recommended],
                "recommended_count": len(recommended),
                "durability_counts": dict(mapping_or_empty(diagnostics.get("durability_counts"))),
                "warnings": diagnostics.get("warnings"),
            }
        )
    if profile_results:
        base_tickers = set(string_sequence(profile_results[0].get("recommended_tickers")))
        base_by_ticker = {
            ticker: item
            for item in dict_sequence(profile_results[0].get("recommended"))
            if (ticker := string_or_none(item.get("ticker"))) is not None
        }
        for result in profile_results:
            tickers = set(string_sequence(result.get("recommended_tickers")))
            current_by_ticker = {
                ticker: item
                for item in dict_sequence(result.get("recommended"))
                if (ticker := string_or_none(item.get("ticker"))) is not None
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
        "macro_context_summary": macro_context_summary(macro_context, asof_date=asof_date),
        "market_regime": market_regime.to_dict() if market_regime is not None else None,
        "profiles": profile_results,
    }


def _selection_candidate(
    item: CandidateRecord,
    *,
    selection_playbook: str | None,
    selection_metrics: Mapping[str, object],
    lenses: Mapping[str, object],
    previous_candidate: bool,
    benchmark_return_20d: float | None = None,
) -> dict[str, object]:
    benchmark_relative_20d = (
        item.price_change_20d - benchmark_return_20d
        if item.price_change_20d is not None and benchmark_return_20d is not None
        else None
    )
    output: dict[str, object] = {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": item.sector_33,
        "market_cap_oku": item.market_cap_oku,
        "avg_turnover_oku": item.avg_turnover_oku,
        "per_trailing": item.per_trailing,
        "per_forward": item.per_forward,
        "pbr": item.pbr,
        "ev_ebitda": item.ev_ebitda,
        "p_s": item.p_s,
        "pcfr": item.pcfr,
        "metrics": dict(item.metrics),
        "price_change_1d": item.price_change_1d,
        "price_change_5d": item.price_change_5d,
        "price_change_20d": item.price_change_20d,
        "price_change_60d": item.price_change_60d,
        "benchmark_relative_20d": benchmark_relative_20d,
        "listing_span_days": item.listing_span_days,
        "price_history_coverage_750d": item.price_history_coverage_750d,
        "gap_from_52w_low": item.gap_from_52w_low,
        "turnover_spike_5d": item.turnover_spike_5d,
        "split_adjustment_flag": item.split_adjustment_flag,
        "evidence_hits": list(item.evidence_hits),
        "freshness_warnings": list(item.freshness_warnings),
        "selection_playbook": selection_playbook,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(item.market_cap_oku),
        "lenses": dict(lenses),
        "previous_candidate": previous_candidate,
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
    diversity_rules: SelectionDiversityRules,
    limit: int,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()
    sector_counts: Counter[str] = Counter()
    playbook_counts: Counter[str] = Counter()
    previous_candidate_count = 0

    def can_add(candidate: Mapping[str, object], *, enforce_diversity: bool) -> bool:
        ticker = string_or_none(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            return False
        if not enforce_diversity:
            return True
        sector = string_or_none(candidate.get("sector_33")) or ""
        # The ranking pass already chose this candidate's playbook from the same
        # order; re-deriving it here would let the two disagree on which screen a
        # candidate counts against for the per-playbook diversity cap.
        playbook = string_or_none(candidate.get("selection_playbook"))
        max_sector = diversity_rules.max_recommended_per_sector
        max_playbook = diversity_rules.max_recommended_per_playbook
        max_previous = diversity_rules.max_previous_candidates_in_recommended
        if (
            max_previous is not None
            and candidate.get("previous_candidate") is True
            and previous_candidate_count >= max_previous
        ):
            return False
        if sector_counts[sector] >= max_sector:
            return False
        return playbook is None or playbook_counts[playbook] < max_playbook

    def add(candidate: Mapping[str, object]) -> None:
        nonlocal previous_candidate_count
        ticker = string_or_none(candidate.get("ticker"))
        if ticker is None:
            return
        selected.append(dict(candidate))
        selected_tickers.add(ticker)
        sector_counts[string_or_none(candidate.get("sector_33")) or ""] += 1
        if (playbook := string_or_none(candidate.get("selection_playbook"))) is not None:
            playbook_counts[playbook] += 1
        if candidate.get("previous_candidate") is True:
            previous_candidate_count += 1

    for candidate in ranked_candidates:
        if len(selected) >= limit:
            break
        if can_add(candidate, enforce_diversity=True):
            add(candidate)
    return selected


def _passes_liquidity(
    item: CandidateRecord,
    liquidity: SelectionLiquidityRules,
    required_jpx_flags: frozenset[str],
) -> tuple[bool, bool]:
    """Apply the analysis-layer scope parameters to one candidate.

    Returns ``(passes, facts_missing)``. Missing liquidity facts are counted so
    degraded inputs stay visible in diagnostics, and are excluded because the
    selection population is the same liquid population used by calibration.
    """
    facts_missing = (
        item.market_cap_oku is None
        or item.avg_turnover_oku is None
        or item.listing_span_days is None
        or item.jpx_flags is None
    )
    passes = liquidity.matches(
        market_cap_oku=item.market_cap_oku,
        avg_turnover_oku=item.avg_turnover_oku,
        listing_span_days=item.listing_span_days,
        jpx_flags=item.jpx_flags,
        required_jpx_flags=required_jpx_flags,
        require_facts=True,
    )
    return passes, facts_missing


def _passes_supply_demand(candidate: Mapping[str, object], rules: SelectionRules) -> bool:
    threshold = rules.supply_demand.margin_std_long_share_exclude_at_or_above
    if threshold is None:
        return True
    metrics = mapping_or_empty(candidate.get("metrics"))
    value = optional_float(metrics.get("margin_std_long_share"))
    return value is None or value < threshold


def _diagnostics(
    *,
    recommended: Sequence[dict[str, object]],
    ranked_candidates: Sequence[dict[str, object]],
    previous_candidates: PreviousCandidates,
    diversity_warning_ratio: float,
    profile: str,
    market_regime: MarketRegimeSnapshot | None = None,
    liquidity_excluded_count: int = 0,
    liquidity_fact_missing_count: int = 0,
    supply_demand_excluded_count: int = 0,
) -> dict[str, object]:
    recommended_tickers = {
        ticker for item in recommended if (ticker := string_or_none(item.get("ticker"))) is not None
    }
    previous_tickers = set(previous_candidates.tickers)
    overlap_tickers = sorted(recommended_tickers & previous_tickers)
    overlap_ratio = len(overlap_tickers) / len(recommended_tickers) if recommended_tickers else 0.0
    warnings = []
    if overlap_ratio >= diversity_warning_ratio and recommended_tickers:
        warnings.append("recommendations_high_previous_overlap")
    metric_type_warning_count = sum(
        len(dict_sequence(candidate.get("metric_type_warnings"))) for candidate in ranked_candidates
    )
    if metric_type_warning_count:
        warnings.append("invalid_numeric_metric_values")
    return {
        "profile": profile,
        "warnings": warnings,
        "market_regime": market_regime.to_dict() if market_regime is not None else None,
        "liquidity_excluded_count": liquidity_excluded_count,
        "liquidity_fact_missing_count": liquidity_fact_missing_count,
        "supply_demand_excluded_count": supply_demand_excluded_count,
        "previous_overlap": {
            "previous_candidates_ref": previous_candidates.ref_path,
            "overlap_count": len(overlap_tickers),
            "overlap_ratio": round(overlap_ratio, 4),
        },
        "durability_counts": _durability_counts(ranked_candidates),
        "invalid_numeric_metric_value_count": metric_type_warning_count,
    }


def _research_recommendation_limit(*, top: int, configured_max: int) -> int:
    return min(top, configured_max) if configured_max > 0 else top
