"""モデル保存と現行候補政策の接続を調べる有限の合成入力。旧政策の再開fixtureとは分ける。"""

from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
    ModelGeneration,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)


def materialize_generation(stream: ModelGenerationStream) -> ModelGeneration:
    """有限testでのみ列を一回消費する。保存・政策・入力再取得は行わない。"""
    return ModelGeneration(**stream.metadata.model_dump(), models=tuple(stream.models))


def publish_generation(generation: ModelGeneration, *, path: Path | None = None) -> bool:
    """有限fixtureの全結果を通常writerへ渡す。runner用の対象列は保存に使わない。"""
    if not isinstance(generation, ModelGeneration):
        raise TypeError("publication requires a complete model generation")
    metadata = GenerationMetadata.model_validate(generation.model_dump(exclude={"models"}))
    stream = ModelGenerationStream(metadata, (), iter(generation.models))
    return ModelGenerationStore(path).publish_stream(stream).created


def empty_selection(generation_id, *, tickers=("1234",), candidate_limit=20):
    return ScreeningSelection(
        generation_id,
        tickers,
        EconomicPathSelection(EconomicPathPolicy(candidate_limit=candidate_limit), 0, ()),
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
                model_id="company_financials",
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
