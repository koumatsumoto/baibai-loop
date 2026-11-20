from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.helpers.proposal import (
    PREPARED,
    PUBLISHED,
    backend_case,
    bind_review,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.stock_assessment.scenario import project_return
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService


def delay_at(document, price):
    valuation = document.valuation
    return {
        "additional_months": 12,
        "horizon_months": valuation.horizon_months + 12,
        "assumption": "terminal_and_cumulative_cash_unchanged",
        **{
            f"{name}_annualized_return_pct": float(
                round(
                    project_return(
                        projection,
                        price_yen=Decimal(price),
                        horizon_months=valuation.horizon_months + 12,
                    ).annualized_return_pct,
                    4,
                )
            )
            for name, projection in (("base", valuation.base), ("downside", valuation.downside))
        },
    }


def test_buy_context_check_and_preview_keep_price_specific_delay_without_new_gate(
    tmp_path, monkeypatch
):
    from baibai_engine.investment_judgment.stock_assessment import document as assessment_document
    from baibai_engine.investment_judgment.trade_proposal import conditions

    case = backend_case(tmp_path)
    draft = reviewed_proposal(case)
    calls = []
    old_basis = conditions.assessment_quote_basis
    old_check_derived = assessment_document._check_derived_metrics

    def basis(*args, **kwargs):
        calls.append("basis")
        return old_basis(*args, **kwargs)

    def check_derived(document, errors, *, check_new_inputs):
        # 保存済み評価の同一性確認ではなく、現在の価格適用に対する指標照合を全呼出で数える。
        if check_new_inputs:
            calls.append("economic_inputs")
        old_check_derived(document, errors, check_new_inputs=check_new_inputs)

    monkeypatch.setattr(conditions, "assessment_quote_basis", basis)
    monkeypatch.setattr(assessment_document, "_check_derived_metrics", check_derived)
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    assert calls == ["basis", "economic_inputs"]
    row = context.calculations["1234"]
    actual = row["buy_at_actual_quote"]["current_price_projection"]
    assert actual["fixed_value_delay"] == delay_at(case.pair.document, "1100")
    at_maximum = row["calculation_at_maximum_price"]["limit_price_projection"]
    assert at_maximum["fixed_value_delay"] == delay_at(case.pair.document, "1000")
    assert actual["fixed_value_delay"] != at_maximum["fixed_value_delay"]
    assert row["buy_at_actual_quote"]["capital_comparison"] is None
    assert (
        row["calculation_at_maximum_price"]["capital_comparison"]["cash_only_terminal_value_yen"]
        == "300000"
    )

    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    calls.clear()
    checked = service.check(draft)
    assert calls == ["basis", "economic_inputs"]
    assert checked.valid
    comparison = checked.calculations["1234"]["limit_price_projection"]
    assert comparison["horizon_months"] == 12
    assert comparison["base"]["annualized_return_pct"] == 10
    assert comparison["fixed_value_delay"]["base_annualized_return_pct"] == 4.8809
    assert comparison["fixed_value_delay"]["base_annualized_return_pct"] < float(
        case.pair.document.valuation.required_annual_return_pct
    )
    assert comparison["price_context"] == "buy_limit_ceiling"
    assert comparison["calculation_price_yen"] == "1000"
    assert not {"price_yen", "price_as_of", "price_basis"} & comparison.keys()

    calls.clear()
    saved = service.publish(draft)
    assert calls == ["basis", "economic_inputs"]
    before = case.app.read_bytes()
    calls.clear()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=1),
    )
    assert calls == ["basis", "economic_inputs"]
    assert preview.status == "unchanged"
    result = preview.items[0]["saved_limit_revalidation"]
    assert result["limit_price_projection"] == comparison
    assert result["hard_blockers"] == []
    assert result["quantity"] == 300
    assert Decimal(result["maximum_price_yen"]) == Decimal(1000)
    assert "fixed_value_delay" not in saved.model_dump_json()
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("action", ["hold", "sell"])
def test_holding_comparison_keeps_actual_quote_and_author_floor_separate(tmp_path, action):
    case = backend_case(tmp_path, holding=True)
    draft = reviewed_proposal(case, action="hold")
    raw = draft.model_dump(mode="json")
    item = raw["items"][0]
    item["remaining_reward"] = {
        "status": "sufficient" if action == "hold" else "insufficient",
        "reason": "合成の価格別比較。遅延の単位感応度だけで採否を変更しない。",
    }
    if action == "sell":
        item.pop("holding_basis")
        item.pop("return_calculation_unavailable_reason")
        item.update(
            action="sell",
            reason_code="insufficient_remaining_reward",
            terms={
                "side": "sell",
                "limit_price_yen": "900",
                "quantity": 100,
                "notional_yen": "90000",
                "target_session": "2026-10-15",
                "valuation_as_of": "2026-10-14",
                "maximum_entry_price_yen": None,
                "price_grid_rule_id": "tse-common-conservative-20261014",
                "conditions": ["下限と保有継続を比較した合成条件。"],
                "warnings": [],
            },
            terms_unavailable_reasons=[],
        )
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    checked = service.check(draft)
    assert checked.valid
    calculations = checked.calculations["1234"]
    assert calculations["current_price_projection"]["fixed_value_delay"] == delay_at(
        case.pair.document, "1100"
    )
    if action == "sell":
        floor = calculations["sell_limit_projection"]
        assert floor["fixed_value_delay"] == delay_at(case.pair.document, "900")
        assert floor["price_context"] == "author_sell_minimum"
        assert Decimal(floor["observed_quote"]["price_yen"]) == Decimal(1100)
    saved = service.publish(draft)
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=1),
    )
    assert preview.status == "unchanged"
    assert preview.items[0]["saved_action"] == action
    assert (
        preview.items[0]["current_quote_calculation"]["current_price_projection"]
        == (calculations["current_price_projection"])
    )
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before
