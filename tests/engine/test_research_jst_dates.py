"""Research dates use the Tokyo calendar date, regardless of timestamp offset."""

from copy import deepcopy
from datetime import datetime

import pytest
from tests.helpers.assessment_v1 import pair_payload

from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.assessment import (
    AssessmentReview,
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.research.holding_policy import evaluate_holding_proposal
from baibai_engine.research.proposal import PositionInput, QuoteInput, RemainingReward

JST_MORNING = "2026-10-14T08:30:00+09:00"
UTC_MORNING = "2026-10-13T23:30:00Z"


def _assessment_pair():
    assessment, review = pair_payload(as_of="2026-10-14", quote_as_of="2026-10-14")
    for source in assessment["input_snapshot"]["sources"]:
        source.update(as_of="2026-10-14", retrieved_at=JST_MORNING)
    assessment["input_snapshot"]["facts"][0]["observed_at"] = JST_MORNING
    assessment["conclusion"]["assessed_at"] = "2026-10-14T09:00:00+09:00"
    review["reviewed_at"] = "2026-10-14T09:15:00+09:00"
    return assessment, review


def _evaluate(assessment, review, *, now="2026-10-14T10:00:00+09:00"):
    document = StockAssessmentDocument.model_validate(assessment)
    review = deepcopy(review)
    review["reviewed_assessment_sha256"] = assessment_core_hash(document)
    return evaluate_assessment(
        document,
        identity=UnpublishedAssessment.DRAFT,
        review=AssessmentReview.model_validate(review),
        now=datetime.fromisoformat(now),
    )


def test_assessment_business_day_is_invariant_to_timestamp_offset():
    assert datetime.fromisoformat(JST_MORNING) == datetime.fromisoformat(UTC_MORNING)
    assessment, review = _assessment_pair()
    for field in ("source", "fact"):
        for stamp in (JST_MORNING, UTC_MORNING):
            candidate = deepcopy(assessment)
            if field == "source":
                candidate["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
            else:
                candidate["input_snapshot"]["facts"][0]["observed_at"] = stamp
            assert _evaluate(candidate, review).errors == ()

    for source in assessment["input_snapshot"]["sources"]:
        source["retrieved_at"] = "2026-10-14T08:00:00+09:00"
    assessment["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-14T08:00:00+09:00"
    for stamp in (JST_MORNING, UTC_MORNING):
        proposal = deepcopy(assessment)
        proposal["conclusion"]["assessed_at"] = stamp
        assert _evaluate(proposal, review).errors == ()

    assessment["conclusion"]["assessed_at"] = "2026-10-14T08:05:00+09:00"
    review["reviewed_at"] = "2026-10-14T08:10:00+09:00"
    for stamp in (JST_MORNING, UTC_MORNING):
        assert _evaluate(assessment, review, now=stamp).errors == ()


def test_assessment_same_calendar_day_outside_utc_midnight_window():
    assessment, review = _assessment_pair()
    assessment["conclusion"]["assessed_at"] = "2026-10-14T22:00:00+09:00"
    review["reviewed_at"] = "2026-10-14T22:15:00+09:00"
    for stamp in ("2026-10-14T21:00:00+09:00", "2026-10-14T12:00:00Z"):
        candidate = deepcopy(assessment)
        candidate["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
        candidate["input_snapshot"]["facts"][0]["observed_at"] = stamp
        assert _evaluate(candidate, review, now="2026-10-14T23:00:00+09:00").errors == ()


@pytest.mark.parametrize(
    ("field", "stamp", "error"),
    [
        ("source", "2026-10-13T23:30:00+09:00", "source annual has invalid time"),
        ("fact", "2026-10-13T23:30:00+09:00", "market_price observation time mismatch"),
        ("source", "2026-10-14T09:30:00+09:00", "source annual has invalid time"),
        ("fact", "2026-10-14T09:30:00+09:00", "market_price observation time mismatch"),
    ],
)
def test_assessment_still_rejects_wrong_business_day_or_future_input(field, stamp, error):
    assessment, review = _assessment_pair()
    if field == "source":
        assessment["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
    else:
        assessment["input_snapshot"]["facts"][0]["observed_at"] = stamp
    assert error in _evaluate(assessment, review).errors


def test_assessment_still_rejects_future_proposal_and_previous_business_day_now():
    assessment, review = _assessment_pair()
    assert (
        "invalid assessment/as_of time"
        in _evaluate(assessment, review, now="2026-10-13T23:30:00+09:00").errors
    )
    assessment["conclusion"]["assessed_at"] = "2026-10-14T10:30:00+09:00"
    assert "invalid assessment/as_of time" in _evaluate(assessment, review).errors


def test_assessment_rejects_utc_date_that_is_next_day_in_tokyo():
    assessment, review = _assessment_pair()
    assessment["conclusion"]["assessed_at"] = "2026-10-15T09:00:00+09:00"
    review["reviewed_at"] = "2026-10-15T09:15:00+09:00"
    assessment["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-14T15:30:00Z"
    assert (
        "market_price observation time mismatch"
        in _evaluate(assessment, review, now="2026-10-15T10:00:00+09:00").errors
    )


@pytest.mark.parametrize(
    ("price_date", "expected_action", "expected_reason"),
    [
        ("2026-10-14", "hold", "sufficient_remaining_reward"),
        ("2026-10-13", "defer", "quote_missing_or_stale"),
        ("2026-10-22", "defer", "quote_missing_or_stale"),
    ],
)
def test_position_quote_age_uses_native_date_without_receipt_clock(
    price_date, expected_action, expected_reason
):
    assessment_payload, _ = pair_payload(as_of="2026-10-21", quote_as_of="2026-10-14")
    assessment = StockAssessmentDocument.model_validate(assessment_payload)
    quote = QuoteInput(
        ticker="1234",
        price_yen=1000,
        price_as_of=price_date,
        price_basis="last_close_unadjusted",
        source_ref="fixture",
        basis_confirmed=True,
    )
    result = evaluate_holding_proposal(
        assessment,
        position=PositionInput(
            ticker="1234",
            sector="情報・通信業",
            common_factors=(),
            quantity=100,
            cost_yen=1000,
            quantity_basis_confirmed=True,
            original_investment_reason="合成の投資理由",
        ),
        as_of=datetime.fromisoformat("2026-10-21T17:00:00+09:00").date(),
        quote=quote,
        remaining_reward=RemainingReward(status="sufficient", reason="残存見返りを確認"),
        case_reassessment_reason="現在の仮説維持を確認",
        primary_verified=True,
        reviewed=True,
        latest=True,
        max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
    )
    assert (result.action, result.reason_code) == (expected_action, expected_reason)
    assert (result.current_price_projection is not None) is (expected_action == "hold")
    if result.current_price_projection is not None:
        assert result.current_price_projection["price_as_of"] == "2026-10-14"
