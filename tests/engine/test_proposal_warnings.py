import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from baibai_engine.research.assessment import StockAssessmentDocument
from baibai_engine.research.proposal import (
    BuyItem,
    GateReason,
    HoldItem,
    InvestmentProposal,
    LiquidityContext,
    PositionInput,
    QuoteInput,
    ReservationInput,
    SellItem,
)
from baibai_engine.research.proposal_warnings import build_proposal_warnings


@pytest.fixture
def case():
    path = Path(__file__).parents[1] / "fixtures/domain-assessment-proposal-synthetic.json"
    fixture = json.loads(path.read_text())
    return (
        StockAssessmentDocument.model_validate(fixture["assessment_publication"]["document"]),
        InvestmentProposal.model_validate(fixture["investment_proposal"]),
    )


def calculate(case, **overrides):
    document, proposal = case
    return build_proposal_warnings(
        **dict(
            inputs=proposal.inputs,
            items=proposal.items,
            liquidity_context=proposal.liquidity_context,
            assessments={proposal.items[0].assessment_ref.assessment_id: document},
        )
        | overrides,
    )


def buy(proposal, *, limit=1000, quantity=100):
    item = proposal.items[0]
    return item.model_copy(
        update={
            "terms": item.terms.model_copy(
                update={
                    "limit_price_yen": Decimal(limit),
                    "quantity": quantity,
                    "notional_yen": Decimal(limit) * quantity,
                }
            )
        }
    )


def position(ticker="5678", *, factors=("domestic-demand",), sector="情報・通信業", **overrides):
    return PositionInput(
        **dict(
            ticker=ticker,
            sector=sector,
            common_factors=factors,
            quantity=100,
            cost_yen=Decimal(50000),
            quantity_basis_confirmed=True,
            original_reason_unavailable_reason="合成の当初理由は確認できない",
        )
        | overrides,
    )


def quote(ticker="5678", *, price=1000, **overrides):
    return QuoteInput(
        **dict(
            ticker=ticker,
            price_yen=Decimal(price),
            price_as_of=date(2026, 10, 14),
            observed_at=datetime.fromisoformat("2026-10-14T15:30:00+09:00"),
            price_basis="last_close_unadjusted",
            source_ref="fixture:daily_bars",
            basis_confirmed=True,
        )
        | overrides,
    )


def reservation(ticker="1234", *, factors=("domestic-demand",), sector="情報・通信業"):
    return ReservationInput(
        reservation_id="synthetic-active-order",
        ticker=ticker,
        reserved_yen=Decimal(50000),
        decision_reference="synthetic-prior-proposal",
        sector=sector,
        common_factors=factors,
    )


def liquidity(adv):
    return (
        LiquidityContext(
            ticker="1234",
            run_revision_id="run-revision-" + "a" * 32,
            analysis_as_of=date(2026, 10, 13),
            rules_hash="synthetic-fixed-rules",
            adv_yen=Decimal(adv),
            unavailable_reason=None,
        ),
    )


def test_synthetic_appendix_has_expected_saved_diagnostics(case):
    _, proposal = case
    result = calculate(case)
    assert result.terms_warnings["1234"] == proposal.items[0].terms.warnings
    assert result.warnings == ()
    assert result.portfolio_exposure == proposal.portfolio_exposure
    assert result.portfolio_exposure.ticker.prospective_pct == 100
    assert result.portfolio_exposure.ticker.warning_pct == 10
    assert result.portfolio_exposure.sector.warning_pct == 40
    assert result.portfolio_exposure.common_factors[0].warning_pct == 35


@pytest.mark.parametrize(
    ("limit", "quantity", "minimum", "maximum", "expected"),
    [
        (900, 100, 100000, 300000, "budget_guide_under"),
        (1000, 100, 50000, 50000, "budget_guide_exceeded"),
    ],
)
def test_budget_guide_lines_remain_warnings(case, limit, quantity, minimum, maximum, expected):
    _, proposal = case
    result = calculate(
        case,
        inputs=proposal.inputs.model_copy(
            update={"budget_min_yen": minimum, "budget_max_yen": maximum}
        ),
        items=(buy(proposal, limit=limit, quantity=quantity),),
    )
    assert expected in result.terms_warnings["1234"]


@pytest.mark.parametrize(
    ("adv", "expected"),
    [
        ("0", "adv_participation_unassessed"),
        ("-1", "adv_participation_unassessed"),
        ("5999999", "adv_participation_exceeds_warning"),
    ],
)
def test_nonpositive_adv_is_retained_but_unassessed_and_positive_adv_uses_existing_line(
    case, adv, expected
):
    context = liquidity(adv)
    result = calculate(case, liquidity_context=context)
    assert expected in result.terms_warnings["1234"]
    assert context[0].adv_yen == Decimal(adv)


