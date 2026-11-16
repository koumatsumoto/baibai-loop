"""新Readingの欠測説明を保存し、旧固定判断には後補完しない。"""

from copy import deepcopy
from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
import yaml
from tests.helpers.indicator_store import observations, store_reader
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.models.market_context.reading.compute import compute_reading
from baibai_engine.machine_analysis.models.market_context.reading.models import snapshot_payload
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.primitives.json import canonical_json

ASOF = date(2026, 11, 16)
NOW = datetime(2026, 11, 17, tzinfo=UTC)


@pytest.mark.parametrize("legacy", [False, True])
def test_yoy_note_keeps_saved_generation_selection_and_resume_identity(tmp_path, legacy):
    definition = load_definitions().by_id()["us.cpi.headline"]
    reading = snapshot_payload(
        compute_reading(
            series=(definition,),
            reader=store_reader(
                observations(definition, [(date(2025, 9, 1), 100.0), (date(2026, 10, 1), 110.0)])
            ),
            rules=load_reading_rules(DEFAULT_RULES_PATH),
            rules_revision="test",
            asof=ASOF,
        )
    )
    assert reading["series"][0]["notes"]
    if legacy:
        reading["series"][0]["notes"] = []
    expected = deepcopy(reading)
    descriptor = {"format": "economic-input-v2", "selection_sha256": "a" * 64}
    digest = sha256(canonical_json(descriptor).encode()).hexdigest()
    generation = ModelGeneration(
        schema_version=2,
        generation_id="reading-old" if legacy else "reading-new",
        as_of=ASOF,
        evaluation_time=NOW,
        inputs={
            "economic": InputSnapshot(
                kind="native_l1",
                reference="economic-" + digest,
                sha256=digest,
                descriptor=descriptor,
            )
        },
        subject_plan={
            "1234": GenerationSubject(scope="stock", model_ids=()),
            "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        },
        models=(
            GeneratedModel(
                model_id="market_context",
                subject="market",
                calculation_revision=(
                    "shared-market-context-v17-sector-sample-sizes"
                    if legacy
                    else "shared-market-context-v18-yoy-availability-note"
                ),
                payload={"reading": reading},
            ),
        ),
    )
    runs = tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    before = runs.read_bytes()
    assert read_generation(generation.generation_id, path=runs) == generation
    assert not publish_generation(generation, path=runs)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="固定前年比の不足を読む", scope="specified_stocks", requested_tickers=("1234",)
        ),
        research_as_of=ASOF,
        input_known_at=NOW,
        generation_id=generation.generation_id,
        selection_summary="指数と前年比の不足を分ける",
        targets=(selected_target(),),
    )
    app = tmp_path / "app.sqlite"
    workspace = tmp_path / "workspace"
    prepared = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=workspace, runs_path=runs, now=NOW
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    projection = project_research_inputs(saved)
    assert (
        projection["shared_subjects"]["market"]["models"]["market_context"]["reading"] == expected
    )
    raw = saved.model_dump(mode="json")
    assert parse_selection(raw).model_dump(mode="json") == raw
    assert selection_sha256(parse_selection(raw)) == prepared.selection_sha256
    assert runs.read_bytes() == before
    runs.unlink()
    app_before = app.read_bytes()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=app, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == projection
    assert app.read_bytes() == app_before
