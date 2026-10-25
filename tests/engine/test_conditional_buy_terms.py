from datetime import date
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.investment_judgment.entry_policy import evaluate_buy_terms
from baibai_engine.investment_judgment.stock_assessment import StockAssessmentDocument
from baibai_engine.investment_judgment.trade_proposal import QuoteInput

AS_OF = date(2026, 10, 14)


def assessment():
    payload, _ = pair_payload()
    return StockAssessmentDocument.model_validate(payload)


def quote(price="1100", **overrides):
    return QuoteInput(
        **dict(
            ticker="1234",
            price_yen=Decimal(price),
            price_as_of=AS_OF,
            price_basis="last_close_unadjusted",
            source_ref="fixture:daily_bars",
        )
        | overrides
    )


def evaluate(document=None, **overrides):
    return evaluate_buy_terms(
        assessment() if document is None else document,
        **dict(
            reviewed=True,
            latest=True,
            as_of=AS_OF,
            quote=quote(),
            proposed_limit_yen=Decimal(1000),
            basis_confirmed=True,
            market_price_max_age_days=7,
            held_quantity=0,
            active_reservation=False,
            proposal_executed=False,
            available_cash_yen=Decimal(300000),
            budget_max_yen=Decimal(300000),
            board_lot=100,
        )
        | overrides,
    )


def codes(result):
    return {reason.code for reason in result.hard_blockers}


def test_real_close_above_pmax_keeps_limit_proposal_and_separate_returns():
    result = evaluate()
    assert result.hard_blockers == ()
    assert result.maximum_price_yen == 1000
    assert result.current_price_above_maximum is True
    assert result.quantity == 300
    assert result.current_price_projection["price_yen"] == 1100
    assert result.current_price_projection["base"]["total_return_pct"] == 0
    assert result.limit_price_projection["price_yen"] == 1000
    assert result.limit_price_projection["base"]["total_return_pct"] == 10
    assert result.limit_price_projection["downside"]["total_return_pct"] == -30


@pytest.mark.parametrize(("limit", "expected"), [("999", 300), ("1000", 300), ("1001", 0)])
def test_decimal_price_limit_boundary(limit, expected):
    result = evaluate(proposed_limit_yen=Decimal(limit))
    assert result.quantity == expected
    assert ("limit_above_pmax" in codes(result)) is (expected == 0)


def test_cash_is_checked_at_recommended_limit_and_guide_is_not_a_hard_cap():
    result = evaluate(
        quote=quote("1000"), proposed_limit_yen=Decimal(900), available_cash_yen=Decimal(95000)
    )
    assert result.hard_blockers == ()
    assert result.quantity == 100
    result = evaluate(budget_max_yen=Decimal(50000))
    assert result.hard_blockers == ()
    assert result.quantity == 100
    assert evaluate(available_cash_yen=Decimal(99999)).quantity == 0


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"reviewed": False}, "reviewed_assessment_required"),
        ({"latest": False}, "latest_assessment_required"),
        ({"as_of": date(2026, 10, 15)}, "valuation_basis_requires_refresh"),
        ({"held_quantity": 100}, "already_held"),
        ({"active_reservation": True}, "active_reservation_exists"),
        ({"proposal_executed": True}, "proposal_already_executed"),
        ({"basis_confirmed": False}, "corporate_action_unresolved"),
        ({"quote": None}, "quote_missing_or_stale"),
        ({"available_cash_yen": Decimal(0)}, "available_cash_below_board_lot"),
        ({"proposed_limit_yen": Decimal(0)}, "positive_limit_required"),
        ({"proposed_limit_yen": Decimal(-1)}, "positive_limit_required"),
    ],
)
def test_discount_limit_cannot_bypass_any_hard_gate(overrides, blocker):
    payload, _ = pair_payload()
    payload["valuation"]["required_annual_return_pct"] = "3"
    result = evaluate(
        StockAssessmentDocument.model_validate(payload),
        **dict(proposed_limit_yen=Decimal(900)) | overrides,
    )
    assert blocker in codes(result)
    assert result.quantity == 0


@pytest.mark.parametrize("price_day", [date(2026, 10, 6), date(2026, 10, 15)])
def test_stale_and_future_real_quotes_cannot_be_replaced_with_a_limit(price_day):
    result = evaluate(quote=quote(price_as_of=price_day), proposed_limit_yen=Decimal(900))
    assert "quote_missing_or_stale" in codes(result)
    assert result.current_price_projection is None
    assert result.limit_price_projection is None


@pytest.mark.parametrize(
    ("stance", "case"), [("unsupported", "broken"), ("uncertain", "uncertain")]
)
def test_assessment_support_and_intact_case_are_required(stance, case):
    document = assessment()
    document = document.model_copy(
        update={
            "conclusion": document.conclusion.model_copy(update={"stance": stance}),
            "investment_case": document.investment_case.model_copy(update={"status": case}),
        }
    )
    assert "assessment_not_supported" in codes(evaluate(document))


def test_unresolved_valuation_blocks_buy_without_inventing_pmax():
    document = assessment()
    document = document.model_copy(
        update={
            "valuation": document.valuation.model_copy(
                update={"status": "unresolved", "base": None}
            )
        }
    )
    result = evaluate(document)
    assert "valuation_unresolved" in codes(result)
    assert result.maximum_price_yen is None
    assert result.quantity == 0


def test_downside_does_not_create_a_new_entry_threshold():
    document = assessment()
    document = document.model_copy(
        update={
            "valuation": document.valuation.model_copy(
                update={
                    "downside": document.valuation.downside.model_copy(
                        update={"terminal_value_per_share_yen": Decimal(0)}
                    )
                }
            )
        }
    )
    result = evaluate(document)
    assert result.hard_blockers == ()
    assert result.maximum_price_yen == 1000
    assert result.limit_price_projection["downside"]["total_return_pct"] == -100


@pytest.mark.parametrize("limit", [True, Decimal("NaN"), Decimal("Infinity")])
def test_nonfinite_or_boolean_limit_cannot_enter_arithmetic(limit):
    with pytest.raises(ValueError, match="finite decimal"):
        evaluate(proposed_limit_yen=limit)


def test_quote_ticker_must_resolve_to_assessment():
    with pytest.raises(ValueError, match="ticker differ"):
        evaluate(quote=quote(ticker="5678"))


@pytest.mark.parametrize("lot", [0, -1, True, Decimal(100), 100.0])
def test_board_lot_is_a_positive_integer(lot):
    with pytest.raises(ValueError, match="positive board_lot"):
        evaluate(board_lot=lot)
