"""保存指値・選択数量の金額比較は、評価価格や現在の採否から独立した表示。"""

from decimal import Decimal

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedProposalView
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("limit", ["900", "1000"])
def test_selected_limit_quantity_and_cash_survive_normal_save_and_read(tmp_path, limit):
    case = backend_case(tmp_path)
    raw = reviewed_proposal(case).payload()
    raw["items"][0]["terms"].update(
        limit_price_yen=limit, quantity=100, notional_yen=str(Decimal(limit) * 100)
    )
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    item = view.model_dump(mode="json")["items"][0]
    comparison = item["saved_buy_comparison"]
    assert comparison["quantity"] == 100
    assert comparison["quantity_basis"] == "author_selected"
    assert comparison["limit_price_yen"] == limit
    assert comparison["notional_yen"] == str(Decimal(limit) * 100)
    assert comparison["unspent_cash_yen"] == str(Decimal(300000) - Decimal(limit) * 100)
    assert comparison["cash_only_terminal_value_yen"] == "300000"
    assert Decimal(comparison["base"]["profit_loss_yen"]) == (Decimal(1100) - Decimal(limit)) * 100
    assert (
        Decimal(comparison["downside"]["profit_loss_yen"])
        == (Decimal(700) - Decimal(limit)) * 100
        < 0
    )
    assert (
        Decimal(comparison["base"]["capital_total_value_yen"])
        == Decimal(300000) + (Decimal(1100) - Decimal(limit)) * 100
    )
    assert item["assessment"]["valuation"]["price_yen"] == "1100"
    assert case.app.read_bytes() == before
    assert (
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
            proposal
        )
        == saved
    )
    assert case.app.read_bytes() == before

    # This read is not a fresh quote, ledger, or readiness check.
    case.market.rename(tmp_path / "retained-market.sqlite")
    assert read_proposal_views(case.app).latest.proposal == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("incomplete", ["terms", "valuation", "date"])
def test_saved_incomplete_or_different_date_does_not_invent_comparison(tmp_path, incomplete):
    case = backend_case(tmp_path)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    item = saved.items[0]
    document = case.pair.document
    if incomplete == "terms":
        item = item.model_copy(update={"terms": None, "proposed_limit_yen": Decimal(1000)})
    elif incomplete == "valuation":
        document = document.model_copy(
            update={
                "valuation": document.valuation.model_copy(
                    update={"status": "unresolved", "unresolved_reason": "合成旧評価の価値未算定"}
                )
            }
        )
    else:
        document = document.model_copy(
            update={
                "input_snapshot": document.input_snapshot.model_copy(
                    update={"as_of": document.input_snapshot.as_of.replace(day=13)}
                )
            }
        )
    # Historical read projection only; these modified objects are not published.
    from dataclasses import replace

    pair = replace(case.pair, document=document)
    view = project_saved_proposal(
        saved.model_copy(update={"items": (item,)}), {pair.assessment_id: pair}
    )
    assert view.model_dump(mode="json")["items"][0]["saved_buy_comparison"] is None


def test_old_serving_payload_and_nonbuy_remain_readable(tmp_path):
    case = backend_case(tmp_path)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    saved = service.publish(reviewed_proposal(case))
    raw = read_proposal_views(case.app).latest.proposal.model_dump(mode="json")
    raw["items"][0].pop("saved_buy_comparison")
    assert SavedProposalView.model_validate(raw).items[0].saved_buy_comparison is None
    no_action = saved.items[0].model_dump(mode="json")
    for key in ("timing", "terms", "proposed_limit_yen", "terms_unavailable_reasons"):
        no_action.pop(key)
    no_action["action"] = "no_action"
    from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal

    value = TradeProposal.model_validate({**saved.payload(), "items": [no_action]})
    view = project_saved_proposal(value, {case.pair.assessment_id: case.pair})
    assert "saved_buy_comparison" not in view.model_dump(mode="json")["items"][0]
