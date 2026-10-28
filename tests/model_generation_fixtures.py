from datetime import UTC, date, datetime

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)


def generation(*, value=0.100049):
    return ModelGeneration(
        generation_id="synthetic-generation",
        as_of=date(2026, 10, 23),
        evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="fixed-input", sha256="a" * 64)
        },
        models=(
            GeneratedModel(
                model_id="technical_analysis",
                subject="1234",
                calculation_revision="v1",
                payload={"price_momentum": {"value": None, "reason": "missing_endpoint"}},
            ),
            GeneratedModel(
                model_id="company_valuation",
                subject="1234",
                calculation_revision="v1",
                payload={"earnings_price": {"earnings_yield": value}},
            ),
            GeneratedModel(
                model_id="market_context",
                subject="market",
                calculation_revision="v1",
                payload={"series": [{"value": None, "unit": "percent"}]},
            ),
        ),
    )
