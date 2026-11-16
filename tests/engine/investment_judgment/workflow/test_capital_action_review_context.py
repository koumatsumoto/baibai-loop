"""資本異動の株数確認を固定Researchへ渡し、旧政策は補完しない。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_capital_action_input_review import (
    REASON,
    native_generation,
)
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
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


def seed(path, *, version=None):
    fixed = native_generation(completed_action=True)
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    assert selected.result.policy.method == "technical-financial-discovery-v15"
    if version is not None:
        row = selected.result.rows[0]
        selected = replace(
            selected,
            result=replace(
                selected.result,
                policy=TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
                rows=(
                    replace(
                        row,
                        financial_paths={
                            key: value
                            for key, value in row.financial_paths.items()
                            if key != "input_review_reasons"
                        },
                        screening_lane="no_current_hypothesis",
                    ),
                ),
                display_order=(),
                summary={
                    **selected.result.summary,
                    "display_pool_count": 0,
                    "screening_lanes": {
                        "growth_research": 0,
                        "input_review": 0,
                        "alternative_financial_research": 0,
                        "no_current_hypothesis": 1,
                    },
                },
            ),
        )
    ScreeningSelectionStore(path).publish("capital-action-selection", selected)
    now = read_publication(path=path, selection_id="capital-action-selection").published_at
    return fixed, selected, now + timedelta(seconds=1)


def arguments(path, now):
    return dict(
        request=ResearchRequest(text="資本異動後の自己株基準を確認", scope="discover_new"),
        generation_id=None,
        screening_selection_id="capital-action-selection",
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=path.parent / "app",
    )


def test_action_gap_survives_first_card_detail_prepare_and_runless_resume(tmp_path):
    path = tmp_path / "runs"
    fixed, selected, now = seed(path)
    original = path.read_bytes()
    first = load_generated_selection_context(**arguments(path, now), screening_lane="input_review")
    assert first["candidate_tickers"] == ["1001"]
    assert first["model_inputs"]["securities"] == {}
    card = first["screening"]["cards"][0]
    assert {r["reason"] for r in card["financial_paths"]["input_review_reasons"]} == {REASON}
    assert card["financial_gate"]["state"] == "input_unresolved"
    assert card["technical"]["state"] == "unavailable"
    assert card["financial_paths"]["matched_routes"] == []
    detail = load_generated_selection_context(**arguments(path, now), comparison_tickers=("1001",))
    cf = detail["model_inputs"]["securities"]["1001"]["models"]["company_financials"]
    assert cf["earnings_price"]["unavailable_reason"] == REASON
    assert cf["earnings_price"]["market_cap_yen"] is None
    assert cf["capital_action_evidence"][0]["issued_shares"] == 130
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=arguments(path, now)["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="capital-action-selection",
        selection_summary="事業仮説を財務合格にせず、自己株確認を先に問う",
        targets=(selected_target("1001"),),
    )
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=path,
        now=now,
    )
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    assert saved.inputs.screening_selection == selected
    projection = project_research_inputs(saved)
    assert projection["securities"]["1001"]["models"]["company_financials"] == cf
    digest = selection_sha256(saved)
    assert path.read_bytes() == original
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


@pytest.mark.parametrize("version", range(1, 7))
def test_old_policy_keeps_absence_despite_available_capital_model(tmp_path, version):
    path = tmp_path / "runs"
    fixed, selected, now = seed(path, version=version)
    codec = TypeAdapter(ScreeningSelection)
    canonical = encode_json(codec.dump_python(selected, mode="json"))
    original = path.read_bytes()
    first = load_generated_selection_context(**arguments(path, now), screening_lane="input_review")
    assert first["candidate_tickers"] == []
    detail = load_generated_selection_context(**arguments(path, now), comparison_tickers=("1001",))
    row = detail["screening"]["comparison_rows"][0]
    assert row["screening_lane"] == "no_current_hypothesis"
    assert "input_review_reasons" not in row["financial_paths"]
    cf = detail["model_inputs"]["securities"]["1001"]["models"]["company_financials"]
    assert cf["earnings_price"]["unavailable_reason"] == REASON
    restored = read_publication(path=path, selection_id="capital-action-selection").selection
    assert encode_json(codec.dump_python(restored, mode="json")) == canonical
    ScreeningSelectionStore(path).publish("capital-action-selection", selected)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(
            fixed,
            tickers=("1001",),
            rows={"1001": selected.result.rows[0]},
            policy=selected.result.policy,
        )