def test_adv_threshold_equality_and_missing_context(case):
    assert (
        "adv_participation_exceeds_warning"
        not in calculate(case, liquidity_context=liquidity("6000000")).terms_warnings["1234"]
    )
    assert (
        "adv_participation_unassessed"
        in calculate(case, liquidity_context=()).terms_warnings["1234"]
    )


def test_wait_limit_notional_and_active_reservations_drive_all_concentrations(case):
    _, proposal = case
    inputs = proposal.inputs.model_copy(
        update={
            "positions": (position(),),
            "active_reservations": (reservation(),),
            "quotes": (*proposal.inputs.quotes, quote()),
            "available_cash_yen": Decimal(850000),
            "total_capital_yen": Decimal(1000000),
        }
    )
    result = calculate(case, inputs=inputs, items=(buy(proposal, limit=900),))
    exposure = result.portfolio_exposure
    assert exposure.ticker.prospective_pct == 14
    assert exposure.sector.prospective_pct == 24
    assert exposure.common_factors[0].prospective_pct == exposure.sector.prospective_pct
    assert "prospective_ticker_concentration_exceeds_warning:1234" in result.terms_warnings["1234"]
    assert (
        "prospective_sector_concentration_exceeds_warning:情報・通信業"
        not in result.terms_warnings["1234"]
    )


@pytest.mark.parametrize("unknown", ["missing", "stale", "quote_basis", "quantity_basis"])
def test_one_unvalued_holding_leaves_nav_unknown_instead_of_zero(case, unknown):
    _, proposal = case
    held = position(quantity_basis_confirmed=unknown != "quantity_basis")
    held_quote = quote(
        price_as_of=date(2026, 10, 1) if unknown == "stale" else date(2026, 10, 14),
        basis_confirmed=unknown != "quote_basis",
    )
    inputs = proposal.inputs.model_copy(
        update={
            "positions": (held,),
            "quotes": proposal.inputs.quotes
            if unknown == "missing"
            else (*proposal.inputs.quotes, held_quote),
            "total_capital_yen": None,
        }
    )
    result = calculate(case, inputs=inputs)
    assert "portfolio_valuation_unknown:5678" in result.warnings
    assert "concentration_and_dry_powder_unassessed" in result.warnings
    assert result.portfolio_exposure.total_capital_yen is None
    assert result.portfolio_exposure.ticker is None
    assert result.portfolio_exposure.sector is None
    assert result.portfolio_exposure.common_factors == ()


def test_common_factor_coverage_keeps_unknown_holdings_reservations_and_buy_visible(case):
    document, proposal = case
    document = document.model_copy(
        update={"input_snapshot": document.input_snapshot.model_copy(update={"common_factors": ()})}
    )
    inputs = proposal.inputs.model_copy(
        update={
            "positions": (position(factors=()),),
            "active_reservations": (reservation("9999", factors=()),),
            "quotes": (*proposal.inputs.quotes, quote()),
            "total_capital_yen": Decimal(450000),
        }
    )
    result = calculate(
        case,
        inputs=inputs,
        assessments={proposal.items[0].assessment_ref.assessment_id: document},
    )
    assert "portfolio_exposure_common_factor_coverage_incomplete" in result.warnings
    assert result.portfolio_exposure.common_factor_unclassified_tickers == ("1234", "5678", "9999")
    assert result.portfolio_exposure.common_factors == ()


@pytest.mark.parametrize("nav", [None, Decimal(0), Decimal(-1)])
def test_unknown_or_nonpositive_total_capital_makes_concentration_unassessed(case, nav):
    _, proposal = case
    result = calculate(case, inputs=proposal.inputs.model_copy(update={"total_capital_yen": nav}))
    assert "concentration_and_dry_powder_unassessed" in result.warnings
    assert result.portfolio_exposure.ticker is None
    assert result.portfolio_exposure.total_capital_yen == nav


@pytest.mark.parametrize(("cash", "warns"), [(299999, True), (300000, False)])
def test_dry_powder_floor_is_a_warning_and_equality_is_allowed(case, cash, warns):
    _, proposal = case
    result = calculate(
        case,
        inputs=proposal.inputs.model_copy(
            update={"available_cash_yen": Decimal(cash), "total_capital_yen": Decimal(1000000)}
        ),
        items=(buy(proposal),),
    )
    assert ("dry_powder_below_floor" in result.terms_warnings["1234"]) is warns


