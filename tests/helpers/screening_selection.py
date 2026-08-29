"""Current ranked-selection fixtures for writer and consumer tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from baibai_engine.screening.rule_config import CandidateDiagnosticRules
from baibai_engine.screening.selection.contracts import (
    SelectionMethodParameters,
    expected_ranked_set,
    selection_method_hash,
)


def ranked_selection_payload(
    *,
    ticker: str,
    er_annual: float,
    rules_hash: str,
    model_id: str = "expected-return-v1",
    asof: str = "2026-07-08",
    profile: str = "default",
    candidates_ref: str = "run-revision-fixture",
    macro_context_ref: str | None = None,
    ranked_set: Sequence[tuple[str, float]] | None = None,
    source_candidates: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    ranked = tuple(ranked_set) if ranked_set is not None else ((ticker, er_annual),)
    sources = tuple(source_candidates or ()) or tuple(
        {
            "ticker": item_ticker,
            "market_cap_oku": 1.0,
            "avg_turnover_oku": 1.0,
            "listing_span_days": 1,
            "jpx_flags": [],
            "metrics": {"er_annual": item_er},
            "evidence_hits": [],
        }
        for item_ticker, item_er in ranked
    )
    parameters = SelectionMethodParameters(
        review_cap=len(ranked),
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
    projection = {
        expected_ticker: values
        for expected_ticker, _, _, values in expected_ranked_set(
            sources, parameters=parameters, asof_date=asof
        )
    }
    return {
        "ranked_set": [
            {
                "ticker": item_ticker,
                "rank": rank,
                "expected_return_pct": item_er * 100,
                "er_annual": item_er,
                "primary_evidence_pattern_id": None,
                **projection[item_ticker],
            }
            for rank, (item_ticker, item_er) in enumerate(ranked, start=1)
        ],
        "method_hash": selection_method_hash(parameters),
        "method_parameters": parameters.model_dump(mode="json"),
        "review_basis": {"judged_through_shortlist_id": None},
        "selection": {
            "asof": asof,
            "profile": profile,
            "input_refs": {
                "candidates_ref": candidates_ref,
                "macro_context_ref": macro_context_ref,
                "previous_candidates_ref": None,
            },
            "screening_rules_hash": rules_hash,
            "er_model_version": model_id,
        },
    }


__all__ = ["ranked_selection_payload"]
