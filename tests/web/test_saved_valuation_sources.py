"""価値の各条件に保存された引用だけを、同exact評価から表示へ渡す。"""

from copy import deepcopy
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.proposal_models import SavedValuationProjectionView
from baibai_web.sources.proposal import DbProposalSource


@pytest.mark.parametrize("action", ["buy", "no_action", "defer"])
def test_scenario_citations_survive_normal_save_and_exact_read(tmp_path, action):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    annual = next(s for s in document["input_snapshot"]["sources"] if s["source_id"] == "annual")
    dedicated = []
    for name, day in (("base", 10), ("downside", 11)):
        source = deepcopy(annual)
        source.update(
            source_id=f"{name}-only",
            as_of=f"2026-10-{day}",
            retrieved_at=f"2026-10-{day + 1}T12:34:56+09:00",
            ref=f"https://example.com/synthetic-{name}-input.pdf",
            used_for=f"合成{name}の前提確認。将来の金額を保証しない。",
        )
        dedicated.append(source)
    document["input_snapshot"]["sources"].extend(dedicated)
    document["valuation"]["base"]["source_ids"] = ["base-only", "annual"]
    document["valuation"]["downside"]["source_ids"] = ["downside-only"]
    review["checked_source_ids"].extend(s["source_id"] for s in dedicated)
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
    later_document["valuation"]["base"]["source_ids"] = ["price"]
    update_review_hash(later_document, later_review)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=saved.assessment_id
    )

    before = case.app.read_bytes()
    view = DbProposalSource(case.app).latest()
    assessment = view.items[0].assessment
    assert assessment.core_sha256 == saved.core_sha256
    assert assessment.valuation.price_yen == "1100"
    for name in ("base", "downside"):
        original = getattr(saved.document.valuation, name)
        projected = getattr(assessment.valuation, name)
        assert projected.source_ids == list(original.source_ids)
        assert [s.model_dump(mode="json") for s in projected.sources] == [
            s.model_dump(mode="json", include=set(type(projected.sources[0]).model_fields))
            for s in saved.document.input_snapshot.sources
            if s.source_id in original.source_ids
        ]
        assert projected.calculation == original.calculation
        assert projected.terminal_value_per_share_yen == str(original.terminal_value_per_share_yen)
    assert "base-only" not in assessment.valuation.downside.source_ids
    assert "downside-only" not in assessment.valuation.base.source_ids
    assert service.publish(draft) == published
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "retained-market.sqlite")
    assert DbProposalSource(case.app).latest() == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("legacy", [{}, {"source_ids": None, "sources": None}])
def test_old_projection_does_not_invent_citations(legacy):
    value = SavedValuationProjectionView.model_validate(
        {
            "terminal_value_per_share_yen": "1000",
            "cash_distribution_per_share_yen": "10",
            "annualized_return_pct": "1",
            "calculation": "合成の旧保存計算",
            **legacy,
        }
    )
    assert value.source_ids is None
    assert value.sources is None
