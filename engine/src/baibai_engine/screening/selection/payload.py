"""Selection payload assembly: ranking, review cap, and diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.foundation.coerce import (
    dict_sequence,
    mapping_or_empty,
    optional_float,
    string_or_none,
)
from baibai_engine.macro.context import MacroContext
from baibai_engine.screening.estimates import EXPECTED_RETURN_MODEL_VERSION

from ..regime import MarketRegimeSnapshot
from ..rule_config import (
    ScreeningRules,
    SelectionLiquidityRules,
)
from ..schema import UNRESOLVED_DIVIDEND_BASIS
from ..tiers import position_tier
from .candidate_diagnostics import _candidate_diagnostics
from .contracts import (
    SelectionMethodParameters,
    selection_method_hash,
)
from .macro_fit import (
    macro_context_summary,
)
from .ranking import (
    _best_selection_evidence,
    _evidence_pattern_order_rank,
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
    _fair_value_anchors,
    _ranked_set_summary,
)


def build_selection_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    macro_context: MacroContext | None,
    rules: ScreeningRules,
    review_cap: int,
    candidates_ref: str,
    macro_context_ref: str | None,
    previous_candidates: PreviousCandidates | None = None,
    market_regime: MarketRegimeSnapshot | None = None,
    detail: str = "summary",
    screening_rules_hash: str | None = None,
    er_model_version: str | None = None,
    review_basis_shortlist_id: str | None = None,
) -> dict[str, object]:
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    if review_cap < 0:
        raise ValueError("review_cap must be non-negative")
    selection_rules = rules.selection
    previous_candidates = previous_candidates or PreviousCandidates(
        ref_path=None, source=None, tickers=()
    )
    previous_tickers = set(previous_candidates.tickers)
    # Research Gate は候補の対 benchmark 20 日相対リターンを参考にするため、
    # market regime snapshot が持つ benchmark return を候補へ機械転記する。
    # snapshot が無ければ null に degrade する。
    benchmark_return_20d = market_regime.benchmark_return_20d if market_regime else None
    # The configured order ranks both the queue and the primary Evidence Pattern.
    evidence_pattern_order = tuple(rules.output.evidence_pattern_order)

    liquidity = selection_rules.liquidity
    required_jpx_flags = frozenset(rules.universe.required_jpx_flags)
    liquidity_excluded_count = 0
    liquidity_fact_missing_count = 0
    evidence_annotated_count = 0
    er_missing_count = 0
    unresolved_dividend_basis: list[str] = []
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
            if item.metrics.get("dividend_basis") == UNRESOLVED_DIVIDEND_BASIS:
                unresolved_dividend_basis.append(item.ticker)
            continue
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if eligible_evidence_hits:
            evidence_annotated_count += 1
        primary_evidence_pattern_id, selection_metrics, strength_key = _best_selection_evidence(
            eligible_evidence_hits,
            evidence_pattern_order=evidence_pattern_order,
        )
        candidate_diagnostics = _candidate_diagnostics(item, selection_rules.candidate_diagnostics)
        candidate = _selection_candidate(
            item,
            primary_evidence_pattern_id=primary_evidence_pattern_id,
            selection_metrics=selection_metrics,
            candidate_diagnostics=candidate_diagnostics,
            previous_candidate=item.ticker in previous_tickers,
            benchmark_return_20d=benchmark_return_20d,
        )
        candidate["decision_input_seed"] = _decision_input_seed(candidate, asof_date=asof_date)
        # 主キーは機械 E[r] (成分分解付き見積り) の降順:「どれくらいお買い得か」の
        # 見積りが着手順位を決める。E[r] 欠損の
        # 候補はranking対象外とし、従キーとしてEvidence Pattern優先順 + 各screenの
        # 強度キーを残す。macro context は診断 annotation であり順位には使わない。
        sort_key = (
            -er_annual,
            _evidence_pattern_order_rank(primary_evidence_pattern_id, evidence_pattern_order),
            *strength_key,
            item.ticker,
        )
        ranked_entries.append((sort_key, candidate))

    ranked_entries.sort(key=lambda item: item[0])
    ranked_candidates = [candidate for _, candidate in ranked_entries]
    ranked_set_candidates = ranked_candidates[:review_cap]
    diagnostics = _diagnostics(
        ranked_set=ranked_set_candidates,
        ranked_candidates=ranked_candidates,
        previous_candidates=previous_candidates,
        market_regime=market_regime,
        liquidity_excluded_count=liquidity_excluded_count,
        liquidity_fact_missing_count=liquidity_fact_missing_count,
    )
    expected_return_model_id = er_model_version or EXPECTED_RETURN_MODEL_VERSION
    method_parameters = SelectionMethodParameters(
        review_cap=review_cap,
        expected_return_model_id=expected_return_model_id,
        screening_rules_hash=screening_rules_hash,
        required_jpx_flags=tuple(sorted(required_jpx_flags)),
        liquidity_parameters=liquidity,
        candidate_diagnostic_parameters=selection_rules.candidate_diagnostics,
        evidence_pattern_order=tuple(evidence_pattern_order),
    )
    method_hash = selection_method_hash(method_parameters)
    ranked_set = [
        {
            **_ranked_set_summary(candidate, rank=rank),
            "er_annual": optional_float(
                mapping_or_empty(candidate.get("metrics")).get("er_annual")
            ),
            "primary_evidence_pattern_id": string_or_none(
                candidate.get("primary_evidence_pattern_id")
            ),
        }
        for rank, candidate in enumerate(ranked_candidates[:review_cap], start=1)
    ]
    payload: dict[str, object] = {
        "ranked_set": ranked_set,
        "method_hash": method_hash,
        "method_parameters": method_parameters.model_dump(mode="json"),
        "review_basis": {
            "judged_through_shortlist_id": review_basis_shortlist_id,
        },
    }
    payload["selection"] = {
        "asof": asof_date.isoformat(),
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
            "er_missing_unresolved_dividend_basis": sorted(unresolved_dividend_basis),
        },
        "research_selection_target_max": rules.output.research_selection_target_max,
        "evidence_pattern_order": list(rules.output.evidence_pattern_order),
        "macro_context_summary": macro_context_summary(macro_context, asof_date=asof_date),
        "diagnostics": diagnostics,
        "detail": detail,
        "screening_rules_hash": screening_rules_hash,
        "er_model_version": expected_return_model_id,
    }
    return payload


def _selection_candidate(
    item: CandidateRecord,
    *,
    primary_evidence_pattern_id: str | None,
    selection_metrics: Mapping[str, object],
    candidate_diagnostics: Mapping[str, object],
    previous_candidate: bool,
    benchmark_return_20d: float | None = None,
) -> dict[str, object]:
    benchmark_relative_20d = (
        item.price_change_20d - benchmark_return_20d
        if item.price_change_20d is not None and benchmark_return_20d is not None
        else None
    )
    metrics = dict(item.metrics)
    valid_fair_values = _fair_value_anchors(metrics)
    for key in ("fv_sector_median_yen", "fv_self_range_yen"):
        metrics[key] = valid_fair_values.get(key)
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
        "metrics": metrics,
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
        # 悪化ゲートが判定材料を持たないまま通した銘柄。通過は「悪化していない」
        # ことの観測ではないので、risk tag の入力として候補へ載せる。
        "deterioration_gate_unmeasurable": item.metrics.get("deterioration_gate_unmeasurable")
        is True,
        "evidence_hits": list(item.evidence_hits),
        "freshness_warnings": list(item.freshness_warnings),
        "primary_evidence_pattern_id": primary_evidence_pattern_id,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(item.market_cap_oku),
        "candidate_diagnostics": dict(candidate_diagnostics),
        "previous_candidate": previous_candidate,
    }
    metric_type_warnings = _numeric_metric_type_warnings(item.metrics, source="metrics")
    metric_type_warnings.extend(_evidence_metric_type_warnings(item.evidence_hits))
    if metric_type_warnings:
        output["metric_type_warnings"] = metric_type_warnings
    output["reason_tags"] = _candidate_reason_tags(output)
    output["risk_tags"] = _candidate_risk_tags(output)
    return output


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


def _diagnostics(
    *,
    ranked_set: Sequence[dict[str, object]],
    ranked_candidates: Sequence[dict[str, object]],
    previous_candidates: PreviousCandidates,
    market_regime: MarketRegimeSnapshot | None = None,
    liquidity_excluded_count: int = 0,
    liquidity_fact_missing_count: int = 0,
) -> dict[str, object]:
    ranked_tickers = {
        ticker for item in ranked_set if (ticker := string_or_none(item.get("ticker"))) is not None
    }
    previous_tickers = set(previous_candidates.tickers)
    overlap_tickers = sorted(ranked_tickers & previous_tickers)
    overlap_ratio = len(overlap_tickers) / len(ranked_tickers) if ranked_tickers else 0.0
    metric_type_warning_count = sum(
        len(dict_sequence(candidate.get("metric_type_warnings"))) for candidate in ranked_candidates
    )
    warnings: list[str] = []
    if metric_type_warning_count:
        warnings.append("invalid_numeric_metric_values")
    return {
        "warnings": warnings,
        "market_regime": market_regime.to_dict() if market_regime is not None else None,
        "liquidity_excluded_count": liquidity_excluded_count,
        "liquidity_fact_missing_count": liquidity_fact_missing_count,
        "previous_overlap": {
            "previous_candidates_ref": previous_candidates.ref_path,
            # The two sources have different population sizes, so the ratio below is
            # only comparable across runs that read the same one.
            "previous_candidates_source": previous_candidates.source,
            "previous_candidates_count": len(previous_tickers),
            # The count is against the previous set and the ratio is against this run's
            # ranked set, so the two divide by different things. Naming both
            # denominators keeps them from being compared as the same fraction.
            "ranked_count": len(ranked_tickers),
            "overlap_count": len(overlap_tickers),
            "overlap_share_of_ranked_set": round(overlap_ratio, 4),
        },
        "durability_counts": _durability_counts(ranked_candidates),
        "invalid_numeric_metric_value_count": metric_type_warning_count,
    }
