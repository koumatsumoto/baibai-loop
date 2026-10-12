"""Research Triageの判断へ、production共通のModelInputを産むpure builder。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from baibai_batch.analysis.models import ModelInput, TriageCandidate
from baibai_batch.analysis.policy import TRIAGE_POLICY
from baibai_engine.batch_api import (
    PublishedReviewSet,
    ReviewSetEntrySnapshot,
    StoredAssessment,
    assessment_projection,
    legacy_context_projection,
    require_aware,
)


def review_set_candidates(review_set: PublishedReviewSet) -> tuple[TriageCandidate, ...]:
    """Project frozen entries in canonical order without current machine or judgment inputs."""
    return tuple(
        TriageCandidate(
            ticker=entry.ticker,
            snapshot=ReviewSetEntrySnapshot(
                name=entry.name,
                sector_33=entry.sector_33,
                nominations=entry.nominations,
                analysis=entry.analysis,
            ),
        )
        for entry in review_set.entries
    )


def build_model_input(
    review_set: PublishedReviewSet, macro_context: StoredAssessment | None, *, known_at: datetime
) -> ModelInput:
    require_aware(known_at)
    return ModelInput(
        schema_version=3,
        task="research-triage",
        instruction=(
            "Use only this JSON. Do not call tools or read files. "
            "Compare every candidate and return exactly one strict decision for each ticker."
        ),
        policy=TRIAGE_POLICY,
        as_of=review_set.as_of.isoformat(),
        known_at=known_at.isoformat(),
        macro_context=assessment_projection(macro_context, as_of=review_set.as_of),
        candidates=review_set_candidates(review_set),
    )


def extract_publication_input(
    review_set: PublishedReviewSet, macro_context: StoredAssessment | None
) -> dict[str, Any]:
    """Quote stored publications; never claim this is the saved runner input.json."""
    current = macro_context is not None and macro_context.schema_version == 5
    return {
        "schema_version": 3
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
        "policy": list(TRIAGE_POLICY),
        "as_of": review_set.as_of.isoformat(),
        "macro_context": assessment_projection(macro_context, as_of=review_set.as_of)
        if current
        else legacy_context_projection(macro_context, as_of=review_set.as_of),
        "candidates": [item.model_dump(mode="json") for item in review_set_candidates(review_set)],
    }
