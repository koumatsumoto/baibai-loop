from datetime import UTC, datetime

from tests.helpers.assessment_history import historical_context_payload
from tests.helpers.research_triage import published_review_set

from baibai_batch.analysis.model_input import build_model_input
from baibai_engine.batch_api import PublishedReviewSet, StoredAssessment
from baibai_engine.market_context.assessment.models import MarketContextAssessment


def test_counter_evidence_and_uncertainty_reach_model_input():
    doc = MarketContextAssessment.model_validate(
        {
            "context_id": "macro-context-2026-07-17-two-sided",
            "as_of": "2026-07-17",
            "known_at": "2026-07-17T10:00:00+00:00",
            "published_at": "2026-07-18T12:00:00+00:00",
            "summary": "金利と需要に相反する材料がある。",
            "inputs": [
                {
                    "input_id": "reading",
                    "reference": "reading/fixed-revision",
                    "layer": "L2",
                    "available_at": "2026-07-17T09:00:00+00:00",
                }
            ],
            "changes": [{"summary": "金利は上昇", "source_ids": ["reading"]}],
            "counter_evidence": [{"summary": "需要には改善材料", "source_ids": ["reading"]}],
            "uncertainties": [{"summary": "需要の持続性は不明", "source_ids": ["reading"]}],
            "implications": [{"summary": "資金調達と受注を両方調査", "source_ids": ["reading"]}],
        }
    )
    review = PublishedReviewSet.model_validate(published_review_set(as_of="2026-07-17"))
    cutoff = datetime(2026, 7, 18, 13, tzinfo=UTC)
    body = build_model_input(
        review, StoredAssessment.from_payload(doc.payload()), known_at=cutoff
    ).model_dump(mode="json")
    assert body["known_at"] == cutoff.isoformat()
    assert body["macro_context"]["counter_evidence"] == doc.payload()["counter_evidence"]
    assert body["macro_context"]["uncertainties"] == doc.payload()["uncertainties"]


def test_historical_synthesis_keeps_the_claim_counter_evidence_and_interactions():
    original = historical_context_payload()
    review = PublishedReviewSet.model_validate(published_review_set())
    body = build_model_input(
        review,
        StoredAssessment.from_payload(original),
        known_at=datetime(2026, 7, 19, 10, tzinfo=UTC),
    ).model_dump(mode="json")
    synthesis = body["macro_context"]["synthesis"]
    assert synthesis["interactions"] == original["synthesis"]["interactions"]
    assert {force["force_id"] for force in synthesis["dominant_forces"]} == {
        "rates-repricing",
        "fx-extreme",
    }
    for quoted, source in zip(
        synthesis["dominant_forces"], original["synthesis"]["dominant_forces"], strict=True
    ):
        for field in ("summary", "transmission", "confidence", "counter_evidence", "source_ids"):
            assert quoted[field] == source[field]
        assert any(
            item["summary"] == source["counter_evidence"]
            for item in body["macro_context"]["counter_evidence"]
        )
