"""独立Reviewの限定を作者と別枠で固定し、review済みを全面確認にしない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_revisit_countercase import (
    NOW,
    _context,
)
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash

from baibai_engine.investment_judgment.company_research.context import (
    CompanyContext,
    company_context_brief,
)
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService


@pytest.mark.parametrize("check", ["verified", "partially_verified", "unverified"])
def test_exact_review_keeps_its_check_clock_scope_and_countercase_separate(tmp_path, check):
    doc, review = pair_payload(price_case="undetermined")
    doc["conclusion"]["stance"] = "uncertain"
    doc["investment_case"]["status"] = "uncertain"
    review.update(
        primary_source_check=check,
        review_scope="確認範囲だけ。" * 90,
        strongest_countercase="作者と異なる独立反証。" * 70,
        uncertainty_acceptance_reason="残る不確実性の範囲。" * 70,
    )
    review.pop("nonmaterial_unknown_reason", None)
    update_review_hash(doc, review)
    path = tmp_path / "app"
    pair = AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), doc, review
    )
    before = path.read_bytes()
    context = _context(path)
    original = context.model_dump_json()
    brief = company_context_brief(context)["assessment"]
    result = brief["independent_review"]
    assert result["primary_source_check"] == check
    assert result["reviewed_at"] == pair.review.reviewed_at.isoformat()
    assert brief["reference"]["review_id"] == pair.review.review_id
    assert brief["reference"]["core_sha256"] == pair.review.reviewed_assessment_sha256
    assert brief["strongest_countercase"] == doc["conclusion"]["strongest_countercase"][:400]
    for name in ("review_scope", "strongest_countercase", "uncertainty_acceptance_reason"):
        assert result[name] == {"text": review[name][:400], "text_truncated": True}
        assert getattr(context.assessment.independent_review, name) == review[name]
    sources = {s.source_id: s for s in pair.document.input_snapshot.sources}
    assert result["sources"] == [
        {
            "source_id": key,
            "source_tier": sources[key].source_tier,
            "as_of": sources[key].as_of.isoformat(),
            "retrieved_at": sources[key].retrieved_at.isoformat(),
        }
        for key in pair.review.checked_source_ids[:3]
    ]
    assert "reviewer_identity" not in context.assessment.independent_review.model_dump()
    assert "recalculated_projections" not in result
    result["review_scope"]["text"] = "not saved"
    assert context.model_dump_json() == original
    assert path.read_bytes() == before


def test_legacy_context_does_not_backfill_review_and_null_reason_is_not_invented(tmp_path):
    doc, review = pair_payload(price_case="undetermined")
    review.pop("uncertainty_acceptance_reason", None)
    review["nonmaterial_unknown_reason"] = None
    path = tmp_path / "app"
    AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), doc, review
    )
    context = _context(path)
    assert (
        "uncertainty_acceptance_reason"
        not in company_context_brief(context)["assessment"]["independent_review"]
    )
    legacy = context.model_dump(mode="json")
    legacy["assessment"].pop("independent_review")
    parsed = CompanyContext.model_validate(legacy)
    assert parsed.model_dump(mode="json") == legacy
    assert "independent_review" not in company_context_brief(parsed)["assessment"]
