"""New ratio dates cannot precede operands; saved pairs remain immutable."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime

import pytest
from tests.engine.investment_judgment.stock_assessment.test_research_calculation import (
    inputs_payload,
)
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash

from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.calculation import (
    CalculationInput,
    calculate,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    ResearchConflictError,
    ResearchValidationError,
    load_reviewed_assessment,
    publish_reviewed_assessment,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.schema import SCHEMA_SQL

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


def ratio_pair():
    inputs = inputs_payload(numerator=120_000_000, denominator=1_000_000, denominator_unit="shares")
    result = calculate(CalculationInput.model_validate(inputs))
    document, review = pair_payload(as_of="2026-09-07", quote_as_of="2026-09-04")
    document.update(input_snapshot=inputs["input_snapshot"], derived=result["derived"])
    update_review_hash(document, review)
    return document, review


@pytest.mark.parametrize("fact_id", ["numerator", "denominator"])
def test_new_ratio_rejects_a_date_before_either_operand(fact_id):
    inputs = inputs_payload(numerator=120_000_000, denominator=1_000_000, denominator_unit="shares")
    next(fact for fact in inputs["input_snapshot"]["facts"] if fact["fact_id"] == fact_id)[
        "as_of"
    ] = "2026-09-05"
    error = f"metric ratio predates input fact {fact_id}"
    with pytest.raises(ValueError, match=error):
        calculate(CalculationInput.model_validate(inputs))

    document, review = ratio_pair()
    document["input_snapshot"] = inputs["input_snapshot"]
    update_review_hash(document, review)
    evaluation = evaluate_assessment(
        StockAssessmentDocument.model_validate(document),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )
    assert evaluation.errors == (error,)


@pytest.mark.parametrize("metric_date", ["2026-09-04", "2026-09-07"])
def test_different_operand_dates_need_not_match_the_ratio_date(metric_date):
    inputs = inputs_payload()
    inputs["input_snapshot"]["facts"][-2]["as_of"] = "2026-09-03"
    inputs["derived"]["metrics"][0]["as_of"] = metric_date
    result = calculate(CalculationInput.model_validate(inputs))
    assert result["derived"]["metrics"][0]["as_of"] == metric_date
    assert result["derived"]["metrics"][0]["value"] == 1 / 3
    document, review = ratio_pair()
    document.update(input_snapshot=inputs["input_snapshot"], derived=result["derived"])
    update_review_hash(document, review)
    assert (
        evaluate_assessment(
            StockAssessmentDocument.model_validate(document),
            review=AssessmentReview.model_validate(review),
            identity=UnpublishedAssessment.DRAFT,
            now=NOW,
        ).errors
        == ()
    )


def test_old_pair_hash_readback_and_retry_survive_new_date_check():
    document, review = ratio_pair()
    document["derived"]["metrics"][0]["as_of"] = "2025-09-04"
    update_review_hash(document, review)
    typed = StockAssessmentDocument.model_validate(document)
    core = assessment_core_hash(typed)
    # Computed with main 18135ae before the new-authoring date check.
    assert core == "f33f50fbaeba1e64cafb60ada2dd87140300f6b9c65e0144dfd22cddaeed717e"
    payload = canonical_json(typed.model_dump(mode="json"))
    typed_review = AssessmentReview.model_validate(review)
    review_json = canonical_json(typed_review.model_dump(mode="json"))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN")
        with pytest.raises(ResearchValidationError, match="predates input fact"):
            publish_reviewed_assessment(
                connection, assessment_id(), document, review, published_at=NOW
            )
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone() == (0,)
        # Seed a pair accepted before this new-authoring check, not a new publication.
        connection.execute(
            "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
            (
                assessment_id(),
                "1234",
                "2026-09-07",
                "supported",
                NOW.isoformat(),
                None,
                payload,
                core,
            ),
        )
        connection.execute(
            "INSERT INTO stock_assessment_review VALUES(?,?,?,?)",
            (
                typed_review.review_id,
                assessment_id(),
                typed_review.reviewed_at.isoformat(),
                review_json,
            ),
        )
        changes = connection.total_changes
        saved = load_reviewed_assessment(connection, assessment_id())
        assert saved.document == typed
        assert saved.core_sha256 == core
        assert saved.evaluation.errors == ()
        assert (
            publish_reviewed_assessment(
                connection, assessment_id(), document, review, published_at=NOW
            )
            == saved
        )
        assert connection.total_changes == changes
        assert connection.execute(
            "SELECT payload,core_sha256 FROM stock_assessment"
        ).fetchone() == (payload, core)
        assert connection.execute("SELECT payload FROM stock_assessment_review").fetchone() == (
            review_json,
        )
        changed = deepcopy(document)
        changed["derived"]["metrics"][0]["as_of"] = "2026-09-04"
        with pytest.raises(ResearchConflictError, match="immutable"):
            publish_reviewed_assessment(
                connection, assessment_id(), changed, review, published_at=NOW
            )
