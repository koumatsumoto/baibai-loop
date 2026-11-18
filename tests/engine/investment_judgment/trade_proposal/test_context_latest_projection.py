"""現在quote診断は固定済み旧評価の読取と分ける。全入力は合成。"""

from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import (
    PUBLISHED,
    backend_case,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService


def _context(case):
    return build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=3),
    )


def _new_pair(case, *, ticker="1234", unresolved=False):
    document, review = pair_payload(
        ticker=ticker,
        review_number=3,
        price_case="undetermined" if unresolved else "supported_at_quote",
    )
    if unresolved:
        document["conclusion"]["stance"] = "uncertain"
        document["investment_case"]["status"] = "uncertain"
        document["valuation"] = {
            "status": "unresolved",
            "market_price_fact_id": None,
            "horizon_months": None,
            "required_annual_return_pct": None,
            "base": None,
            "downside": None,
            "unresolved_reason": "合成の追加情報により価値仮定は再検討中。",
        }
        review["recalculated_projections"] = []
    else:
        document["valuation"]["base"]["terminal_value_per_share_yen"] = "2200"
        review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "2200"
    update_review_hash(document, review)
    return AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=2)
    ).publish_reviewed_assessment(
        assessment_id(2),
        document,
        review,
        supersedes_id=case.pair.assessment_id if ticker == "1234" else None,
    )


@pytest.mark.parametrize("holding", [False, True], ids=["new", "holding"])
@pytest.mark.parametrize("unresolved", [False, True], ids=["revised-value", "uncertain-value"])
def test_context_stops_superseded_current_projection_without_replacing_fixed_pair(
    tmp_path, holding, unresolved
):
    case = backend_case(tmp_path, holding=holding)
    before = _context(case)
    assert before.calculations["1234"]["current_quote_projection"] is not None
    newer = _new_pair(case, unresolved=unresolved)
    original = case.app.read_bytes()
    context = _context(case)
    assert context.calculations["1234"]["current_quote_projection"] is None
    assert context.latest_assessment_ids["1234"] == newer.assessment_id
    assert context.research_scope == before.research_scope
    assert context.assessments == before.assessments
    assert set(context.assessments) == {case.pair.assessment_id}
    assert context.assessments[case.pair.assessment_id] == case.pair
    assert context.calculations["1234"]["case_reassessment_required"] == holding
    if not holding:
        row = context.calculations["1234"]
        diagnostic = row["buy_at_actual_quote"]
        assert "latest_assessment_required" in {
            reason["code"] for reason in diagnostic["hard_blockers"]
        }
        assert diagnostic["current_price_projection"] is None
        assert diagnostic["limit_price_projection"] is None
        assert diagnostic["capital_comparison"] is None
        assert diagnostic["quantity"] == 0
        assert diagnostic["maximum_price_yen"] is None
        assert diagnostic["current_price_above_maximum"] is None
        assert "calculation_at_maximum_price" not in row
        assert "maximum_common_grid_price_yen" not in row
    assert case.app.read_bytes() == original


def test_other_company_revision_leaves_current_projection_unchanged(tmp_path):
    case = backend_case(tmp_path)
    before = _context(case)
    _new_pair(case, ticker="5678")
    context = _context(case)
    assert context.latest_assessment_ids["1234"] == case.pair.assessment_id
    assert context.calculations == before.calculations
    assert context.assessments == before.assessments


def test_explicitly_bound_new_pair_uses_its_value_not_the_old_value(tmp_path):
    case = backend_case(tmp_path)
    before = _context(case)
    newer = _new_pair(case)
    case.operations.record_assessment(
        case.operation.operation_id,
        newer.assessment_id,
        now=PUBLISHED + timedelta(minutes=2),
    )
    context = _context(case)
    assert set(context.assessments) == {newer.assessment_id}
    assert (
        context.calculations["1234"]["current_quote_projection"]["base"]["total_value_yen"] == 2200
    )
    assert before.calculations["1234"]["maximum_common_grid_price_yen"] == "1000"
    assert context.calculations["1234"]["maximum_common_grid_price_yen"] == "2000"
    assert context.calculations != before.calculations


def test_old_saved_no_action_exact_read_and_retry_are_not_a_current_projection(tmp_path):
    case = backend_case(tmp_path)
    draft = reviewed_proposal(case, action="no_action")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    saved = service.publish(draft)
    _new_pair(case, unresolved=True)
    original = case.app.read_bytes()
    assert _context(case).calculations["1234"]["current_quote_projection"] is None
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == original
