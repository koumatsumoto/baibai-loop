"""保有判断の対象数量/原価は保存時点の事実で、現在注文や収益の分母ではない。"""

from decimal import Decimal

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal
from tests.web.test_saved_sell_limit_comparison import _draft as sell_draft

from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedHoldItemView, SavedSellItemView
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("action", ["hold", "sell"])
def test_normal_saved_holding_position_preserves_quantity_basis_cost_and_input_clocks(
    tmp_path, action
):
    case = backend_case(tmp_path, holding=True, broken=action == "sell")
    draft = (
        reviewed_proposal(case, action="hold")
        if action == "hold"
        else sell_draft(case, broken=True)
    )
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(draft).valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    item = view.items[0]
    position = item.model_dump(mode="json").get("saved_position")
    assert position == {
        "quantity": 100,
        "quantity_basis_confirmed": True,
        "cost_yen": "90000",
        "inputs_checked_at": saved.inputs.checked_at.isoformat(),
        "ledger_as_of": saved.inputs.ledger_as_of.isoformat(),
    }
    assert item.action == action
    assert item.assessment.core_sha256 == case.pair.core_sha256
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


def test_broken_sell_keeps_unconfirmed_recorded_quantity_without_inventing_terms(tmp_path):
    case = backend_case(tmp_path, holding=True, broken=True)
    case.market.rename(tmp_path / "unavailable-market.sqlite")
    raw = reviewed_proposal(case, action="sell").payload()
    raw["items"][0]["terms_unavailable_reasons"] = [
        {"code": code, "message": "合成の具体条件不足。"}
        for code in (
            "quote_missing_or_stale",
            "quantity_basis_unconfirmed",
            "security_classification_unavailable",
            "order_session_unknown",
        )
    ]
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(draft).valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    item = read_proposal_views(case.app).latest.proposal.items[0]
    position = item.model_dump(mode="json").get("saved_position")
    assert position is not None
    assert position["quantity"] == 100
    assert position["quantity_basis_confirmed"] is False
    assert position["cost_yen"] == "90000"
    assert item.terms is None
    assert item.saved_quote is None
    assert item.saved_sell_limit_comparison is None
    assert item.action == saved.items[0].action == "sell"
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("cost", ["0", "90000.0000000000000001"])
def test_saved_display_does_not_round_recorded_cost_or_substitute_quote_value(tmp_path, cost):
    case = backend_case(tmp_path, holding=True)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case, action="hold"))
    raw = saved.payload()
    raw["items"][0]["position"].update(quantity=200, cost_yen=cost)
    raw["inputs"]["positions"][0].update(quantity=200, cost_yen=cost)
    # Historical read-projection boundary only, not a new publication or actual holding.
    historical = TradeProposal.model_validate(raw)
    before = historical.model_dump_json()
    item = project_saved_proposal(historical, {case.pair.assessment_id: case.pair}).items[0]
    position = item.model_dump(mode="json").get("saved_position")
    assert position is not None
    assert position["quantity"] == 200
    assert position["cost_yen"] == cost
    assert Decimal(position["cost_yen"]) != Decimal(item.saved_quote.price_yen) * 200
    assert historical.model_dump_json() == before


@pytest.mark.parametrize("action", ["hold", "sell"])
@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_display_omission_is_unknown_not_zero_or_confirmed(tmp_path, action, mode):
    case = backend_case(tmp_path, holding=True, broken=action == "sell")
    draft = (
        reviewed_proposal(case, action="hold")
        if action == "hold"
        else sell_draft(case, broken=True)
    )
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(draft)
    raw = read_proposal_views(case.app).latest.proposal.items[0].model_dump(mode="json")
    raw["saved_position"] = None
    if mode == "absent":
        raw.pop("saved_position")
    model = SavedHoldItemView if action == "hold" else SavedSellItemView
    assert model.model_validate(raw).saved_position is None


def test_saved_position_does_not_consult_current_inputs(tmp_path, mocker):
    case = backend_case(tmp_path, holding=True)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case, action="hold")
    )
    current_inputs = mocker.patch(
        "baibai_engine.investment_judgment.trade_proposal.inputs.collect_proposal_inputs",
        side_effect=AssertionError("must not substitute current holdings"),
    )
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    assert view.items[0].model_dump(mode="json").get("saved_position") is not None
    current_inputs.assert_not_called()
    assert case.app.read_bytes() == before
