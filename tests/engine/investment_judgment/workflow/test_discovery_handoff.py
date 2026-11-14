"""候補外の明示調査を固定し、保存財務Selectionの意味を差し替えない。"""

from datetime import UTC, datetime

import yaml
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)

NOW = datetime(2026, 11, 14, 11, tzinfo=UTC)


def test_nonfinancial_candidate_prepares_and_resumes_without_relabeling_saved_policy(tmp_path):
    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "research"
    financial_selection = select_economic_paths(
        (
            CompanyCoordinates(
                ticker="1234",
                name="保存財務候補",
                sector="機械",
                paths=(
                    PathCoordinates(
                        "earning_power", "annual_ttm", 0.1, 0.2, "market", "sector", True, {}
                    ),
                ),
                context={},
                model_revisions={},
                not_generated=(),
            ),
        ),
        policy=EconomicPathPolicy(),
    )
    source = seed_generated_selection(
        runs=runs,
        now=NOW,
        tickers=("1234", "9999"),
        screening_result=financial_selection,
        models=(
            GeneratedModel(
                model_id="technical_analysis",
                subject="9999",
                calculation_revision="synthetic-ta-v1",
                payload={"price_momentum": None},
            ),
            GeneratedModel(
                model_id="company_financials",
                subject="9999",
                calculation_revision="synthetic-financial-v1",
                payload={
                    "financial": {"ocf_ttm": None},
                    "earnings_price": {"profit_ttm_yen": -7.0},
                },
            ),
        ),
    )
    assert source.inputs.screening_selection.result.tickers == ("1234",)
    target = selected_target("9999").model_copy(
        update={
            "rationale": "合成診断の別入口から明示選定。保存財務候補の順位とは別。",
            "research_question": "観測赤字と未観測CFを分けて回復仮説を反証する",
            "key_risk": "欠測を安全と誤認しない",
        }
    )
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(text="広い発見後の有限調査", scope="discover_new"),
        research_as_of=source.research_as_of,
        input_known_at=source.input_known_at,
        generation_id=source.inputs.generation.generation_id,
        screening_selection_id=source.inputs.screening_selection_id,
        selection_summary="別入口の選定理由を明記し、財務Selectionは比較元のまま保持",
        targets=(target,),
    )
    source_before = runs.read_bytes()
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=app,
        workspace=workspace,
        runs_path=runs,
        now=NOW,
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert saved.targets == (target,)
    assert saved.inputs.screening_selection == source.inputs.screening_selection
    assert saved.inputs.screening_selection.result.tickers == ("1234",)
    assert saved.inputs.screening_selection.result.policy.method == "best-economic-path-v4"
    assert saved.inputs.generation.result("technical_analysis", "9999").payload == {
        "price_momentum": None
    }
    financial = saved.inputs.generation.result("company_financials", "9999").payload
    assert financial["financial"]["ocf_ttm"] is None
    assert financial["earnings_price"]["profit_ttm_yen"] == -7.0
    assert runs.read_bytes() == source_before
    projection, digest = project_research_inputs(saved), selection_sha256(saved)

    # 再開はTA探索・財務政策の再実行や可変runsへの依存ではない。
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=app, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == digest
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == projection
    assert selection_binding(OperationService(app).get(prepared.operation_id).payload) == saved
