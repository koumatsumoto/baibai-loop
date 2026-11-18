"""共有材料との関連理由を初回から固定再開へ渡し、旧行へ後補完しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_financial_input_review_context import seed
from tests.engine.machine_analysis.screening.test_related_subject_context import BASIS, SUBJECT
from tests.engine.machine_analysis.screening.test_technical_financial import evaluate_fixed
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationSubject
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


@pytest.mark.parametrize("legacy", [False, True])
def test_first_comparison_detail_and_runless_resume_keep_saved_relation(tmp_path, legacy):
    path = tmp_path / "runs"
    original, _, _, _ = seed(path)
    plan = deepcopy(original.subject_plan)
    context = {"related_subjects": [SUBJECT], "related_subject_basis": {SUBJECT: BASIS}}
    plan["1234"] = plan["1234"].model_copy(
        update={"related_subjects": (SUBJECT,), "related_subject_basis": {SUBJECT: BASIS}}
    )
    plan[SUBJECT] = GenerationSubject(scope="sector", model_ids=("industry_trends",))
    shared = GeneratedModel(
        model_id="industry_trends",
        subject=SUBJECT,
        calculation_revision="synthetic-v1",
        payload={"coverage": "partial", "observations": []},
    )
    fixed = original.model_copy(
        update={
            "generation_id": "related-context-fixed",
            "subject_plan": plan,
            "models": (*original.models, shared),
        }
    )
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    assert selected.result.policy.method == "technical-financial-discovery-v26"
    if legacy:
        selected = replace(
            selected,
            result=replace(
                selected.result,
                policy=TechnicalFinancialPolicy(method="technical-financial-discovery-v24"),
                rows=tuple(
                    replace(row, related_subject_context=None) for row in selected.result.rows
                ),
            ),
        )
    codec = TypeAdapter(ScreeningSelection)
    canonical = encode_json(codec.dump_python(selected, mode="json"))
    ScreeningSelectionStore(path).publish("related-context-policy", selected)
    publication = read_publication(path=path, selection_id="related-context-policy")
    now = publication.published_at + timedelta(seconds=1)
    args = dict(
        request=ResearchRequest(text="事業との関連と調査母集団を分ける", scope="discover_new"),
        generation_id=None,
        screening_selection_id="related-context-policy",
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=tmp_path / "app",
    )
    before = path.read_bytes()
    first = load_generated_selection_context(**args, screening_lane="input_review")
    assert first["model_inputs"]["securities"] == {}
    card = next(c for c in first["screening"]["cards"] if c["ticker"] == "1234")
    assert ("related_subject_context" in card) is not legacy
    if not legacy:
        assert card["related_subject_context"] == context
    detail = load_generated_selection_context(**args, comparison_tickers=("1234",))
    row = detail["screening"]["comparison_rows"][0]
    assert ("related_subject_context" in row) is not legacy
    source_plan = detail["model_inputs"]["analysis_source"]["subject_plan"]
    assert source_plan["1234"]["related_subject_basis"] == {SUBJECT: BASIS}
    assert detail["model_inputs"]["shared_subjects"][SUBJECT]["models"] == {
        "industry_trends": shared.payload
    }
    restored = read_publication(path=path, selection_id="related-context-policy").selection
    assert encode_json(codec.dump_python(restored, mode="json")) == canonical
    assert path.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=args["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="related-context-policy",
        selection_summary="関連情報を曝露量・調査所属・利益改善の認定にしない",
        targets=(selected_target("1234"),),
    )
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=path,
        now=now,
    )
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    projection = project_research_inputs(saved)
    assert projection["shared_subjects"][SUBJECT]["models"]["industry_trends"] == shared.payload
    digest = selection_sha256(saved)
    path.rename(tmp_path / "unavailable-runs")
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=now + timedelta(days=1),
    )
    assert resumed.operation_id == prepared.operation_id
    reread = selection_binding(
        OperationService(tmp_path / "app").get(prepared.operation_id).payload
    )
    assert selection_sha256(reread) == digest
    assert project_research_inputs(reread) == projection
    assert not path.exists()
