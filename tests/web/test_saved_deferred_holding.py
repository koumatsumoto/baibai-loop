"""保有見直しの判断保留でも、保存時の既保有を失わせない。"""

import pytest
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedDeferItemView
from baibai_web.sources.proposal import DbProposalSource


def _saved(case, *, unreviewed=False):
    if unreviewed:
        case.operation = case.operations.record_unfinished(
            case.operation.operation_id,
            ticker="1234",
            reason="合成: 反証の経済範囲を未確認。保有は残っている。",
            references=("fixture:holding-question",),
            now=PREPARED,
        )
    draft = reviewed_proposal(case, action="defer")
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(draft).valid
    return service, draft, service.publish(draft)


@pytest.mark.parametrize("unreviewed", [False, True])
@pytest.mark.parametrize("basis_available", [False, True])
def test_normal_holding_defer_keeps_saved_position_without_becoming_hold_or_order(
    tmp_path, monkeypatch, unreviewed, basis_available
):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    if not basis_available:
        case.market.rename(tmp_path / "removed-market.sqlite")
    service, draft, saved = _saved(case, unreviewed=unreviewed)
    before = case.app.read_bytes()
    view = DbProposalSource(case.app).latest()
    item = view.items[0]
    position = item.model_dump(mode="json").get("saved_position")
    assert position == {
        "quantity": 100,
        "quantity_basis_confirmed": basis_available,
        "cost_yen": "90000",
        "inputs_checked_at": saved.inputs.checked_at.isoformat(),
        "ledger_as_of": saved.inputs.ledger_as_of.isoformat(),
    }
    assert item.action == saved.items[0].action == "defer"
    assert item.resume_conditions == list(saved.items[0].resume_conditions)
    assert (item.assessment is None) == unreviewed
    assert (item.unfinished is not None) == unreviewed
    assert "terms" not in item.model_dump()
    assert "remaining_reward" not in item.model_dump()
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before

    def forbidden(*_args, **_kwargs):
        pytest.fail("saved position must not be refreshed from current inputs")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.trade_proposal.inputs.collect_proposal_inputs",
        forbidden,
    )
    if case.market.exists():
        case.market.rename(tmp_path / "removed-market.sqlite")
    assert DbProposalSource(case.app).latest() == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("position_case", ["absent", "other_ticker"])
def test_holding_defer_does_not_invent_missing_position_or_borrow_another_ticker(
    tmp_path, position_case
):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _service, _draft, saved = _saved(case)
    raw = saved.payload()
    if position_case == "absent":
        raw["inputs"]["positions"] = []
    else:
        raw["inputs"]["positions"][0]["ticker"] = "5678"
    historical = TradeProposal.model_validate(raw)
    before = historical.model_dump_json()
    view = project_saved_proposal(historical, {case.pair.assessment_id: case.pair})
    assert view.items[0].model_dump(mode="json").get("saved_position") is None
    assert historical.model_dump_json() == before


def test_new_research_defer_does_not_infer_holding_from_price_or_valuation(tmp_path):
    case = backend_case(tmp_path, valuation_unresolved=True)
    _service, _draft, saved = _saved(case)
    view = DbProposalSource(case.app).latest()
    assert saved.research_scope.targets[0].purpose == "new"
    assert view.items[0].model_dump(mode="json").get("saved_position") is None
    assert view.items[0].saved_quote is not None
    # A new-research scope is not silently converted to a holding review.
    raw = saved.payload()
    raw["inputs"]["positions"] = [
        {
            "ticker": "1234",
            "sector": "情報・通信業",
            "common_factors": [],
            "quantity": 100,
            "cost_yen": "90000",
            "quantity_basis_confirmed": False,
            "original_reason_unavailable_reason": "合成対照の元理由未確認。",
        }
    ]
    historical = TradeProposal.model_validate(raw)
    projected = project_saved_proposal(historical, {case.pair.assessment_id: case.pair})
    assert projected.items[0].model_dump(mode="json").get("saved_position") is None


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_defer_display_remains_unrecorded_not_zero_or_confirmed(tmp_path, mode):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _saved(case)
    raw = DbProposalSource(case.app).latest().items[0].model_dump(mode="json")
    raw["saved_position"] = None
    if mode == "absent":
        raw.pop("saved_position")
    assert SavedDeferItemView.model_validate(raw).saved_position is None
