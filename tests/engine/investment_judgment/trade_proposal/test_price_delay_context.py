from __future__ import annotations

from datetime import date, timedelta
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

from baibai_engine.investment_judgment.stock_assessment.document import current_price_projection
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


def test_buy_context_check_and_preview_keep_price_specific_delay_without_new_gate(tmp_path):
    case = backend_case(tmp_path)
    draft = reviewed_proposal(case)
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    row = context.calculations["1234"]
    actual = row["buy_at_actual_quote"]["current_price_projection"]
    assert actual["fixed_value_delay"] == delay_at(case.pair.document, "1100")
    at_maximum = row["calculation_at_maximum_price"]["limit_price_projection"]
    assert at_maximum["fixed_value_delay"] == delay_at(case.pair.document, "1000")
    assert actual["fixed_value_delay"] != at_maximum["fixed_value_delay"]

    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    checked = service.check(draft)
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

    saved = service.publish(draft)
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=1),
    )
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


@pytest.mark.parametrize("missing", ["stale", "future", "basis", "old_valuation", "unresolved"])
def test_unavailable_basis_does_not_gain_a_delay_projection(tmp_path, missing):
    case = backend_case(tmp_path, valuation_unresolved=missing == "unresolved")
    document = case.pair.document
    values = dict(
        price_yen=Decimal(1000),
        price_as_of=date(2026, 10, 14),
        as_of=date(2026, 10, 14),
        basis_confirmed=True,
        max_quote_age_days=7,
    )
    if missing == "stale":
        values["price_as_of"] = date(2026, 10, 1)
    elif missing == "future":
        values["price_as_of"] = date(2026, 10, 15)
    elif missing == "basis":
        values["basis_confirmed"] = False
    elif missing == "old_valuation":
        values["as_of"] = date(2026, 10, 15)
    assert current_price_projection(document, **values) is None


@pytest.mark.parametrize("downside_value", [Decimal(0), Decimal(700)])
def test_delay_does_not_reduce_loss_amount_or_extend_saved_horizon(tmp_path, downside_value):
    case = backend_case(tmp_path)
    document = case.pair.document
    document = document.model_copy(
        update={
            "valuation": document.valuation.model_copy(
                update={
                    "downside": document.valuation.downside.model_copy(
                        update={
                            "terminal_value_per_share_yen": downside_value,
                            "cash_distribution_per_share_yen": Decimal(0),
                        }
                    )
                }
            )
        }
    )
    before = document.model_dump_json()
    result = current_price_projection(
        document,
        price_yen=Decimal(1000),
        price_as_of=date(2026, 10, 14),
        as_of=date(2026, 10, 14),
        basis_confirmed=True,
        max_quote_age_days=7,
    )
    assert result["downside"]["total_value_yen"] == downside_value
    assert result["downside"]["total_return_pct"] == (downside_value / 1000 - 1) * 100
    assert result["fixed_value_delay"] == delay_at(document, "1000")
    if downside_value == 0:
        assert result["fixed_value_delay"]["downside_annualized_return_pct"] == -100
    else:
        assert (
            result["downside"]["annualized_return_pct"]
            < result["fixed_value_delay"]["downside_annualized_return_pct"]
            < 0
        )
    assert result["horizon_months"] == 12
    assert document.model_dump_json() == before
