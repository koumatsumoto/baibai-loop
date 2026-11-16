"""会計変更noticeは固定初回比較から再開へ保持し、旧保存へ後補完しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_technical_financial_context import args, seed
from tests.engine.machine_analysis.screening.test_accounting_notice_context import NOTICE
from tests.engine.machine_analysis.screening.test_economic_path_inputs import payloads
from tests.engine.machine_analysis.screening.test_technical_financial import evaluate_fixed
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
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
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


@pytest.mark.parametrize("legacy", [False, True])
def test_recorded_notice_to_fixed_research_and_runless_resume_without_old_backfill(
    tmp_path, legacy
):
    path = tmp_path / "runs"
    fixed, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "accounting-notice-fixed"})
    earnings = deepcopy(payloads()["company_financials"]["earnings_price"])
    earnings["accounting_disclosures"] = [deepcopy(NOTICE)]
    for model in fixed.models:
        if model.subject == "1234" and model.model_id == "company_financials":
            model.payload["earnings_price"] = earnings
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    expected = next(
        row.financial_paths["accounting_change_context"]
        for row in selected.result.rows
        if row.ticker == "1234"
    )
    if legacy:
        selected = replace(
            selected,
            result=replace(
                selected.result,
                policy=TechnicalFinancialPolicy(method="technical-financial-discovery-v10"),
                rows=tuple(
                    replace(
                        row,
                        financial_paths={
                            k: v
                            for k, v in row.financial_paths.items()
                            if k != "accounting_change_context"
                        },
                    )
                    for row in selected.result.rows
                ),
            ),
        )
    codec = TypeAdapter(ScreeningSelection)
    canonical = encode_json(codec.dump_python(selected, mode="json"))
    ScreeningSelectionStore(path).publish("accounting-notice-policy", selected)
    publication = read_publication(path=path, selection_id="accounting-notice-policy")
    now = publication.published_at + timedelta(seconds=1)
    arguments = {**args(path, now), "screening_selection_id": "accounting-notice-policy"}
    before = path.read_bytes()
    target_page = (
        selected.result.lane_tickers("growth_research").index("1234")
        // selected.result.policy.page_size
    )
    first = load_generated_selection_context(**arguments, screening_page=target_page)
    assert first["model_inputs"]["securities"] == {}
    card = next(card for card in first["screening"]["cards"] if card["ticker"] == "1234")
    assert ("accounting_change_context" in card["financial_paths"]) is not legacy
    if not legacy:
        assert card["financial_paths"]["accounting_change_context"] == expected
    detail = load_generated_selection_context(**arguments, comparison_tickers=("1234",))
    full = detail["model_inputs"]["securities"]["1234"]["models"]["company_financials"]
    assert full["earnings_price"]["accounting_disclosures"] == [NOTICE]
    row = detail["screening"]["comparison_rows"][0]
    assert ("accounting_change_context" in row["financial_paths"]) is not legacy
    assert (
        encode_json(
            codec.dump_python(
                read_publication(path=path, selection_id="accounting-notice-policy").selection,
                mode="json",
            )
        )
        == canonical
    )
    assert not ScreeningSelectionStore(path).publish("accounting-notice-policy", selected)
    assert path.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=arguments["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="accounting-notice-policy",
        selection_summary="報告利益の改善と会社が明示した会計変更を分けて調べる",
        targets=(selected_target("1234"),),
    )
    app = tmp_path / "app"
    prepared = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=tmp_path / "workspace", runs_path=path, now=now
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert saved.inputs.screening_selection == selected
    projection = project_research_inputs(saved)
    assert (
        projection["screening_selection"]["result"]["comparison_rows"][0]["financial_paths"]
        == row["financial_paths"]
    )
    digest = selection_sha256(saved)
    path.rename(tmp_path / "unavailable-runs")
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=tmp_path / "workspace",
        now=now + timedelta(days=1),
    )
    assert resumed.operation_id == prepared.operation_id
    reread = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert selection_sha256(reread) == digest
    assert project_research_inputs(reread) == projection
    assert not path.exists()
