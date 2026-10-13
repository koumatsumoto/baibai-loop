from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    StoredCapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.storage.application.json import canonical_json


def _payload() -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": "capital-allocation-assessment-20260830-test",
        "as_of": date(2026, 8, 30),
        "published_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "result": "allocate",
        "headline": "one ready alternative merits capital",
        "research_triage_id": "research-triage-test",
        "market_context_assessment_id": None,
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


def test_saved_allocation_keeps_original_payload_and_review_hash_without_an_old_writer(tmp_path):
    current = CapitalAllocationAssessment.model_validate(_payload()).payload()
    old = {**current, "schema_version": 1, "macro_context_id": "macro-context-2026-07-17-saved"}
    old.pop("market_context_assessment_id")
    allocation = StoredCapitalAllocationAssessment.from_payload(old)
    assert allocation.schema_version == 1
    assert allocation.market_context_assessment_id == old["macro_context_id"]
    assert allocation.payload() == old
    reviewed = {k: v for k, v in old.items() if k not in {"review", "published_at"}}
    assert (
        capital_allocation_draft_sha256(allocation)
        == sha256(canonical_json(reviewed).encode()).hexdigest()
    )
    with pytest.raises(ValidationError):
        CapitalAllocationAssessment.model_validate(old)
    database = tmp_path / "not-created.sqlite"
    with pytest.raises(ValueError, match="read-only"):
        CapitalAllocationAssessmentService(database).publish(allocation)
    assert not database.exists()
