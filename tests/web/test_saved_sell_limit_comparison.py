"""保存した売却下限での保有見返り。合成writerとread-only表示の境界。"""

from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedSellItemView
from baibai_web.sources.proposal import read_proposal_views


def _draft(case, *, limit="900", broken=False):
    raw = reviewed_proposal(case, action="sell").payload()
    raw["items"][0].update(
        reason_code="economic_invalidation" if broken else "insufficient_remaining_reward",
        remaining_reward=None
        if broken
        else {"status": "insufficient", "reason": "合成の現在終値での見返りは不足。"},
        case_reassessment_reason="合成の今回確認。下限での保有と回収は別に比較する。",
        terms={
            "side": "sell",
            "limit_price_yen": limit,
            "quantity": 100,
            "notional_yen": str(Decimal(limit) * 100),
            "target_session": "2026-10-15",
            "valuation_as_of": "2026-10-14",
            "maximum_entry_price_yen": None,
            "price_grid_rule_id": "tse-common-conservative-20261014",
            "conditions": ["仮説、数量、注文状況を再確認する。"],
            "warnings": [],
        },
        terms_unavailable_reasons=[],
    )
    return bind_review(raw, {case.pair.assessment_id: case.pair.document})


@pytest.mark.parametrize("limit", ["900", "1200"])
def test_normal_saved_sell_keeps_floor_comparison_separate_from_observed_quote(tmp_path, limit):
    case = backend_case(tmp_path, holding=True)
    draft = _draft(case, limit=limit)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    check = service.check(draft)
    assert check.valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    item = view.items[0]
    comparison = item.model_dump(mode="json").get("saved_sell_limit_comparison")
    assert comparison is not None
    assert comparison["price_context"] == "author_sell_minimum"
    assert comparison["calculation_price_yen"] == limit
    assert comparison["horizon_months"] == 12
    assert comparison["return_basis"] == "conditional_pretax_without_reinvestment"
    assert not {"price_as_of", "price_basis", "price_yen"} & comparison.keys()
    for name in ("base", "downside"):
        expected = check.calculations["1234"]["sell_limit_projection"][name]
        assert Decimal(comparison[name]["total_value_per_share_yen"]) == Decimal(
            str(expected["total_value_yen"])
        )
        for field in ("total_return_pct", "annualized_return_pct"):
            assert Decimal(comparison[name][field]) == Decimal(str(expected[field]))
    assert Decimal(item.saved_quote.price_yen) == 1100
    assert item.assessment.valuation.price_yen == "1100"
    assert item.assessment.valuation.base.annualized_return_pct == "0.0000"
    assert item.action == "sell"
    assert item.assessment.core_sha256 == case.pair.core_sha256
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("unresolved", [False, True])
def test_broken_sell_does_not_reuse_invalidated_value_for_floor_comparison(tmp_path, unresolved):
    case = backend_case(tmp_path, holding=True, broken=True, valuation_unresolved=unresolved)
    draft = _draft(case, broken=True)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(draft)
    item = project_saved_proposal(saved, {case.pair.assessment_id: case.pair}).items[0]
    assert item.action == "sell"
    assert item.saved_sell_limit_comparison is None


def test_old_display_omission_and_missing_terms_do_not_create_a_floor(tmp_path):
    case = backend_case(tmp_path, holding=True)
    draft = _draft(case)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(draft)
    raw = saved.payload()
    raw["items"][0].update(
        terms=None,
        terms_unavailable_reasons=[{"code": "legacy_unknown", "message": "旧記録の条件未確定"}],
    )
    historical = TradeProposal.model_validate(raw)
    item = project_saved_proposal(historical, {case.pair.assessment_id: case.pair}).items[0]
    assert item.saved_sell_limit_comparison is None
    old = item.model_dump(mode="json")
    del old["saved_sell_limit_comparison"]
    assert SavedSellItemView.model_validate(old).saved_sell_limit_comparison is None


def test_saved_floor_stays_on_exact_pair_after_a_later_assessment(tmp_path):
    from datetime import timedelta

    case = backend_case(tmp_path, holding=True)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        _draft(case)
    )
    first = read_proposal_views(case.app).latest.proposal
    document, review = pair_payload(review_number=3)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(2), document, review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    after = read_proposal_views(case.app).latest.proposal
    assert after == first
    assert after.items[0].saved_sell_limit_comparison is not None
    assert after.items[0].assessment.assessment_id == case.pair.assessment_id
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("missing", ["date", "valuation", "case"])
def test_historical_projection_does_not_fill_a_missing_value_basis(tmp_path, missing):
    from dataclasses import replace

    case = backend_case(tmp_path, holding=True)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(_draft(case))
    document = case.pair.document
    if missing == "date":
        document = document.model_copy(
            update={
                "input_snapshot": document.input_snapshot.model_copy(
                    update={"as_of": document.input_snapshot.as_of.replace(day=13)}
                )
            }
        )
    elif missing == "valuation":
        document = document.model_copy(
            update={
                "valuation": document.valuation.model_copy(
                    update={"status": "unresolved", "unresolved_reason": "合成旧評価の未確定"}
                )
            }
        )
    else:
        document = document.model_copy(
            update={
                "investment_case": document.investment_case.model_copy(update={"status": "broken"})
            }
        )
    # Read-projection boundary only; these altered objects are never published or adopted.
    pair = replace(case.pair, document=document)
    view = project_saved_proposal(saved, {pair.assessment_id: pair})
    assert view.items[0].saved_sell_limit_comparison is None
