"""Research Triageの判断へ、production共通のModelInputを産むpure builder。"""

from __future__ import annotations

from baibai_batch.analysis.models import MacroProjection, ModelInput, TriageCandidate
from baibai_batch.analysis.policy import TRIAGE_POLICY
from baibai_engine.batch_api import (
    MACRO_CONTEXT_STALE_DAYS,
    MacroContextDocument,
    PublishedReviewSet,
    ReviewSetEntrySnapshot,
)


def macro_projection(
    review_set: PublishedReviewSet, document: MacroContextDocument | None
) -> MacroProjection:
    if document is None:
        return MacroProjection(status="missing")
    age_days = (review_set.as_of - document.as_of).days
    return MacroProjection(
        status="stale" if age_days > MACRO_CONTEXT_STALE_DAYS else "current",
        as_of=document.as_of.isoformat(),
        age_days=age_days,
        summary=document.summary,
        synthesis=(
            None if document.synthesis is None else document.synthesis.model_dump(mode="json")
        ),
        connection=document.connection.model_dump(mode="json"),
    )


def build_model_input(
    review_set: PublishedReviewSet, macro_context: MacroContextDocument | None
) -> ModelInput:
    candidates = tuple(
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
    return ModelInput(
        schema_version=(
            2
            if any(
                "ttm_quality_ev_ebitda" in entry.analysis.data_quality.model_fields_set
                or "ttm_quality_fcf" in entry.analysis.data_quality.model_fields_set
                for entry in review_set.entries
            )
            else 1
        ),
        task="research-triage",
        instruction=(
            "Use only this JSON. Do not call tools or read files. Compare every candidate and "
            "return exactly one strict decision for each ticker."
        ),
        policy=TRIAGE_POLICY,
        as_of=review_set.as_of.isoformat(),
        macro_context=macro_projection(review_set, macro_context),
        candidates=candidates,
    )
