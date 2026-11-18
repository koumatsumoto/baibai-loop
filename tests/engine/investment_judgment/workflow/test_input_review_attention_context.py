"""旧methodの保存順は、新しい表示方針で書き換えずに選択・再開できる。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_technical_financial_context import seed
from tests.helpers.research_selection import selected_target

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
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_saved_policy_order_and_workspace_bytes_survive_runless_resume(
    tmp_path, monkeypatch, version
):
    runs = tmp_path / "runs"
    fixed, current, _ = seed(runs)
    # A deliberately distinct stored order catches even deterministic silent recomputation.
    previous = replace(
        current,
        result=replace(
            current.result,
            policy=TechnicalFinancialPolicy(method=f"technical-financial-discovery-{version}"),
            display_order=tuple(reversed(current.result.display_order)),
        ),
    )
    identity = f"historical-{version}"
    ScreeningSelectionStore(runs).publish(identity, previous)
    now = read_publication(path=runs, selection_id=identity).published_at + timedelta(seconds=1)
    source_bytes = runs.read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail("saved policy was recomputed")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.technical_financial.discovery_display_order",
        forbidden,
    )
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.technical_financial_selection.project_technical_financial_row",
        forbidden,
    )
    request = ResearchRequest(text="保存世代の入力確認", scope="discover_new")
    context = load_generated_selection_context(
        request=request,
        generation_id=fixed.generation_id,
        screening_selection_id=identity,
        screening_lane="input_review",
        research_as_of=None,
        known_at=now,
        runs_path=runs,
        db_path=tmp_path / "app",
    )
    assert tuple(context["candidate_tickers"]) == previous.result.lane_tickers("input_review")
    assert context["screening"]["presentation"]["ties"] == "ticker_code_order_not_quality_or_return"
    assert "financial_gate_states" not in context["screening"]["lanes"]["input_review"]
    prepared = prepare_selected_workspace(
        draft=GeneratedResearchSelectionDraft(
            schema_version=2,
            request=request,
            research_as_of=fixed.as_of,
            input_known_at=now,
            generation_id=fixed.generation_id,
            screening_selection_id=identity,
            selection_summary="保存した予備調査の理由を変えずに選ぶ",
            targets=(selected_target("3456"),),
        ),
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=runs,
        now=now,
    )
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    digest = selection_sha256(saved)
    assert runs.read_bytes() == source_bytes
    runs.rename(tmp_path / "unavailable-runs")
    files = [tmp_path / "app", *sorted((tmp_path / "workspace").iterdir())]
    before = {p: p.read_bytes() for p in files if p.is_file()}
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=now + timedelta(days=1),
    )
    assert resumed.status == "resumed"
    assert (
        selection_sha256(
            selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
        )
        == digest
    )
    assert {p: p.read_bytes() for p in before} == before
    assert not runs.exists()
