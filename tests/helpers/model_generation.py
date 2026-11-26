"""モデル保存と現行候補政策の接続を調べる有限の合成入力。旧政策の再開fixtureとは分ける。"""

from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.policies.discovery import evaluate_discovery_rows
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


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


def publish_machine_generation(
    path: Path,
    *,
    release_id: str,
    release_sha256: str,
    as_of: date = date(2026, 10, 23),
    select: bool = True,
) -> None:
    result = generation().model_copy(
        update={
            "schema_version": 2,
            "generation_id": f"models-{release_id}",
            "as_of": as_of,
            "inputs": {
                "market": InputSnapshot(
                    kind="lake_release", reference=release_id, sha256=release_sha256
                )
            },
            "subject_plan": {
                "1234": GenerationSubject(
                    scope="stock", model_ids=("technical_analysis", "company_financials")
                ),
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            },
        }
    )
    publish_generation(result, path=path)
    if select:
        metadata = GenerationMetadata.model_validate(result.model_dump(exclude={"models"}))
        ScreeningSelectionStore(path).publish(
            discovery_publication_id(metadata.generation_id),
            evaluate_discovery_rows(metadata, tickers=("1234",), rows={}),
        )
