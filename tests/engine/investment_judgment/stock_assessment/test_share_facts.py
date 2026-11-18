"""New share-count inputs cannot reverse losses; saved pairs stay exact."""

import sqlite3
from contextlib import closing
from copy import deepcopy

import pytest
from tests.engine.investment_judgment.stock_assessment.test_stock_assessment import (
    evaluation,
    ratio_pair,
)
from tests.helpers.assessment_v1 import NOW, assessment_id, undetermined_case, update_review_hash

from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.calculation import (
    CalculationInput,
    calculate,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    ResearchValidationError,
    load_reviewed_assessment,
    publish_reviewed_assessment,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.schema import SCHEMA_SQL


def _calculation(document):
    payload = {key: deepcopy(document[key]) for key in ("input_snapshot", "derived", "valuation")}
    for metric in payload["derived"]["metrics"]:
        metric.pop("value")
    return CalculationInput.model_validate(payload)


@pytest.mark.parametrize(
    ("value", "unit"),
    [(-1_000_000, "shares"), (1_000_000, "JPY"), (True, "shares"), ("未確認", "shares")],
)
def test_new_assessment_and_calculation_reject_invalid_explicit_share_counts(value, unit):
    document, review = ratio_pair()
    document["input_snapshot"]["facts"][-1].update(value=value, unit=unit)
    if value == -1_000_000:
        document["input_snapshot"]["facts"][-2]["value"] = -120_000_000
    message = (
        "metric eps input facts must be numeric"
        if isinstance(value, bool | str)
        else "metric eps shares_outstanding input shares must be non-negative and measured in shares"
    )
    assert message in evaluation(document, review).errors
    with pytest.raises(ValueError, match=message):
        calculate(_calculation(document))


@pytest.mark.parametrize("kind", ["shares_outstanding", "other"])
def test_negative_profit_and_signed_general_ratios_are_not_a_buy_gate(kind):
    document, review = ratio_pair()
    document["input_snapshot"]["facts"][-2]["value"] = -120_000_000
    shares = document["input_snapshot"]["facts"][-1]
    shares["fact_kind"] = kind
    if kind == "other":
        shares["value"] = -1_000_000
    result = calculate(_calculation(document))
    expected = -120 if kind == "shares_outstanding" else 120
    assert result["derived"]["metrics"][0]["value"] == expected
    document["derived"] = result["derived"]
    assert evaluation(document, review).errors == ()


def test_zero_count_can_be_a_fact_but_cannot_be_a_ratio_denominator():
    document, review = ratio_pair()
    document["input_snapshot"]["facts"][-1]["value"] = 0
    message = "metric eps ratio denominator cannot be zero"
    assert evaluation(document, review).errors == (message,)
    with pytest.raises(ValueError, match=message):
        calculate(_calculation(document))

    document["derived"]["metrics"] = []
    assert calculate(_calculation(document))["derived"]["metrics"] == []
    assert evaluation(document, review).errors == ()


@pytest.mark.parametrize(("value", "unit"), [("未確認", "shares"), (1000, "thousand_shares")])
def test_unresolved_assessment_keeps_unused_unknown_or_original_unit(value, unit):
    document, review = ratio_pair()
    document["input_snapshot"]["facts"][-1].update(value=value, unit=unit)
    document["derived"]["metrics"] = []
    document["price_value_case"] = undetermined_case()
    document["valuation"].update(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="株数基準をまだ原典照合していないため、価値を未算定とする",
    )
    document["conclusion"]["stance"] = "uncertain"
    review["recalculated_projections"] = []
    assert calculate(_calculation(document))["derived"]["metrics"] == []
    assert evaluation(document, review).errors == ()
    assert StockAssessmentDocument.model_validate(document).input_snapshot.facts[-1].value == value


def test_negative_count_new_publication_is_rejected_but_old_exact_read_and_retry_survive():
    document, review = ratio_pair()
    document["input_snapshot"]["facts"][-2]["value"] = -120_000_000
    document["input_snapshot"]["facts"][-1]["value"] = -1_000_000
    # This positive ratio was accepted before the new share-count boundary.
    document["derived"]["metrics"][0]["value"] = 120
    update_review_hash(document, review)
    typed = StockAssessmentDocument.model_validate(document)
    core = assessment_core_hash(typed)
    payload = canonical_json(typed.model_dump(mode="json"))
    typed_review = AssessmentReview.model_validate(review)
    review_json = canonical_json(typed_review.model_dump(mode="json"))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN")
        with pytest.raises(ResearchValidationError, match="shares_outstanding input shares"):
            publish_reviewed_assessment(
                connection, assessment_id(), document, review, published_at=NOW
            )
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment_review").fetchone() == (0,)
        # Seed only this historical fixture; do not publish it under the new rule.
        connection.execute(
            "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
            (
                assessment_id(),
                "1234",
                typed.input_snapshot.as_of.isoformat(),
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
        before = connection.total_changes
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
        assert connection.total_changes == before
        assert connection.execute(
            "SELECT payload, core_sha256 FROM stock_assessment"
        ).fetchone() == (payload, core)
        assert connection.execute("SELECT payload FROM stock_assessment_review").fetchone() == (
            review_json,
        )
