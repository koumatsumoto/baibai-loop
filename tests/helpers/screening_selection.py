"""Canonical Value / Carry selection fixtures for writer and consumer tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from baibai_engine.screening.rule_config import CandidateDiagnosticRules
from baibai_engine.screening.selection.contracts import (
    VALUE_CARRY_ONLY_ATTENTION_POLICY_ID,
    VALUE_CARRY_OPPORTUNITY_LANE_ID,
    VALUE_CARRY_SELECTION_POLICY_ID,
    ValueCarryOnlyAttentionParameters,
    ValueCarrySelectionPolicyParameters,
    value_carry_expected_longlist,
    value_carry_only_attention_policy_hash,
    value_carry_selection_policy_hash,
)


def value_carry_selection_payload(
    *,
    ticker: str,
    er_annual: float,
    rules_hash: str,
    model_id: str = "expected-return-v1",
    asof: str = "2026-07-08",
    profile: str = "default",
    candidates_ref: str = "run-revision-fixture",
    macro_context_ref: str | None = None,
    longlist: Sequence[tuple[str, float]] | None = None,
    source_candidates: Sequence[Mapping[str, object]] | None = None,
    recommendations: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Build the smallest exact selection accepted by the new-write boundary."""

    ranked = tuple(longlist or ((ticker, er_annual),))
    resolved_sources = tuple(source_candidates or ()) or tuple(
        {
            "ticker": row_ticker,
            "market_cap_oku": 1.0,
            "avg_turnover_oku": 1.0,
            "listing_span_days": 1,
            "jpx_flags": [],
            "metrics": {"er_annual": row_er},
            "evidence_hits": [],
        }
        for row_ticker, row_er in ranked
    )
    policy_parameters = ValueCarrySelectionPolicyParameters(
        lane_longlist_depth=len(ranked),
        expected_return_model_id=model_id,
        screening_rules_hash=rules_hash,
        required_jpx_flags=(),
        liquidity_parameters={
            "min_market_cap_oku": 0.0,
            "min_avg_turnover_oku": 0.0,
            "min_listing_span_days": 0,
            "exclude_jpx_flagged": False,
        },
        candidate_diagnostic_parameters=CandidateDiagnosticRules(),
        evidence_pattern_order=(),
    )
    projection_by_ticker = {
        expected_ticker: projection
        for expected_ticker, _, _, projection in value_carry_expected_longlist(
            resolved_sources,
            parameters=policy_parameters,
            asof_date=asof,
        )
    }
    policy_hash = value_carry_selection_policy_hash(**policy_parameters.model_dump(mode="python"))
    parameters = ValueCarryOnlyAttentionParameters(value_carry_limit=len(ranked))
    selection = {
        "asof": asof,
        "profile": profile,
        "input_refs": {
            "candidates_ref": candidates_ref,
            "macro_context_ref": macro_context_ref,
            "previous_candidates_ref": None,
        },
        "screening_rules_hash": rules_hash,
        "er_model_version": model_id,
    }
    return {
        "recommendations": [dict(row) for row in recommendations]
        if recommendations is not None
        else [{"ticker": ticker}],
        "longlist_origin": {
            "opportunity_lane_id": VALUE_CARRY_OPPORTUNITY_LANE_ID,
            "selection_policy_id": VALUE_CARRY_SELECTION_POLICY_ID,
            "selection_policy_hash": policy_hash,
        },
        "selection_policy_parameters": policy_parameters.model_dump(mode="json"),
        "attention_policy_id": VALUE_CARRY_ONLY_ATTENTION_POLICY_ID,
        "attention_policy_hash": value_carry_only_attention_policy_hash(
            selection_policy_hash=policy_hash,
            parameters=parameters,
        ),
        "attention_policy_parameters": parameters.model_dump(mode="json"),
        "review_basis": {"judged_through_shortlist_id": None},
        "review_tickers": [row_ticker for row_ticker, _ in ranked],
        "longlist": [
            {
                "ticker": row_ticker,
                "rank": rank,
                "expected_return_pct": row_er * 100,
                "opportunity_lane_id": VALUE_CARRY_OPPORTUNITY_LANE_ID,
                "selection_policy_id": VALUE_CARRY_SELECTION_POLICY_ID,
                "selection_policy_hash": policy_hash,
                "lane_rank": rank,
                "lane_native_value": row_er,
                "lane_native_unit": "annual_ratio",
                "baseline_er_rank": rank,
                "policy_diagnostic_ids": [],
                **projection_by_ticker[row_ticker],
            }
            for rank, (row_ticker, row_er) in enumerate(ranked, start=1)
        ],
        "selection": selection,
    }
