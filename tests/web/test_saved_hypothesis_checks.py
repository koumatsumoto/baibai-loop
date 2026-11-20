"""同exact評価の仮説検討を、総合採否や後続評価の回答に置き換えず表示する。"""

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
def test_normal_saved_proposal_keeps_each_hypothesis_result_reason_and_dedicated_source(
    tmp_path, action
):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    dedicated = deepcopy(document["input_snapshot"]["sources"][0])
    dedicated.update(
        source_id="hypothesis-only",
        as_of="2026-10-10",
        retrieved_at="2026-10-11T12:34:56+09:00",
        ref="https://example.com/synthetic-hypothesis.pdf",
        used_for="合成の個別仮説の反証。全体の採否や現在の事実を認定しない。",
    )
    document["input_snapshot"]["sources"].append(dedicated)
    hypotheses = [
        {
            "statement": "ユーザーの仮説：売上増加なら利益率も改善する。",
            "origin": "user",
            "result": "refuted",
            "reason": "合成の同期間比較では営業利益率が低下。" * 30 + "\n末尾の反証留保。",
            "source_ids": ["hypothesis-only"],
        },
        {
            "statement": "当該資料の売上は増加している。",
            "origin": "research",
            "result": "supported",
            "reason": "当該期間の観測だけであり、持続利益の確認ではない。",
            "source_ids": ["annual", "hypothesis-only"],
        },
        {
            "statement": "将来の現金回収が改善するか。",
            "origin": "research",
            "result": "unresolved",
            "reason": "回収の時期・金額は未確認。",
            "source_ids": [],
        },
    ]
    document["findings"]["hypotheses"] = hypotheses
    review["checked_source_ids"].append("hypothesis-only")
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
    later_document["findings"]["hypotheses"] = []
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
    assert raw.get("hypothesis_checks") == hypotheses
    expected_sources = [
        source.model_dump(mode="json", exclude={"ticker"})
        for source in saved.document.input_snapshot.sources
        if source.source_id in {"annual", "hypothesis-only"}
    ]
    assert raw.get("hypothesis_sources") == expected_sources
    assert assessment.core_sha256 == saved.core_sha256
    assert assessment.stance == saved.document.conclusion.stance
    assert assessment.review.primary_source_check == saved.review.primary_source_check
    assert "hypothesis-only" not in assessment.value_gap.findings_source_ids
    assert "hypothesis-only" not in assessment.investment_case_source_ids
    assert view.items[0].action == action
    assert service.publish(draft) == published
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_display_does_not_infer_hypotheses_from_stance_or_countercase(tmp_path, mode):
    case = backend_case(tmp_path)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    raw = read_proposal_views(case.app).latest.proposal.items[0].assessment.model_dump(mode="json")
    raw["hypothesis_checks"] = None
    raw["hypothesis_sources"] = None
    if mode == "absent":
        del raw["hypothesis_checks"]
        del raw["hypothesis_sources"]
    value = SavedAssessmentView.model_validate(raw)
    assert value.hypothesis_checks is None
    assert value.hypothesis_sources is None
    assert value.strongest_countercase
    assert value.stance == "supported"
