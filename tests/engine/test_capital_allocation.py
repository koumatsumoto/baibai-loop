from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": "capital-allocation-assessment-20260830-test",
        "as_of": date(2026, 8, 30),
        "published_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "result": "allocate",
        "headline": "one ready alternative merits capital",
        "research_triage_id": "research-triage-test",
        "macro_context_id": None,
        "comparison": "the allocated alternative dominates the researched set",
        "forgone": "cash remains the comparison baseline",
        "alternatives": [
            {
                "ticker": "1234",
                "thesis_id": "thesis-1234",
                "thesis_core_sha256": "a" * 64,
                "thesis_review_id": "review-1234",
                "disposition": "allocate",
                "rationale": "the reviewed thesis is decision-ready",
            }
        ],
        "review": {
            "attempt": 1,
            "reviewer_identity": "independent-reviewer",
            "reviewed_at": datetime(2026, 8, 30, 11, tzinfo=UTC),
            "conclusion": "pass",
            "draft_sha256": "b" * 64,
            "open_findings": [],
        },
    }


def test_allocate_requires_exactly_one_reviewed_alternative() -> None:
    assessment = CapitalAllocationAssessment.model_validate(_payload())
    assert assessment.alternatives[0].disposition == "allocate"


def test_non_allocation_cannot_carry_an_allocated_alternative() -> None:
    payload = _payload()
    payload["result"] = "no_allocation"
    with pytest.raises(ValidationError, match="only allocate"):
        CapitalAllocationAssessment.model_validate(payload)


def test_payload_carries_no_thesis_derived_machine_scalars() -> None:
    assessment = CapitalAllocationAssessment.model_validate(_payload())
    alternative = assessment.payload()["alternatives"][0]
    assert set(alternative) == {
        "ticker",
        "thesis_id",
        "thesis_core_sha256",
        "thesis_review_id",
        "disposition",
        "rationale",
    }


def test_review_digest_changes_with_judgment_but_not_publication_time() -> None:
    assessment = CapitalAllocationAssessment.model_validate(_payload())
    later = assessment.model_copy(update={"published_at": datetime(2026, 8, 30, 13, tzinfo=UTC)})
    assert capital_allocation_draft_sha256(assessment) == capital_allocation_draft_sha256(later)
