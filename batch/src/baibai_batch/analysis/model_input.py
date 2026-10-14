"""Research Triageの判断へ、production共通のModelInputを産むpure builder。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from baibai_batch.analysis.models import ModelInput, TriageCandidate
from baibai_batch.analysis.policy import HISTORICAL_TRIAGE_POLICY, TRIAGE_POLICY
from baibai_engine.batch_api import (
    PublishedReviewSet,
    ReviewSetEntrySnapshot,
    StoredAssessment,
    assessment_projection,
    interpreted_next_earnings_status,
    legacy_context_projection,
    require_aware,
)


def review_set_candidates(review_set: PublishedReviewSet) -> tuple[TriageCandidate, ...]:
    """Project frozen entries without turning legacy date-only status into disclosure fact."""
    candidates: list[TriageCandidate] = []
    for entry in review_set.entries:
        context = entry.analysis.context.model_copy(
            update={
                "next_earnings_status": interpreted_next_earnings_status(
                    entry.analysis.context.next_earnings_status
                )
            }
        )
        candidates.append(
            TriageCandidate(
                ticker=entry.ticker,
                snapshot=ReviewSetEntrySnapshot(
                    name=entry.name,
                    sector_33=entry.sector_33,
                    nominations=entry.nominations,
                    analysis=entry.analysis.model_copy(update={"context": context}),
                ),
            )
        )
    return tuple(candidates)


def build_model_input(
    review_set: PublishedReviewSet,
    market_context_assessment: StoredAssessment | None,
    *,
    known_at: datetime,
) -> ModelInput:
    require_aware(known_at)
    return ModelInput(
        schema_version=4,
        task="research-triage",
        instruction=(
            "Use only this JSON. Do not call tools or read files. "
            "Compare every candidate and return exactly one strict decision for each ticker. "
            "Missing carry inputs contribute zero in machine E[r]; missing or absent "
            "component metadata is not evidence of zero dividends or unchanged shares. "
            "A reached earnings schedule and stale_fin_flag do not confirm a disclosure."
        ),
        policy=TRIAGE_POLICY,
        as_of=review_set.as_of.isoformat(),
        known_at=known_at.isoformat(),
        market_context_assessment=assessment_projection(
            market_context_assessment, as_of=review_set.as_of
        ),
        candidates=review_set_candidates(review_set),
    )


def extract_publication_input(
    review_set: PublishedReviewSet,
    market_context_assessment: StoredAssessment | None,
    *,
    legacy_triage: bool = False,
) -> dict[str, Any]:
    """Quote stored publications; never claim this is the saved runner input.json."""
    current = (
        market_context_assessment is not None and market_context_assessment.schema_version in (5, 6)
    )
    return {
        "schema_version": 4
        if not legacy_triage
        else 3
        if current
        else 2
        if any(
            "ttm_quality_ev_ebitda" in entry.analysis.data_quality.model_fields_set
            or "ttm_quality_fcf" in entry.analysis.data_quality.model_fields_set
            for entry in review_set.entries
        )
        else 1,
        "task": "research-triage",
        "instruction": (
            "Use only this JSON. Do not call tools or read files. "
            "Compare every candidate and return exactly one strict decision for each ticker."
        ),
        "policy": list(HISTORICAL_TRIAGE_POLICY if legacy_triage else TRIAGE_POLICY),
        "as_of": review_set.as_of.isoformat(),
        ("macro_context" if legacy_triage else "market_context_assessment"): assessment_projection(
            market_context_assessment, as_of=review_set.as_of
        )
        if current
        else legacy_context_projection(market_context_assessment, as_of=review_set.as_of),
        "candidates": [item.model_dump(mode="json") for item in review_set_candidates(review_set)],
    }
