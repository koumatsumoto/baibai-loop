"""株数確認の予備調査を同じ保存入力で読む。G・価格・採否は補わない。"""

from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_input_review import case
from tests.engine.machine_analysis.screening.test_unresolved_financial_input_review import (
    unresolved_case,
)
from tests.helpers.archived_technical_financial import (
    FOUR_STOCK_ORDER,
    stored_publication,
    stored_row,
)
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.technical_discovery import _metadata

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
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def seed(path, *, stale=False, unresolved=False):
    payloads = {
        "1234": case(reason="missing_capital_basis", technical=False),
        "2345": case(expansion=True, reason="missing_capital_basis", technical=False),
        "3456": case(),
        "4567": case(reason="unknown_reason", technical=False),
    }
    payloads["2345"]["company_growth"]["periods"][-1]["observations"][-1]["metrics"]["net_assets"][
        "value"
    ] = -22.0
    if stale:
        for ticker in ("1234", "2345"):
            payloads[ticker]["company_financials"]["earnings_price"].update(
                unavailable_reason="stale_price",
                price_date="2026-11-12",
                shares_ex_treasury=100.0,
                market_cap_yen=10000.0,
            )
    if unresolved:
        payloads["1234"] = unresolved_case(stale=stale)
    generated = []
    for ticker, data in payloads.items():
        data["company_growth"]["ticker"] = ticker
        for period in data["company_growth"]["periods"]:
            for observation in period["observations"]:
                if observation["measurement"] == "reported":
                    for metric in ("parent_profit", "reported_basic_eps", "operating_cash_flow"):
                        observation["metrics"].setdefault(
                            metric, {"value": None, "reason": "not_observed", "documents": []}
                        )
        generated.extend(
            GeneratedModel(
                model_id=name, subject=ticker, calculation_revision="test-v1", payload=payload
            )
            for name, payload in data.items()
        )
    metadata = _metadata().model_copy(
        update={
            "subject_plan": {
                ticker: GenerationSubject(scope="stock", model_ids=tuple(data))
                for ticker, data in payloads.items()
            }
        }
    )
    fixed = ModelGeneration(**dict(metadata), models=tuple(generated))
    publish_generation(fixed, path=path)
    reason = {
        "path_id": "earning_power",
        "reason": "stale_price" if stale else "missing_capital_basis",
        "price_date": "2026-11-12" if stale else "2026-11-13",
        "expected_price_date": "2026-11-13",
        "generation_id": fixed.generation_id,
    }
    rows = (
        stored_row(
            "1234",
            lane="input_review",
            gate={"state": "input_unresolved" if unresolved else "not_selected"},
            paths={"input_review_reasons": [reason]},
        ),
        stored_row(
            "2345",
            lane="input_review",
            gate={
                "state": "not_selected",
                "checks": {"positive_net_assets": {"state": "not_met", "value": -22.0}},
            },
            paths={"input_review_reasons": [reason]},
        ),
        stored_row("3456", lane="alternative_financial_research", gate={"state": "not_selected"}),
        stored_row("4567", lane="no_current_hypothesis", gate={"state": "not_selected"}),
    )
    selection = stored_publication(fixed.generation_id, rows, order=FOUR_STOCK_ORDER, page_size=1)
    identity = "archived-financial-review"
    ScreeningSelectionStore(path).publish(identity, selection)
    publication = read_publication(path=path, selection_id=identity)
    return fixed, selection, identity, publication.published_at + timedelta(seconds=1)


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("unresolved", [False, True])
def test_review_reasons_survive_finite_detail_and_saved_selection_resume(
    tmp_path, stale, unresolved
):
    path = tmp_path / "runs"
    fixed, selected, identity, now = seed(path, stale=stale, unresolved=unresolved)
    original_bytes = path.read_bytes()
    kwargs = dict(
        request=ResearchRequest(text="株数基準の確認", scope="discover_new"),
        generation_id=fixed.generation_id,
        screening_selection_id=identity,
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=tmp_path / "app",
    )
    detail = load_generated_selection_context(**kwargs, comparison_tickers=("1234", "2345", "4567"))
    saved_rows = {row.ticker: row for row in selected.result.rows}
    for row in detail["screening"]["comparison_rows"]:
        assert row == TypeAdapter(type(saved_rows[row["ticker"]])).dump_python(
            saved_rows[row["ticker"]], mode="json"
        )
    assert (
        detail["screening"]["comparison_rows"][1]["financial_gate"]["checks"][
            "positive_net_assets"
        ]["value"]
        == -22.0
    )
    with pytest.raises(ValueError, match="not yet published"):
        load_generated_selection_context(**{**kwargs, "known_at": now - timedelta(seconds=2)})
    with pytest.raises(ValueError, match="differs"):
        load_generated_selection_context(**{**kwargs, "generation_id": "different"})
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=kwargs["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id=identity,
        selection_summary="株数不足と既知の成長非合致を区別し、原典確認を明示選択する",
        targets=(selected_target("1234"), selected_target("2345")),
    )
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=path,
        now=now,
    )
    operation = OperationService(tmp_path / "app").get(prepared.operation_id)
    saved = selection_binding(operation.payload)
    digest = selection_sha256(saved)
    projection = project_research_inputs(saved)
    assert (
        projection["screening_selection"]["result"]["comparison_rows"][1]["financial_gate"]["state"]
        == "not_selected"
    )
    assert path.read_bytes() == original_bytes
    path.rename(tmp_path / "runs-not-available")
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=now + timedelta(days=1),
    )
    assert resumed.operation_id == prepared.operation_id
    assert (
        selection_sha256(
            selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
        )
        == digest
    )
    assert project_research_inputs(saved) == projection
    assert not path.exists()
