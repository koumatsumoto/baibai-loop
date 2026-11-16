"""Two-case algebra is neither a probability estimate nor an adoption rule."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.investment_judgment.stock_assessment.calculation import (
    CalculationInput,
    calculate,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
    current_price_projection,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import QuoteInput
from baibai_engine.investment_judgment.trade_proposal.entry_policy import evaluate_buy_terms


def document_payload(*, base=1200, downside=800, cash=0, months=12, rate=12):
    document, _ = pair_payload()
    document["input_snapshot"]["facts"][0]["value"] = 1000
    document["valuation"].update(horizon_months=months, required_annual_return_pct=rate)
    for name, value in (("base", base), ("downside", downside)):
        document["valuation"][name].update(
            terminal_value_per_share_yen=value, cash_distribution_per_share_yen=cash
        )
    return document


def calculation_payload(document, **extra):
    return {key: deepcopy(document[key]) for key in ("input_snapshot", "valuation")} | extra


def balance(document, **extra):
    result = calculate(CalculationInput.model_validate(calculation_payload(document, **extra)))
    return result["projection_numbers"]["two_case_balance"]


@pytest.mark.parametrize(
    ("base", "downside", "condition", "weight"),
    [
        (1200, 800, "minimum_base_weight", "50"),
        (1200, 1000, "any_base_weight", None),
        (1000, 800, "minimum_base_weight", "100"),
        (800, 1200, "maximum_base_weight", "50"),
        (1000, 1200, "any_base_weight", None),
        (800, 1000, "maximum_base_weight", "0"),
        (1200, 1100, "any_base_weight", None),
        (900, 800, "no_base_weight", None),
        (1000, 1000, "any_base_weight", None),
        (800, 800, "no_base_weight", None),
        (2000, 0, "minimum_base_weight", "50"),
    ],
)
def test_weight_conditions_do_not_assume_case_order_or_invent_impossible_probabilities(
    base, downside, condition, weight
):
    result = balance(document_payload(base=base, downside=downside))
    comparison = result["nominal_cash"]
    assert comparison == {
        "comparator_total_value_yen": "1000",
        "condition": condition,
        "break_even_base_weight_pct": weight,
    }
    assert result["scope"] == "base_downside_only"
    assert result["weight_basis"] == "algebraic_not_estimated_probability"
    assert result["outcome_assumption"] == "exactly_base_or_downside"
    assert result["criterion"] == "weighted_total_value_at_least_comparator"
    assert result["investor_taxes_and_fees_included"] is False
    assert result["cash_basis"] == "constant_nominal_without_interest"
    assert result["return_basis"] == "conditional_pretax_without_reinvestment"
    if weight is not None:
        fraction = Decimal(weight) / 100
        assert fraction * base + (1 - fraction) * downside == 1000


def test_cash_equality_and_required_return_are_different_same_horizon_conditions():
    result = balance(document_payload())
    assert result["nominal_cash"]["break_even_base_weight_pct"] == "50"
    assert result["required_return"] == {
        "comparator_total_value_yen": "1120.00",
        "condition": "minimum_base_weight",
        "break_even_base_weight_pct": "80.00",
    }
    later = balance(document_payload(months=24))
    assert later["nominal_cash"] == result["nominal_cash"]
    assert later["required_return"]["condition"] == "no_base_weight"
    assert later["required_return"]["break_even_base_weight_pct"] is None
    assert later["base_profit_loss_per_share_yen"] == "200"
    assert later["downside_profit_loss_per_share_yen"] == "-200"


def test_distribution_counts_once_and_hypothetical_price_and_quantity_do_not_change_inputs():
    document = document_payload(base="1000.125", downside="600.125", cash="100.875")
    inputs = calculation_payload(document)
    baseline = calculate(CalculationInput.model_validate(inputs))
    inputs["price_comparison"] = {"price_yen": "901", "hypothetical_quantity": 100}
    before = deepcopy(inputs)
    compared = calculate(CalculationInput.model_validate(inputs))
    assert inputs == before
    numbers = compared.pop("price_comparison_numbers")
    assert compared == baseline
    result = numbers["two_case_balance"]
    assert result["base_profit_loss_per_share_yen"] == "200.000"
    assert result["downside_profit_loss_per_share_yen"] == "-200.000"
    assert Decimal(result["nominal_cash"]["break_even_base_weight_pct"]) == 50
    assert numbers["base"]["profit_loss_yen"] == "20000.000"
    assert numbers["price_context"] == "hypothetical_entry"
    assert "price_as_of" not in numbers
    inputs["price_comparison"]["hypothetical_quantity"] = 1
    assert (
        calculate(CalculationInput.model_validate(inputs))["price_comparison_numbers"][
            "two_case_balance"
        ]
        == result
    )


def test_current_and_limit_comparisons_use_their_own_price_without_a_new_gate():
    # A severe opposite case can still be a reviewed, explicitly accepted conditional buy.
    document = StockAssessmentDocument.model_validate(document_payload(base=1120, downside=0))
    before = assessment_core_hash(document)
    as_of = document.input_snapshot.as_of
    result = evaluate_buy_terms(
        document,
        reviewed=True,
        latest=True,
        as_of=as_of,
        quote=QuoteInput(
            ticker="1234",
            price_yen=Decimal(1000),
            price_as_of=as_of,
            price_basis="last_close_unadjusted",
            source_ref="synthetic:quote",
        ),
        proposed_limit_yen=Decimal(800),
        basis_confirmed=True,
        market_price_max_age_days=7,
        held_quantity=0,
        active_reservation=False,
        proposal_executed=False,
        available_cash_yen=Decimal(1000000),
        budget_max_yen=Decimal(300000),
        board_lot=100,
        selected_quantity=100,
    )
    assert result.hard_blockers == ()
    assert result.quantity == 100
    assert result.maximum_price_yen == 1000
    original = result.current_price_projection["two_case_balance"]
    limit = result.limit_price_projection["two_case_balance"]
    assert Decimal(original["nominal_cash"]["comparator_total_value_yen"]) == 1000
    assert Decimal(limit["nominal_cash"]["comparator_total_value_yen"]) == 800
    assert original["required_return"]["break_even_base_weight_pct"] == "100.00"
    assert limit["required_return"]["break_even_base_weight_pct"] == "80.00"
    assert result.capital_comparison["downside"]["profit_loss_yen"] == "-80000"
    assert assessment_core_hash(document) == before


def test_unresolved_or_unusable_current_projection_does_not_get_a_balance():
    document = document_payload()
    document["valuation"] = {
        "status": "unresolved",
        "market_price_fact_id": None,
        "horizon_months": None,
        "required_annual_return_pct": None,
        "base": None,
        "downside": None,
        "unresolved_reason": "未採択のためケース混合を計算しない",
    }
    assert (
        calculate(CalculationInput.model_validate(calculation_payload(document)))[
            "projection_numbers"
        ]
        is None
    )
    resolved = StockAssessmentDocument.model_validate(document_payload())
    for as_of, confirmed in ((date(2026, 10, 15), True), (date(2026, 10, 14), False)):
        assert (
            current_price_projection(
                resolved,
                price_yen=Decimal(1000),
                price_as_of=date(2026, 10, 14),
                as_of=as_of,
                basis_confirmed=confirmed,
                max_quote_age_days=7,
            )
            is None
        )
