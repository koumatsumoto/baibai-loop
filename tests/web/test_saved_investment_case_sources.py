"""投資理由の引用を他区画や後発評価で補わず、同exact pairから表示へ残す。"""

from copy import deepcopy
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.proposal_models import SavedAssessmentView
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("action", ["buy", "no_action", "defer"])
def test_normal_saved_case_keeps_dedicated_sources_and_clocks_without_current_replacement(
    tmp_path, action
):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    annual = next(s for s in document["input_snapshot"]["sources"] if s["source_id"] == "annual")
    dedicated = deepcopy(annual)
    dedicated.update(
        source_id="case-only",
        as_of="2026-10-10",
        retrieved_at="2026-10-11T12:34:56+09:00",
        ref="https://example.com/synthetic-investment-case.pdf",
        used_for="合成の投資理由・反証条件を検討。引用だけでは仮説維持を保証しない。",
    )
    document["input_snapshot"]["sources"].append(dedicated)
    document["investment_case"]["source_ids"] = ["case-only", "annual"]
    review["checked_source_ids"].append("case-only")
    update_review_hash(document, review)
    saved = AssessmentStoreService(case.app, clock=lambda: PREPARED).publish_reviewed_assessment(
        assessment_id(10), document, review, supersedes_id=case.pair.assessment_id
    )
    case.pair = saved
    case.operations.record_assessment(
        case.operation.operation_id, saved.assessment_id, now=PREPARED
    )
    draft = reviewed_proposal(case, action=action)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    published = service.publish(draft)

    later_document, later_review = pair_payload(review_number=21)
    later_document["investment_case"]["source_ids"] = ["price"]
    update_review_hash(later_document, later_review)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=saved.assessment_id
    )

    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    assessment = view.items[0].assessment
    raw = assessment.model_dump(mode="json")
    assert raw.get("investment_case_source_ids") == ["case-only", "annual"]
    assert raw.get("investment_case_sources") == [
        {
            key: source[key]
            for key in (
                "source_id",
                "source_tier",
                "ref",
                "provider",
                "dataset",
                "as_of",
                "retrieved_at",
                "used_for",
            )
        }
        for source in document["input_snapshot"]["sources"]
        if source["source_id"] in {"annual", "case-only"}
    ]
    assert assessment.core_sha256 == saved.core_sha256
    assert assessment.investment_case_explanation == saved.document.investment_case.explanation
    assert assessment.invalidation_conditions == list(
        saved.document.investment_case.invalidation_conditions
    )
    assert assessment.review.primary_source_check == saved.review.primary_source_check
    assert "case-only" not in assessment.value_gap.findings_source_ids
    assert "case-only" not in assessment.valuation.base.source_ids
    assert view.items[0].action == action
    assert service.publish(draft) == published
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_display_does_not_infer_case_sources_from_other_sections(tmp_path, mode):
    case = backend_case(tmp_path)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    raw = read_proposal_views(case.app).latest.proposal.items[0].assessment.model_dump(mode="json")
    raw["investment_case_source_ids"] = None
    raw["investment_case_sources"] = None
    if mode == "absent":
        del raw["investment_case_source_ids"]
        del raw["investment_case_sources"]
    value = SavedAssessmentView.model_validate(raw)
    assert value.investment_case_source_ids is None
    assert value.investment_case_sources is None
    assert value.value_gap.findings_source_ids
    assert value.valuation.base.source_ids
