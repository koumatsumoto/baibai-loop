"""保存 hold の確認済み quote 見返りを、元評価価格と分けて表示する。"""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedHoldItemView
from baibai_web.sources.proposal import read_proposal_views


def _draft(case, *, sufficient=True):
    raw = reviewed_proposal(case, action="hold").payload()
    if sufficient:
        raw["items"][0].update(
            holding_basis="sufficient_remaining_reward",
            remaining_reward={
                "status": "sufficient",
                "reason": "合成の現在見返りと代替案を比較した。",
            },
        )
    return bind_review(raw, {case.pair.assessment_id: case.pair.document})


@pytest.mark.parametrize("quote", ["1000", "1200"])
def test_normal_hold_quote_return_differs_from_original_assessment_price(tmp_path, quote):
    case = backend_case(tmp_path, holding=True)
    seed_daily_bars(case.market, [("1234", "2026-10-14", float(quote), 1.0)])
    draft = _draft(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    check = service.check(draft)
    assert check.valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    item = view.items[0]
    comparison = item.model_dump(mode="json").get("saved_hold_quote_comparison")
    assert comparison is not None
    assert comparison["price_context"] == "saved_hold_quote"
    assert Decimal(comparison["calculation_price_yen"]) == Decimal(quote)
    assert comparison["price_as_of"] == "2026-10-14"
    assert comparison["price_basis"] == "last_close_unadjusted"
    assert comparison["horizon_months"] == 12
    assert comparison["return_basis"] == "conditional_pretax_without_reinvestment"
    for name in ("base", "downside"):
        expected = check.calculations["1234"]["current_price_projection"][name]
        assert Decimal(comparison[name]["total_value_per_share_yen"]) == Decimal(
            str(expected["total_value_yen"])
        )
        for field in ("total_return_pct", "annualized_return_pct"):
            assert Decimal(comparison[name][field]) == Decimal(str(expected[field]))
    assert item.assessment.valuation.price_yen == "1100"
    assert item.assessment.valuation.base.annualized_return_pct == "0.0000"
    assert Decimal(comparison["base"]["annualized_return_pct"]) != 0
    assert item.action == "hold"
    assert item.assessment.core_sha256 == case.pair.core_sha256
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


def test_intact_only_hold_does_not_upgrade_to_sufficient(tmp_path):
    case = backend_case(tmp_path, holding=True)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    service.publish(_draft(case, sufficient=False))
    item = read_proposal_views(case.app).latest.proposal.items[0]
    assert item.model_dump(mode="json")["saved_hold_quote_comparison"] is None
    assert item.holding_basis == "investment_case_intact"
    assert item.remaining_reward is None


@pytest.mark.parametrize(
    "unavailable", ["valuation_unresolved", "valuation_basis_requires_refresh"]
)
def test_normal_hold_with_unavailable_calculation_stays_unknown(tmp_path, unavailable):
    case = backend_case(
        tmp_path,
        holding=True,
        valuation_unresolved=unavailable == "valuation_unresolved",
        assessment_as_of="2026-10-13"
        if unavailable == "valuation_basis_requires_refresh"
        else "2026-10-14",
    )
    raw = _draft(case, sufficient=False).payload()
    raw["items"][0]["return_calculation_unavailable_reason"] = unavailable
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(draft)
    item = read_proposal_views(case.app).latest.proposal.items[0]
    assert item.model_dump(mode="json")["saved_hold_quote_comparison"] is None
    assert item.return_calculation_unavailable_reason == unavailable
    assert item.holding_basis == "investment_case_intact"


@pytest.mark.parametrize("missing", ["quote", "quantity_basis", "case", "reason"])
def test_historical_unknowns_are_not_backfilled_for_quote_comparison(tmp_path, missing):
    case = backend_case(tmp_path, holding=True)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(_draft(case))
    item = saved.items[0]
    document = case.pair.document
    if missing == "quote":
        saved = saved.model_copy(update={"inputs": saved.inputs.model_copy(update={"quotes": ()})})
    elif missing == "quantity_basis":
        item = item.model_copy(
            update={
                "position": item.position.model_copy(update={"quantity_basis_confirmed": False})
            }
        )
    elif missing == "case":
        document = document.model_copy(
            update={
                "investment_case": document.investment_case.model_copy(update={"status": "broken"})
            }
        )
    else:
        item = item.model_copy(
            update={"return_calculation_unavailable_reason": "corporate_action_unresolved"}
        )
    # Historical display boundary only; these changed objects are never published.
    pair = replace(case.pair, document=document)
    view = project_saved_proposal(
        saved.model_copy(update={"items": (item,)}), {pair.assessment_id: pair}
    )
    assert view.items[0].model_dump(mode="json")["saved_hold_quote_comparison"] is None


def test_old_json_and_later_assessment_do_not_replace_saved_hold(tmp_path):
    case = backend_case(tmp_path, holding=True)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        _draft(case)
    )
    first = read_proposal_views(case.app).latest.proposal
    raw = first.items[0].model_dump(mode="json")
    raw.pop("saved_hold_quote_comparison", None)
    assert SavedHoldItemView.model_validate(raw).saved_hold_quote_comparison is None
    document, review = pair_payload(review_number=3)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(2), document, review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    assert read_proposal_views(case.app).latest.proposal == first
    assert first.items[0].assessment.assessment_id == case.pair.assessment_id
    assert case.app.read_bytes() == before
