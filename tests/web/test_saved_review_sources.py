"""Reviewが挙げた確認対象を作者引用と混ぜず同exact保存から表示へ残す。"""

from copy import deepcopy
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.proposal_models import SavedAssessmentReviewView
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("action", ["buy", "no_action", "defer"])
def test_saved_review_keeps_its_checked_sources_not_all_snapshot_or_later_pair(tmp_path, action):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    annual = next(s for s in document["input_snapshot"]["sources"] if s["source_id"] == "annual")
    dedicated = deepcopy(annual)
    dedicated.update(
        source_id="review-only",
        source_tier="secondary",
        as_of="2026-10-10",
        retrieved_at="2026-10-11T12:34:56+09:00",
        ref="https://example.com/synthetic-review-check.html",
        used_for="合成の独立検証で反対論を照合。本文全体や将来の確認済みを意味しない。",
    )
    unchecked = deepcopy(dedicated)
    unchecked.update(source_id="not-reviewed", ref="https://example.com/not-checked.html")
    document["input_snapshot"]["sources"].extend([dedicated, unchecked])
    review["checked_source_ids"] = ["review-only", *review["checked_source_ids"]]
    review["review_scope"] = "記載の確認対象だけを照合。資料の全事実・未開示条件は未確認。"
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
    later_review["review_scope"] = "後発の別の検証。旧表示へ差し替えない。"
    update_review_hash(later_document, later_review)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=saved.assessment_id
    )

    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    assessment = view.items[0].assessment
    raw = assessment.review.model_dump(mode="json")
    assert raw.get("checked_source_ids") == review["checked_source_ids"]
    assert raw.get("checked_sources") == [
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
        if source["source_id"] in review["checked_source_ids"]
    ]
    assert "not-reviewed" not in str(raw)
    assert assessment.review.review_scope == review["review_scope"]
    assert assessment.review.primary_source_check == review["primary_source_check"]
    assert assessment.review.reviewed_at == saved.review.reviewed_at
    assert assessment.review_id == saved.review.review_id
    assert assessment.core_sha256 == saved.core_sha256
    assert "review-only" not in assessment.investment_case_source_ids
    assert "review-only" not in assessment.value_gap.findings_source_ids
    assert view.items[0].action == action
    assert service.publish(draft) == published
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_display_review_does_not_infer_checked_sources_from_author_sections(tmp_path, mode):
    case = backend_case(tmp_path)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    assessment = read_proposal_views(case.app).latest.proposal.items[0].assessment
    raw = assessment.review.model_dump(mode="json")
    raw["checked_source_ids"] = None
    raw["checked_sources"] = None
    if mode == "absent":
        del raw["checked_source_ids"]
        del raw["checked_sources"]
    value = SavedAssessmentReviewView.model_validate(raw)
    assert value.checked_source_ids is None
    assert value.checked_sources is None
    assert assessment.investment_case_source_ids
    assert value.primary_source_check == case.pair.review.primary_source_check
