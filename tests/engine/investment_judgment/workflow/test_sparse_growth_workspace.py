"""有限な調査では、固定資料の一欄がないこともそのまま読める。"""

from copy import deepcopy

import pytest
import yaml
from tests.engine.investment_judgment.workflow.test_growth_projection_copy import growth_payload
from tests.helpers.model_generation import generation, publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    _generated_selection,
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


@pytest.mark.parametrize("sparse", ["1234", "2345", "3456"], ids=["target", "contrast", "outside"])
@pytest.mark.parametrize("entry", ["prepare", "start"])
def test_sparse_report_does_not_strand_a_started_case(tmp_path, sparse, entry):
    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "research"
    tickers = ("1234", "2345", "3456")
    meta = GenerationMetadata(
        **generation().model_dump(exclude={"models", "schema_version", "subject_plan"}),
        schema_version=2,
        subject_plan={
            t: GenerationSubject(scope="stock", model_ids=("company_growth",)) for t in tickers
        },
    )
    template = growth_payload("current")
    models, rows = [], {}
    for t in tickers:
        raw = {**deepcopy(template), "ticker": t}
        if t == sparse:
            reported = next(
                o for o in raw["periods"][-1]["observations"] if o["measurement"] == "reported"
            )
            del reported["metrics"]["reported_basic_eps"]
        model = GeneratedModel(
            model_id="company_growth", subject=t, calculation_revision="fixture", payload=raw
        )
        models.append(model)
        rows[t] = project_discovery_row(t, metadata=meta, models=(model,))
    publish_generation(ModelGeneration(**meta.model_dump(), models=tuple(models)), path=runs)
    identity = discovery_publication_id(meta.generation_id)
    ScreeningSelectionStore(runs).publish(
        identity, evaluate_discovery_rows(meta, tickers=tickers, rows=rows)
    )
    now = read_discovery_index(path=runs, selection_id=identity).published_at
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(text="欠測を含む合成比較", scope="discover_new"),
        research_as_of=meta.as_of,
        input_known_at=now,
        generation_id=meta.generation_id,
        screening_selection_id=identity,
        selection_summary="未収録を実績ゼロとみなさない",
        targets=(selected_target("1234"),),
        comparison_tickers=("2345",),
    )
    case = _generated_selection(draft, runs_path=runs, app_path=app)
    digest = selection_sha256(case)
    service = OperationService(app, runs_path=runs)
    if entry == "prepare":
        started = prepare_selected_workspace(
            draft=draft, db_path=app, workspace=workspace, runs_path=runs, now=now
        )
        assert started.selection_sha256 == digest
    else:
        started = service.start(
            session_kind="capital-allocation",
            as_of=meta.as_of,
            started_at=now,
            payload=OperationPayload(checkpoint="開始", artifacts=(case.model_dump(mode="json"),)),
        )
    before = app.read_bytes()
    runs.rename(tmp_path / "detached")  # Nothing may retrieve a substitute for the absent column.
    result = resume_selected_workspace(
        operation_id=started.operation_id, db_path=app, workspace=workspace, now=now
    )
    assert result.status == "resumed"
    assert result.selection_sha256 == digest
    saved = selection_binding(service.get(started.operation_id).payload)
    assert saved == case
    view = yaml.safe_load((workspace / "domain-inputs.yaml").read_text())["securities"]
    assert set(view) == {"1234", "2345"}
    for t in view:
        growth = view[t]["models"]["company_growth"]
        index = int(growth["full_result_ref"].split("/")[-2])
        original = saved.inputs.generation.models[index]
        assert original.payload == next(m.payload for m in models if m.subject == t)
        row = growth["reported"]["rows"][-1]
        assert ("reported_basic_eps" in row.get("unrecorded_metrics", ())) == (t == sparse)
    assert app.read_bytes() == before
