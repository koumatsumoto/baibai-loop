from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)


def generation(models):
    return ModelGeneration(
        generation_id="synthetic-generation",
        as_of=date(2026, 10, 23),
        evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="fixed-market", sha256="a" * 64),
            "indicators": InputSnapshot(
                kind="sqlite_snapshot", reference="fixed-indicators", sha256="b" * 64
            ),
        },
        models=models,
    )


def test_model_payloads_retain_periods_units_missingness_and_full_precision():
    model = GeneratedModel(
        model_id="company_financials",
        subject="1234",
        calculation_revision="synthetic-v1",
        payload={
            "earnings_yield": 0.100049,
            "profit": {"period_end": "2026-06-30", "unit": "JPY", "value": 1234567.0},
            "forecast": {"value": None, "reason": "not_observed"},
        },
    )
    original = generation((model,))
    restored = ModelGeneration.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.result("company_financials", "1234").payload == model.payload
    assert restored.inputs["market"].reference != restored.inputs["indicators"].reference


def test_adding_a_model_does_not_change_existing_results_or_make_every_model_required():
    valuation = GeneratedModel(
        model_id="company_financials",
        subject="1234",
        calculation_revision="v1",
        payload={"Y": 0.1},
    )
    addition = GeneratedModel(
        model_id="future_model",
        subject="1234",
        calculation_revision="v1",
        payload={"value": None},
    )
    original = generation((valuation,))
    extended = generation((valuation, addition))
    assert original.result("company_financials", "1234") == extended.result(
        "company_financials", "1234"
    )
    assert extended.result("technical_analysis", "1234") is None


def test_duplicate_model_subject_does_not_become_an_extra_vote():
    model = GeneratedModel(
        model_id="company_financials",
        subject="1234",
        calculation_revision="v1",
        payload={},
    )
    with pytest.raises(ValidationError, match="one result per model and subject"):
        generation((model, model))
