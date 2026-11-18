"""利益の資金化を反証できるCF原期は初回/詳細/固定Researchで同じ。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_financial_input_review_context import seed
from tests.engine.machine_analysis.screening.test_cash_flow_context import CASH_FLOW, FIELD
from tests.helpers.archived_technical_financial import FOUR_STOCK_ORDER, archived_from_models
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
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("source_limits", [False, True])
@pytest.mark.parametrize("financing_details", [False, True])
def test_first_comparison_and_detached_research_keep_cashflow_without_backfilling_old(
    tmp_path, legacy, source_limits, financing_details
):
    path = tmp_path / "runs"
    fixed, _, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "cashflow-context-fixed"})
    context = deepcopy(CASH_FLOW)
    if source_limits:
        context.update(
            edinet_reported_flows={
                "amounts": {"operating_cash_flow": -900, "free_cash_flow": None},
                "consolidation_basis": "nonconsolidated",
                "source_doc_id": "synthetic-edinet",
            },
            edinet_failure_reasons="metric_unavailable:capex,entity_scale_mismatch",
        )
    if financing_details:
        context.setdefault("edinet_reported_flows", {}).setdefault("amounts", {}).update(
            financing_lease_repayments_cash_effect=-12,
            financing_noncontrolling_dividends_cash_effect=None,
        )
    for model in fixed.models:
        if model.subject == "1234" and model.model_id == "company_financials":
            model.payload.setdefault("financial", {}).update(deepcopy(context))
    publish_generation(fixed, path=path)
    selected = archived_from_models(fixed, order=FOUR_STOCK_ORDER)
    assert selected.result.policy.method == "technical-financial-discovery-v26"
    if legacy:
        selected = replace(
            selected,
            result=replace(
                selected.result,
                policy=TechnicalFinancialPolicy(method="technical-financial-discovery-v8"),
                rows=tuple(
                    replace(
                        row,
                        financial_gate={k: v for k, v in row.financial_gate.items() if k != FIELD},
                    )
                    for row in selected.result.rows
                ),
            ),
        )
    codec = TypeAdapter(ScreeningSelection)
    canonical = encode_json(codec.dump_python(selected, mode="json"))
    ScreeningSelectionStore(path).publish("cashflow-policy", selected)
    publication = read_publication(path=path, selection_id="cashflow-policy")
    now = publication.published_at + timedelta(seconds=1)
    args = dict(
        request=ResearchRequest(
            text="利益の成長と資金化・測定期を分けて比較", scope="discover_new"
        ),
        generation_id=None,
        screening_selection_id="cashflow-policy",
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=tmp_path / "app",
    )
    before = path.read_bytes()
    first = load_generated_selection_context(**args, screening_lane="input_review")
    assert first["model_inputs"]["securities"] == {}
    card = next(c for c in first["screening"]["cards"] if c["ticker"] == "1234")
    assert (FIELD in card["financial_gate"]) is not legacy
    if not legacy:
        assert card["financial_gate"][FIELD] == context
    detail = load_generated_selection_context(**args, comparison_tickers=("1234",))
    financial = detail["model_inputs"]["securities"]["1234"]["models"]["company_financials"]
    assert {key: financial["financial"][key] for key in context} == context
    restored = read_publication(path=path, selection_id="cashflow-policy").selection
    assert encode_json(codec.dump_python(restored, mode="json")) == canonical
    ScreeningSelectionStore(path).publish("cashflow-policy", selected)
    assert path.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=args["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="cashflow-policy",
        selection_summary="利益の成長を資金回収や健全性の認定にしない",
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
    assert projection["securities"]["1234"]["models"]["company_financials"] == financial
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
