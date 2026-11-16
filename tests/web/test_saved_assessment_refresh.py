"""差分確認の根拠はexact評価と一緒に読み、現在の再確認へ読み替えない。"""

from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.scenario import maximum_entry_price
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.proposal_models import SavedAssessmentView
from baibai_web.sources.proposal import DbProposalSource


@pytest.mark.parametrize("action", ["buy", "no_action", "defer"])
def test_saved_refresh_keeps_remaining_period_distribution_and_exact_prior(tmp_path, action):
    case = backend_case(tmp_path, assessment_as_of="2026-07-14")
    prior = case.pair
    document, review = pair_payload(review_number=11)
    document["valuation"]["horizon_months"] = 9
    document["valuation"]["base"]["cash_distribution_per_share_yen"] = "40"
    review["recalculated_projections"][0]["cash_distribution_per_share_yen"] = "40"
    document["refresh"] = {
        "prior_assessment_ref": {
            "assessment_id": prior.assessment_id,
            "core_sha256": prior.core_sha256,
            "review_id": prior.review.review_id,
        },
        "checked_at": "2026-10-14T15:55:00+09:00",
        "changed_sections": ["valuation", "sources", "risks"],
        "material_information_check": "今回の重要開示を確認。全原典再取得はしていない。",
        "unchanged_assumptions_reason": "旧根拠の範囲を読み直し、終価を維持。",
        "horizon_reason": "3か月経過したので残り9か月。旧12か月を延長しない。",
        "distribution_reason": "旧期の権利分を今後へ再計上せず、今回から40円のみ。",
        "corporate_action_reason": "同じ一株の権利を確認。" * 40
        + "\n末尾：未公表の変更は保証しない。",
    }
    update_review_hash(document, review)
    saved = AssessmentStoreService(case.app, clock=lambda: PREPARED).publish_reviewed_assessment(
        assessment_id(10), document, review, supersedes_id=prior.assessment_id
    )
    case.pair = saved
    case.operations.record_assessment(
        case.operation.operation_id, saved.assessment_id, now=PREPARED
    )
    draft = reviewed_proposal(case, action=action)
    if action == "buy":
        raw = draft.model_dump(mode="json")
        raw["items"][0]["terms"]["maximum_entry_price_yen"] = str(
            maximum_entry_price(
                saved.document.valuation.base,
                horizon_months=9,
                required_annual_return_pct=saved.document.valuation.required_annual_return_pct,
            )
        )
        draft = bind_review(raw, {saved.assessment_id: saved.document})
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(draft)
    later_document, later_review = pair_payload(review_number=21)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=saved.assessment_id
    )

    before = case.app.read_bytes()
    item = DbProposalSource(case.app).latest().items[0]
    view = item.assessment
    assert view.refresh.model_dump(mode="json") == saved.document.refresh.model_dump(mode="json")
    assert view.valuation.horizon_months == 9
    assert view.valuation.base.cash_distribution_per_share_yen == "40"
    assert view.core_sha256 == saved.core_sha256
    assert view.review_id == saved.review.review_id
    assert item.action == action
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
        assert exact == saved
        assert (
            require_assessment_ref(connection, saved.document.refresh.prior_assessment_ref) == prior
        )
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("legacy", [False, True])
def test_absent_refresh_is_not_filled_from_review_or_valuation(tmp_path, legacy):
    case = backend_case(tmp_path)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case, action="no_action")
    )
    view = DbProposalSource(case.app).latest().items[0].assessment
    payload = view.model_dump(mode="json")
    if legacy:
        payload.pop("refresh", None)
    parsed = SavedAssessmentView.model_validate(payload)
    assert parsed.refresh is None
    assert parsed.review == view.review
    assert parsed.valuation == view.valuation
