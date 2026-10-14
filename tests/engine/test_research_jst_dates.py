"""Research dates use the Tokyo business day, regardless of timestamp offset."""

from copy import deepcopy
from datetime import datetime

import pytest
from tests.helpers.research_v4 import pair_payload

from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.position_review import PositionReviewDocument, evaluate_position_review
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    thesis_core_hash,
)

JST_MORNING = "2026-10-14T08:30:00+09:00"
UTC_MORNING = "2026-10-13T23:30:00Z"


def _thesis_pair():
    thesis, review = pair_payload(as_of="2026-10-14", quote_as_of="2026-10-14")
    thesis["input_snapshot"]["sources"][0].update(as_of="2026-10-14", retrieved_at=JST_MORNING)
    thesis["input_snapshot"]["facts"][0]["observed_at"] = JST_MORNING
    thesis["judgment"]["proposed_at"] = "2026-10-14T09:00:00+09:00"
    review["reviewed_at"] = "2026-10-14T09:15:00+09:00"
    return thesis, review


def _evaluate(thesis, review, *, now="2026-10-14T10:00:00+09:00"):
    document = ThesisDocument.model_validate(thesis)
    review = deepcopy(review)
    review["reviewed_thesis_sha256"] = thesis_core_hash(document)
    return evaluate_thesis(
        document,
        identity=UnpublishedThesis.DRAFT,
        review=ThesisReview.model_validate(review),
        now=datetime.fromisoformat(now),
    )


def test_thesis_business_day_is_invariant_to_timestamp_offset():
    assert datetime.fromisoformat(JST_MORNING) == datetime.fromisoformat(UTC_MORNING)
    thesis, review = _thesis_pair()
    for field in ("source", "fact"):
        for stamp in (JST_MORNING, UTC_MORNING):
            candidate = deepcopy(thesis)
            if field == "source":
                candidate["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
            else:
                candidate["input_snapshot"]["facts"][0]["observed_at"] = stamp
            assert _evaluate(candidate, review).errors == ()

    thesis["input_snapshot"]["sources"][0]["retrieved_at"] = "2026-10-14T08:00:00+09:00"
    thesis["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-14T08:00:00+09:00"
    for stamp in (JST_MORNING, UTC_MORNING):
        proposal = deepcopy(thesis)
        proposal["judgment"]["proposed_at"] = stamp
        assert _evaluate(proposal, review).errors == ()

    thesis["judgment"]["proposed_at"] = "2026-10-14T08:05:00+09:00"
    review["reviewed_at"] = "2026-10-14T08:10:00+09:00"
    for stamp in (JST_MORNING, UTC_MORNING):
        assert _evaluate(thesis, review, now=stamp).errors == ()


def test_thesis_same_calendar_day_outside_utc_midnight_window():
    thesis, review = _thesis_pair()
    thesis["judgment"]["proposed_at"] = "2026-10-14T22:00:00+09:00"
    review["reviewed_at"] = "2026-10-14T22:15:00+09:00"
    for stamp in ("2026-10-14T21:00:00+09:00", "2026-10-14T12:00:00Z"):
        candidate = deepcopy(thesis)
        candidate["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
        candidate["input_snapshot"]["facts"][0]["observed_at"] = stamp
        assert _evaluate(candidate, review, now="2026-10-14T23:00:00+09:00").errors == ()


@pytest.mark.parametrize(
    ("field", "stamp", "error"),
    [
        ("source", "2026-10-13T23:30:00+09:00", "source ir has invalid time"),
        ("fact", "2026-10-13T23:30:00+09:00", "market_price observation time mismatch"),
        ("source", "2026-10-14T09:30:00+09:00", "source ir has invalid time"),
        ("fact", "2026-10-14T09:30:00+09:00", "market_price observation time mismatch"),
    ],
)
def test_thesis_still_rejects_wrong_business_day_or_future_input(field, stamp, error):
    thesis, review = _thesis_pair()
    if field == "source":
        thesis["input_snapshot"]["sources"][0]["retrieved_at"] = stamp
    else:
        thesis["input_snapshot"]["facts"][0]["observed_at"] = stamp
    assert error in _evaluate(thesis, review).errors


def test_thesis_still_rejects_future_proposal_and_previous_business_day_now():
    thesis, review = _thesis_pair()
    assert (
        "invalid proposal/as_of time"
        in _evaluate(thesis, review, now="2026-10-13T23:30:00+09:00").errors
    )
    thesis["judgment"]["proposed_at"] = "2026-10-14T10:30:00+09:00"
    assert "invalid proposal/as_of time" in _evaluate(thesis, review).errors


def test_thesis_rejects_utc_date_that_is_next_day_in_tokyo():
    thesis, review = _thesis_pair()
    thesis["judgment"]["proposed_at"] = "2026-10-15T09:00:00+09:00"
    review["reviewed_at"] = "2026-10-15T09:15:00+09:00"
    thesis["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-14T15:30:00Z"
    assert (
        "market_price observation time mismatch"
        in _evaluate(thesis, review, now="2026-10-15T10:00:00+09:00").errors
    )


@pytest.mark.parametrize(
    ("jst_stamp", "utc_stamp", "expected_action", "expected_reason"),
    [
        (JST_MORNING, UTC_MORNING, "hold", "sufficient_remaining_reward"),
        (
            "2026-10-14T21:00:00+09:00",
            "2026-10-14T12:00:00Z",
            "hold",
            "sufficient_remaining_reward",
        ),
        (
            "2026-10-13T08:30:00+09:00",
            "2026-10-12T23:30:00Z",
            None,
            "quote_stale",
        ),
        (
            "2026-10-22T00:30:00+09:00",
            "2026-10-21T15:30:00Z",
            None,
            "quote_after_judgment",
        ),
    ],
)
def test_position_quote_age_uses_jst_day_with_unchanged_policy(
    jst_stamp, utc_stamp, expected_action, expected_reason
):
    thesis_payload, _ = pair_payload(as_of="2026-10-21", quote_as_of="2026-10-14")
    thesis = ThesisDocument.model_validate(thesis_payload)
    for stamp in (jst_stamp, utc_stamp):
        document = PositionReviewDocument.model_validate(
            {
                "schema_version": 3,
                "position_review_id": "position-review-20261021-1234-holding",
                "thesis_id": "current",
                "position_id": "holding",
                "ticker": "1234",
                "as_of": "2026-10-21",
                "holding": {"quantity": 100, "cost_yen": 1000, "quantity_basis_confirmed": True},
                "quote": {
                    "price_yen": 1000,
                    "observed_at": stamp,
                    "price_basis": "realtime",
                    "source_ref": "fixture",
                    "basis_confirmed": True,
                },
                "remaining_reward": {"status": "sufficient", "reason": "残存見返りを確認"},
            }
        )
        result = evaluate_position_review(
            document,
            thesis,
            primary_verified=True,
            max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        )
        assert (result.action, result.reason) == (expected_action, expected_reason)
        assert (result.current_price_projection is not None) is (expected_action is not None)
        if result.current_price_projection is not None:
            assert result.current_price_projection["price_as_of"] == "2026-10-14"
