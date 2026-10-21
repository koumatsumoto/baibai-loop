"""旧判断テストから、要求率とquote不足の意味を新評価/提案へ引き継ぐ。"""

from datetime import date
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.research.assessment import StockAssessmentDocument, assessment_core_hash
from baibai_engine.research.entry_policy import evaluate_buy_terms
from baibai_engine.research.holding_policy import evaluate_holding_proposal
from baibai_engine.research.proposal import PositionInput, QuoteInput, RemainingReward

AS_OF = date(2026, 10, 14)


def assessment(*, rate="10", case="intact", resolved=True):
    payload, _ = pair_payload()
    payload["valuation"]["required_annual_return_pct"] = rate
    payload["investment_case"]["status"] = case
    if case != "intact":
        payload["conclusion"]["stance"] = "unsupported" if case == "broken" else "uncertain"
    if not resolved:
        payload["conclusion"]["stance"] = "uncertain"
        payload["valuation"] = dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="評価に必要な根拠が未確認",
        )
    return StockAssessmentDocument.model_validate(payload)


def buy(document, *, limit=Decimal(1000)):
    return evaluate_buy_terms(
        document,
        reviewed=True,
        latest=True,
        as_of=AS_OF,
        quote=QuoteInput(
            ticker="1234",
            price_yen=Decimal(1100),
            price_as_of=AS_OF,
            price_basis="last_close_unadjusted",
            source_ref="fixture:daily_bars",
        ),
        proposed_limit_yen=limit,
        basis_confirmed=True,
        market_price_max_age_days=7,
        held_quantity=0,
        active_reservation=False,
        proposal_executed=False,
        available_cash_yen=Decimal(300000),
        budget_max_yen=Decimal(300000),
        board_lot=100,
    )


@pytest.mark.parametrize("rate", ["0.0001", "3", "5.9999", "6", "6.0001"])
def test_positive_case_rate_is_not_a_uniform_buy_floor(rate):
    document = assessment(rate=rate)
    result = buy(document)
    assert result.quantity == 300
    assert result.hard_blockers == ()
    assert result.maximum_price_yen > Decimal(1000)
    # The actual quote stays above Pmax; a low rate never makes any price acceptable.
    assert result.current_price_above_maximum is True
    at_maximum = buy(document, limit=result.maximum_price_yen)
    assert at_maximum.hard_blockers == ()
    above = buy(document, limit=result.maximum_price_yen + Decimal("0.000000001"))
    assert above.quantity == 0
    assert tuple(reason.code for reason in above.hard_blockers) == ("limit_above_pmax",)


@pytest.mark.parametrize("rate", [0, "-0.0001", True, "NaN", "Infinity", "-Infinity", None])
def test_required_rate_must_be_positive_finite_and_explicit(rate):
    with pytest.raises(ValueError, match=r"finite|greater than 0|resolved valuation requires"):
        assessment(rate=rate)


def test_case_rate_is_not_a_percentage_share_capped_at_one_hundred():
    document = assessment(rate="101")
    result = buy(document, limit=Decimal(500))
    assert result.quantity > 0
    assert result.maximum_price_yen < Decimal(550)


def test_case_rate_does_not_rewrite_existing_assessment_hash_or_pmax():
    document = assessment(rate="8.5")
    original_json, original_hash = document.model_dump_json(), assessment_core_hash(document)
    current = buy(document, limit=Decimal(1030))
    assert current.maximum_price_yen.quantize(Decimal("0.0001")) == Decimal("1013.8249")
    assert tuple(reason.code for reason in current.hard_blockers) == ("limit_above_pmax",)
    assert document.model_dump_json() == original_json
    assert assessment_core_hash(document) == original_hash


@pytest.mark.parametrize("case", ["broken", "uncertain"])
def test_low_case_rate_never_turns_an_uncertain_or_broken_case_into_a_buy(case):
    result = buy(assessment(rate="3", case=case))
    assert result.quantity == 0
    assert any(reason.code == "assessment_not_supported" for reason in result.hard_blockers)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("next_day", "valuation_basis_requires_refresh"),
        ("unresolved_valuation", "valuation_unresolved"),
        ("missing_quote", "quote_missing_or_stale"),
        ("stale_quote", "quote_missing_or_stale"),
        ("future_quote", "quote_missing_or_stale"),
        ("holding_basis", "quantity_basis_unconfirmed"),
        ("quote_basis", "corporate_action_unresolved"),
        ("primary_evidence", "primary_evidence_requires_verification"),
        ("uncertain_case", "investment_case_uncertain"),
    ],
)
def test_sufficient_reward_claim_requires_available_current_inputs(change, reason):
    document = assessment(
        case="uncertain" if change == "uncertain_case" else "intact",
        resolved=change != "unresolved_valuation",
    )
    quoted_date = {"stale_quote": date(2026, 10, 6), "future_quote": date(2026, 10, 15)}.get(
        change, AS_OF
    )
    quoted = (
        None
        if change == "missing_quote"
        else QuoteInput(
            ticker="1234",
            price_yen=Decimal(1250),
            price_as_of=quoted_date,
            price_basis="last_close_unadjusted",
            source_ref="fixture:daily_bars",
        )
    )
    result = evaluate_holding_proposal(
        document,
        position=PositionInput(
            ticker="1234",
            sector="情報・通信業",
            common_factors=(),
            quantity=100,
            cost_yen=Decimal(100000),
            quantity_basis_confirmed=change != "holding_basis",
            original_reason_unavailable_reason="合成の当初理由は確認できない",
        ),
        as_of=date(2026, 10, 15) if change == "next_day" else AS_OF,
        quote=quoted,
        price_basis_confirmed=change != "quote_basis",
        remaining_reward=RemainingReward(status="sufficient", reason="将来分配と残存価値を確認"),
        case_reassessment_reason="本日の仮説と反証を再確認",
        reviewed=True,
        latest=True,
        primary_verified=change != "primary_evidence",
        max_quote_age_days=7,
    )
    assert result.action == "defer"
    assert result.reason_code == reason
    assert result.current_price_projection is None
