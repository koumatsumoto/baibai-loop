"""保存済み作者仮説を、Reviewの反論や現在の評価へ置き換えず表示へ渡す。"""

from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.proposal_models import SavedAssessmentView
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("action", ["buy", "no_action", "defer"])
def test_normal_saved_proposal_keeps_author_countercase_and_realization(tmp_path, action):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    countercase = (
        "作者の反証：" + "借換条件は未確約。" * 40 + "\n末尾：追加資本で普通株価値が毀損し得る。"
    )
    realization = {
        "mechanism": "受注が利益になっても、検収後の現金回収まで必要資金が残る。",
        "timing": "uncertain",
        "timing_reason": "12か月は比較窓であり、入金時期を確認したものではない。",
        "delay_risk": "回収遅延と借換不成立が重なる場合、希薄化の可能性がある。",
    }
    document["conclusion"]["strongest_countercase"] = countercase
    document["findings"]["realization"] = realization
    document["permanent_loss_risks"][0]["evidence_status"] = "partially_verified"
    review["strongest_countercase"] = "Reviewerの反論：顧客側の投資延期で受注が減る可能性。"
    review["uncertainty_acceptance_reason"] = "未確約の条件を残し、代替資金経路と見返りを比較した。"
    update_review_hash(document, review)
    saved = AssessmentStoreService(case.app, clock=lambda: PREPARED).publish_reviewed_assessment(
        assessment_id(10), document, review, supersedes_id=case.pair.assessment_id
    )
    case.pair = saved
    case.operations.record_assessment(
        case.operation.operation_id, saved.assessment_id, now=PREPARED
    )
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action=action))

    later_document, later_review = pair_payload(review_number=21)
    later_document["conclusion"]["strongest_countercase"] = "後続評価だけの反証"
    later_document["findings"]["realization"]["mechanism"] = "後続評価だけの機序"
    update_review_hash(later_document, later_review)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=saved.assessment_id
    )

    before = case.app.read_bytes()
    item = read_proposal_views(case.app).latest.proposal.items[0]
    projected = item.assessment
    assert projected.strongest_countercase == countercase
    assert projected.realization.model_dump(mode="json") == realization
    assert projected.review.strongest_countercase == review["strongest_countercase"]
    assert projected.review.uncertainty_acceptance_reason == review["uncertainty_acceptance_reason"]
    assert projected.valuation.horizon_months == 12
    assert item.action == action
    assert projected.core_sha256 == saved.core_sha256
    assert "後続評価だけ" not in projected.model_dump_json()
    with closing(connect_read_only(case.app)) as connection:
        assert load_investment_proposal(connection, proposal.proposal_id) == proposal
        exact = require_assessment_ref(
            connection,
            AssessmentRef(
                assessment_id=saved.assessment_id,
                core_sha256=saved.core_sha256,
                review_id=saved.review.review_id,
            ),
        )
        assert exact.document == saved.document
    assert case.app.read_bytes() == before

    old = projected.model_dump(mode="json")
    del old["strongest_countercase"]
    del old["realization"]
    legacy = SavedAssessmentView.model_validate(old)
    assert legacy.strongest_countercase is None
    assert legacy.realization is None
    assert legacy.review.strongest_countercase == projected.review.strongest_countercase
