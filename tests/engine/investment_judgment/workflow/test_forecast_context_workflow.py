"""初回予想反証と同じ原モデルを固定し、元runsなしでも再開する。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_technical_financial_context import seed
from tests.engine.machine_analysis.screening.test_forecast_context import FORECAST
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
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def test_forecast_card_detail_and_detached_research_keep_same_recorded_source(tmp_path):
    path = tmp_path / "runs"
    fixed, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "forecast-context-fixed"})
    for model in fixed.models:
        if model.subject == "1234" and model.model_id == "company_financials":
            model.payload["financial"].update(FORECAST)
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    ScreeningSelectionStore(path).publish("forecast-policy", selected)
    publication = read_publication(path=path, selection_id="forecast-policy")
    now = publication.published_at + timedelta(seconds=1)
    arguments = dict(
        request=ResearchRequest(text="営業回復と会社通期赤字予想を比較", scope="discover_new"),
        generation_id=None,
        screening_selection_id="forecast-policy",
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=tmp_path / "app",
    )
    before = path.read_bytes()
    first = load_generated_selection_context(**arguments)
    assert first["model_inputs"]["securities"] == {}
    card = next(card for card in first["screening"]["cards"] if card["ticker"] == "1234")
    assert card["financial_gate"]["forecast_context"] == FORECAST
    detail = load_generated_selection_context(**arguments, comparison_tickers=("1234",))
    full = detail["model_inputs"]["securities"]["1234"]["models"]["company_financials"]
    assert {key: full["financial"][key] for key in FORECAST} == FORECAST
    assert path.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=arguments["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="forecast-policy",
        selection_summary="会社予想は実績の営業回復と別の反証として調べる",
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
    digest = selection_sha256(saved)
    projection = project_research_inputs(saved)
    saved_row = projection["screening_selection"]["result"]["comparison_rows"][0]
    assert saved_row["financial_gate"]["forecast_context"] == FORECAST
    assert saved.inputs.screening_selection == selected
    path.unlink()
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


@pytest.mark.parametrize("version", range(1, 6))
def test_legacy_saved_card_is_not_backfilled_from_available_forecast_model(tmp_path, version):
    path = tmp_path / "runs"
    fixed, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "legacy-forecast-input"})
    for model in fixed.models:
        if model.subject == "1234" and model.model_id == "company_financials":
            model.payload["financial"].update(FORECAST)
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    legacy = replace(
        selected,
        result=replace(
            selected.result,
            policy=selected.result.policy.model_copy(
                update={"method": f"technical-financial-discovery-v{version}"}
            ),
            rows=tuple(
                replace(
                    row,
                    financial_gate={
                        key: value
                        for key, value in row.financial_gate.items()
                        if key != "forecast_context"
                    },
                )
                for row in selected.result.rows
            ),
        ),
    )
    ScreeningSelectionStore(path).publish("legacy-forecast-policy", legacy)
    publication = read_publication(path=path, selection_id="legacy-forecast-policy")
    before = path.read_bytes()
    arguments = dict(
        request=ResearchRequest(text="旧保存を変更せず読む", scope="discover_new"),
        generation_id=None,
        screening_selection_id="legacy-forecast-policy",
        research_as_of=None,
        known_at=publication.published_at + timedelta(seconds=1),
        runs_path=path,
        db_path=tmp_path / "app",
    )
    first = load_generated_selection_context(**arguments)
    assert first["model_inputs"]["securities"] == {}
    assert all(
        "forecast_context" not in card["financial_gate"] for card in first["screening"]["cards"]
    )
    detail = load_generated_selection_context(**arguments, comparison_tickers=("1234",))
    full = detail["model_inputs"]["securities"]["1234"]["models"]["company_financials"]
    assert {key: full["financial"][key] for key in FORECAST} == FORECAST
    assert read_publication(path=path, selection_id="legacy-forecast-policy") == publication
    assert path.read_bytes() == before
