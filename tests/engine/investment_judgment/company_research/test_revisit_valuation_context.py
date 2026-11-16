"""価値未完成の原理由を再訪へ残し、現在の売買判断や価値ゼロにしない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_revisit_countercase import NOW, _context
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService


@pytest.mark.parametrize(
    "reason", [None, "価格は確認済み、普通株cashへの橋渡しは未完成。", "未完成の原理由" * 100]
)
def test_exact_valuation_state_and_reason_reach_brief_without_current_price_inference(
    tmp_path, reason
):
    doc, review = pair_payload()
    if reason is not None:
        doc["valuation"] = {
            "status": "unresolved",
            "market_price_fact_id": doc["valuation"]["market_price_fact_id"],
            "horizon_months": None,
            "required_annual_return_pct": None,
            "base": None,
            "downside": None,
            "unresolved_reason": reason,
        }
        doc["conclusion"]["stance"] = "uncertain"
        doc["investment_case"]["status"] = "uncertain"
        review["recalculated_projections"] = []
    update_review_hash(doc, review)
    path = tmp_path / "app"
    pair = AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), doc, review
    )
    before = path.read_bytes()
    context = _context(path)
    original = context.model_dump_json()
    brief = company_context_brief(context)["assessment"]
    shown = brief["valuation_context"]
    assert shown == {
        "status": "unresolved" if reason is not None else "resolved",
        "unresolved_reason": reason[:400] if reason is not None else None,
        "unresolved_reason_truncated": reason is not None and len(reason) > 400,
    }
    assert context.assessment.valuation_context.unresolved_reason == reason
    assert brief["reference"]["core_sha256"] == pair.core_sha256
    assert brief["stance"] == doc["conclusion"]["stance"]
    assert "market_price_fact_id" not in shown
    assert "base" not in shown
    assert brief["role"] == "historical_reviewed_assessment_not_current_readiness"
    shown["status"] = "changed"
    assert context.model_dump_json() == original
    assert path.read_bytes() == before
