"""市場の窓・絶対/相対騰落・売買代金は、同じ世代の有限Caseで再読する。"""

from datetime import timedelta

import pytest
from tests.engine.machine_analysis.screening.test_activity_context import FIELD, INCOMPLETE_ACTIVITY
from tests.engine.machine_analysis.screening.test_technical_strategy import _strategy_models
from tests.helpers.archived_technical_financial import stored_publication, stored_row
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
from baibai_engine.machine_analysis.generation.contracts import GenerationSubject, ModelGeneration
from baibai_engine.machine_analysis.screening.discovery_contract import discovery_publication_id
from baibai_engine.machine_analysis.screening.discovery_index import read_discovery_index
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


@pytest.mark.parametrize("archive", [False, True], ids=["current_index", "stored_historical_row"])
def test_detached_market_evidence_keeps_same_source_without_backfilling_old_row(tmp_path, archive):
    runs, app = tmp_path / "runs", tmp_path / "app"
    models = _strategy_models()
    models[1].payload["price"][FIELD] = dict(INCOMPLETE_ACTIVITY)
    meta = _metadata().model_copy(
        update={
            "subject_plan": {
                "1234": GenerationSubject(
                    scope="stock", model_ids=tuple(m.model_id for m in models)
                )
            }
        }
    )
    publish_generation(ModelGeneration(**meta.model_dump(), models=models), path=runs)
    if archive:
        # 実際に保存した項目だけ。市場モデルに数値があっても、当時の行へ補わない。
        identity = "archived-market"
        recorded = stored_row("1234", lane="input_review", technical={"state": "not_matched"})
        source = stored_publication(meta.generation_id, (recorded,), order=("1234",))
    else:
        identity = discovery_publication_id(meta.generation_id)
        observed = project_discovery_row("1234", models=models, metadata=meta)
        source = evaluate_discovery_rows(meta, tickers=("1234",), rows={"1234": observed})
    ScreeningSelectionStore(runs).publish(identity, source)
    now = read_discovery_index(path=runs, selection_id=identity).published_at
    request = ResearchRequest(text="市場下落時の株価と売買代金の被覆を比較", scope="discover_new")
    context = dict(
        request=request,
        generation_id=meta.generation_id,
        screening_selection_id=identity,
        research_as_of=meta.as_of,
        known_at=now,
        runs_path=runs,
        db_path=app,
    )
    evidence = load_generated_selection_context(**context, comparison_tickers=("1234",))
    if archive:
        assert evidence["screening"]["comparison_rows"][0]["technical"] == recorded.technical
        assert FIELD not in recorded.technical
        assert "market_conditioned_60d" not in recorded.technical
    else:
        assert evidence["discovery"]["rows"] == [observed]
    expected = {m.model_id: m.payload for m in models}
    actual = evidence["model_inputs"]["securities"]["1234"]["models"]
    assert actual == expected
    assert evidence["input_known_at"] == now.isoformat()
    assert (
        actual["technical_analysis"]["price_path"]["observations"]["R20"]["window_end_date"]
        == "2026-11-13"
    )
    conditions = actual["market_context"]["relative"]["market_conditioned_60d"]
    assert conditions["window_start_date"] == "2026-08-14"
    assert conditions["benchmarks"]["1321"]["down"]["own_mean_return"] > 0
    assert conditions["benchmarks"]["1306"]["down"]["own_mean_return"] < 0
    assert conditions["benchmarks"]["1321"]["down"]["days"] == 25

    prepared = prepare_selected_workspace(
        draft=GeneratedResearchSelectionDraft(
            schema_version=2,
            request=request,
            research_as_of=meta.as_of,
            input_known_at=now,
            generation_id=meta.generation_id,
            screening_selection_id=identity,
            selection_summary="期間・比較対象と実際に観測できた商いを分ける",
            targets=(selected_target(),),
        ),
        workspace=tmp_path / "case",
        db_path=app,
        runs_path=runs,
        now=now,
    )
    operation = OperationService(app)
    saved = selection_binding(operation.get(prepared.operation_id).payload)
    original = selection_sha256(saved)
    runs.rename(tmp_path / "source-offline")
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=tmp_path / "case",
        now=now + timedelta(days=1),
    )
    frozen = selection_binding(operation.get(resumed.operation_id).payload)
    assert selection_sha256(frozen) == original
    projection = project_research_inputs(frozen)
    assert projection["securities"]["1234"]["models"] == expected
    turnover = projection["securities"]["1234"]["models"]["market_context"]["price"][FIELD]
    assert (turnover["current_yen"], turnover["prior_valid_days"], turnover["prior_mean_yen"]) == (
        0,
        9,
        None,
    )
    result = projection["screening_selection"]["result"]
    if archive:
        assert result["comparison_rows"][0]["technical"] == recorded.technical
    else:
        assert result["rows"] == [observed]
    assert not runs.exists()