@pytest.mark.parametrize("action", ["hold", "sell"])
def test_holding_only_has_no_prospective_purchase_but_keeps_missing_portfolio_diagnostics(
    case, action
):
    _, proposal = case
    held = position("1234", factors=())
    common = dict(
        ticker="1234",
        action=action,
        assessment_ref=proposal.items[0].assessment_ref,
        position=held,
        rationale="合成の保有見直し",
        review_conditions=("仮説変化時に再確認",),
        case_reassessment_reason="本日の反証を確認",
    )
    item = (
        HoldItem(**common, holding_basis="investment_case_intact")
        if action == "hold"
        else SellItem(
            **common,
            reason_code="economic_invalidation",
            remaining_reward=None,
            terms=None,
            terms_unavailable_reasons=(
                GateReason(code="quote_missing_or_stale", message="合成欠測"),
            ),
        )
    )
    inputs = proposal.inputs.model_copy(
        update={"positions": (held,), "quotes": (), "total_capital_yen": None}
    )
    result = calculate(case, inputs=inputs, items=(item,), assessments={})
    assert result.terms_warnings == {}
    assert result.portfolio_exposure.ticker is None
    assert result.portfolio_exposure.sector is None
    assert result.portfolio_exposure.common_factors == ()
    assert "portfolio_valuation_unknown:1234" in result.warnings
    assert "portfolio_exposure_common_factor_coverage_incomplete" in result.warnings


def test_unexecuted_sell_does_not_add_proceeds_or_remove_holding_exposure(case):
    _, proposal = case
    held = position()
    sell = SellItem(
        ticker="5678",
        action="sell",
        assessment_ref=proposal.items[0].assessment_ref,
        position=held,
        rationale="合成売却判断",
        review_conditions=("事業前提を再確認",),
        case_reassessment_reason="合成の反証確認",
        reason_code="economic_invalidation",
        remaining_reward=None,
        terms=proposal.items[0].terms.model_copy(
            update={
                "side": "sell",
                "maximum_entry_price_yen": None,
                "quantity": 100,
                "notional_yen": Decimal(100000),
            }
        ),
    )
    inputs = proposal.inputs.model_copy(
        update={
            "positions": (held,),
            "quotes": (*proposal.inputs.quotes, quote()),
            "available_cash_yen": Decimal(100000),
            "total_capital_yen": Decimal(200000),
        }
    )
    result = calculate(case, inputs=inputs, items=(buy(proposal), sell))
    assert "dry_powder_below_floor" in result.terms_warnings["1234"]
    assert result.portfolio_exposure.sector.prospective_pct == 100
    assert inputs.available_cash_yen == 100000


def test_saved_warning_payload_is_not_rewritten_by_another_snapshot(case):
    _, proposal = case
    original_json = proposal.model_dump_json()
    original_result = calculate(case)
    current_inputs = proposal.inputs.model_copy(
        update={"total_capital_yen": Decimal(10000000), "available_cash_yen": Decimal(10000000)}
    )
    current_result = calculate(
        case, inputs=current_inputs, liquidity_context=liquidity("1000000000")
    )
    assert original_result != current_result
    assert "dry_powder_below_floor" in original_result.terms_warnings["1234"]
    assert "dry_powder_below_floor" not in current_result.terms_warnings["1234"]
    assert proposal.model_dump_json() == original_json


def test_buy_metadata_cannot_silently_become_unknown_or_use_another_ticker(case):
    document, proposal = case
    with pytest.raises(ValueError, match="exact assessment"):
        calculate(case, assessments={})
    wrong = document.model_copy(
        update={"input_snapshot": document.input_snapshot.model_copy(update={"ticker": "5678"})}
    )
    with pytest.raises(ValueError, match="exact assessment"):
        calculate(case, assessments={proposal.items[0].assessment_ref.assessment_id: wrong})


def test_terms_unavailable_has_no_concrete_buy_warnings_or_prospective_rows(case):
    _, proposal = case
    partial = BuyItem.model_validate(
        proposal.items[0].model_dump()
        | {
            "terms": None,
            "proposed_limit_yen": Decimal(1000),
            "terms_unavailable_reasons": (
                GateReason(code="price_grid_rule_unavailable", message="将来の制度未確認"),
            ),
        }
    )
    result = calculate(case, items=(partial,), assessments={})
    assert result.terms_warnings == {}
    assert result.portfolio_exposure.ticker is None


def test_multiple_buys_use_shared_initial_policy_limit(case):
    _, proposal = case
    with pytest.raises(ValueError, match="at most 1 new buy"):
        calculate(
            case, items=(proposal.items[0], proposal.items[0].model_copy(update={"ticker": "5678"}))
        )
