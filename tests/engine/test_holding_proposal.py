from datetime import date
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.investment_judgment.stock_assessment import StockAssessmentDocument
from baibai_engine.investment_judgment.holding_policy import evaluate_holding_proposal
from baibai_engine.investment_judgment.trade_proposal import HoldItem, PositionInput, QuoteInput, RemainingReward

AS_OF = date(2026, 10, 14)


def assessment(*, case="intact", as_of=AS_OF):
    payload, _ = pair_payload()
    document = StockAssessmentDocument.model_validate(payload)
    return document.model_copy(
        update={
            "input_snapshot": document.input_snapshot.model_copy(update={"as_of": as_of}),
            "investment_case": document.investment_case.model_copy(update={"status": case}),
        }
    )


def position(**overrides):
    return PositionInput(
        **dict(
            ticker="1234",
            sector="情報・通信業",
            common_factors=(),
            quantity=200,
            cost_yen=Decimal(200000),
            quantity_basis_confirmed=True,
            original_investment_reason="合成事業の継続収益を確認",
        )
        | overrides
    )


def evaluate(document=None, **overrides):
    return evaluate_holding_proposal(
        assessment() if document is None else document,
        **dict(
            position=position(),
            as_of=AS_OF,
            quote=QuoteInput(
                ticker="1234",
                price_yen=Decimal(1000),
                price_as_of=AS_OF,
                price_basis="last_close_unadjusted",
                source_ref="fixture:daily_bars",
            ),
            price_basis_confirmed=True,
            remaining_reward=None,
            case_reassessment_reason="本日の反証確認で仮説維持を確認",
            primary_verified=True,
            reviewed=True,
            latest=True,
            max_quote_age_days=7,
        )
        | overrides,
    )


def reward(status):
    return RemainingReward(status=status, reason="合成の残存見返りを内容 Review で確認")


def test_broken_verified_sells_before_price_or_quantity_arithmetic():
    result = evaluate(
        assessment(case="broken", as_of=date(2026, 10, 1)),
        quote=None,
        position=position(quantity_basis_confirmed=False),
        remaining_reward=reward("sufficient"),
    )
    assert result.action == "sell"
    assert result.reason_code == "economic_invalidation"
    assert result.hard_blockers == ()
    assert result.sell_quantity is None
    assert result.current_price_projection is None
    assert evaluate(assessment(case="broken"), quote=None).sell_quantity == 200


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"primary_verified": False}, "primary_evidence_requires_verification"),
        ({"reviewed": False}, "reviewed_assessment_required"),
        ({"latest": False}, "latest_assessment_required"),
        ({"case_reassessment_reason": None}, "case_reassessment_required"),
        ({"case_reassessment_reason": "  "}, "case_reassessment_required"),
    ],
)
def test_explicit_current_case_check_and_reviewed_latest_pair_are_required(overrides, code):
    result = evaluate(**overrides)
    assert result.action == "defer"
    assert code in {reason.code for reason in result.hard_blockers}


def test_uncertain_case_or_reward_stays_deferred():
    assert evaluate(assessment(case="uncertain")).reason_code == "investment_case_uncertain"
    result = evaluate(remaining_reward=reward("uncertain"))
    assert result.action == "defer"
    assert result.reason_code == "remaining_reward_requires_confirmation"


def test_intact_currently_reconfirmed_holds_with_old_valuation_without_a_return_claim():
    result = evaluate(assessment(as_of=date(2026, 10, 1)), quote=None)
    assert result.action == "hold"
    assert result.holding_basis == "investment_case_intact"
    assert result.current_price_projection is None
    assert result.return_calculation_unavailable_reason == "valuation_basis_requires_refresh"
    document = assessment()
    document = document.model_copy(
        update={"valuation": document.valuation.model_copy(update={"status": "unresolved"})}
    )
    result = evaluate(document)
    assert result.action == "hold"
    assert result.return_calculation_unavailable_reason == "valuation_unresolved"


@pytest.mark.parametrize("status", ["sufficient", "insufficient"])
def test_explicit_remaining_reward_requires_current_arithmetic(status):
    result = evaluate(assessment(as_of=date(2026, 10, 1)), remaining_reward=reward(status))
    assert result.action == "defer"
    assert result.reason_code == "valuation_basis_requires_refresh"
    assert result.holding_basis is None
    assert evaluate(quote=None, remaining_reward=reward(status)).action == "defer"


def test_confirmed_remaining_reward_controls_hold_or_entire_sale_without_buy_floor():
    hold = evaluate(remaining_reward=reward("sufficient"))
    assert hold.action == "hold"
    assert hold.holding_basis == "sufficient_remaining_reward"
    assert hold.current_price_projection["base"]["total_return_pct"] == 10
    sell = evaluate(remaining_reward=reward("insufficient"))
    assert sell.action == "sell"
    assert sell.reason_code == "insufficient_remaining_reward"
    assert sell.sell_quantity == 200
    # 12% の buy floor を要求する policy があっても、holding にその引数はない。
    assert evaluate().action == "hold"


def test_unknown_quantity_basis_does_not_invent_returns_or_partial_sale():
    result = evaluate(position=position(quantity_basis_confirmed=False))
    assert result.action == "hold"
    assert result.return_calculation_unavailable_reason == "quantity_basis_unconfirmed"
    result = evaluate(
        position=position(quantity_basis_confirmed=False), remaining_reward=reward("insufficient")
    )
    assert result.action == "defer"
    assert result.sell_quantity is None


def test_hold_contract_rejects_known_insufficient_and_never_accepts_order_terms():
    from baibai_engine.investment_judgment.stock_assessment import AssessmentRef

    values = dict(
        ticker="1234",
        action="hold",
        assessment_ref=AssessmentRef(
            assessment_id="stock-assessment-00000000-0000-4000-8000-000000000001",
            core_sha256="a" * 64,
            review_id="assessment-review-00000000-0000-4000-8000-000000000002",
        ),
        position=position(),
        holding_basis="investment_case_intact",
        case_reassessment_reason="本日の反証を再確認",
        rationale="仮説が維持されている",
        review_conditions=("仮説の反証時に再確認",),
    )
    with pytest.raises(ValueError, match="insufficient"):
        HoldItem(**values, remaining_reward=reward("insufficient"))
    with pytest.raises(ValueError, match="Extra inputs"):
        HoldItem(**values, terms=None)


def test_position_ticker_cannot_be_rebound_to_another_assessment():
    with pytest.raises(ValueError, match="ticker differ"):
        evaluate(position=position(ticker="5678"))
