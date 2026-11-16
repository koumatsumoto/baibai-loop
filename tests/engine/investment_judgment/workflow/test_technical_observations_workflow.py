"""初回の技術条件の数値・窓を固定選択と元runsなし再開へ渡す。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_technical_financial_context import args, seed
from tests.engine.machine_analysis.screening.test_technical_financial import evaluate_fixed
from tests.engine.machine_analysis.screening.test_technical_strategy import (
    _conditioned,
    _strategy_models,
)
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
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


@pytest.mark.parametrize("legacy", [False, True])
def test_initial_card_to_detached_research_preserves_recorded_or_old_absent_observations(
    tmp_path, legacy
):
    path = tmp_path / "runs"
    fixed, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "conditioned-context-fixed"})
    for model in fixed.models:
        if model.subject == "1234" and model.model_id == "market_context":
            model.payload.setdefault("relative", {})["market_conditioned_60d"] = _conditioned(
                _strategy_models()
            )
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    models = tuple(model for model in fixed.models if model.subject == "1234")
    strategy = evaluate_technical_strategy("1234", models=models, metadata=fixed)
    expected = strategy["observations"]
    conditioned = strategy["supporting_context"]["market_conditioned_60d"]
    assert selected.result.policy.method == "technical-financial-discovery-v25"
    if legacy:
        selected = replace(
            selected,
            result=replace(
                selected.result,
                policy=TechnicalFinancialPolicy(method="technical-financial-discovery-v9"),
                rows=tuple(
                    replace(
                        row,
                        technical={
                            k: v
                            for k, v in row.technical.items()
                            if k not in {"observations", "market_conditioned_60d"}
                        },
                    )
                    for row in selected.result.rows
                ),
            ),
        )
    codec = TypeAdapter(ScreeningSelection)
    canonical = encode_json(codec.dump_python(selected, mode="json"))
    ScreeningSelectionStore(path).publish("observations-fixed", selected)
    publication = read_publication(path=path, selection_id="observations-fixed")
    now = publication.published_at + timedelta(seconds=1)
    arguments = {**args(path, now), "screening_selection_id": "observations-fixed"}
    before = path.read_bytes()
    target_page = (
        selected.result.lane_tickers("growth_research").index("1234")
        // selected.result.policy.page_size
    )
    first = load_generated_selection_context(**arguments, screening_page=target_page)
    assert first["model_inputs"]["securities"] == {}
    card = next(card for card in first["screening"]["cards"] if card["ticker"] == "1234")
    assert ("observations" in card["technical"]) is not legacy
    assert ("market_conditioned_60d" in card["technical"]) is not legacy
    if not legacy:
        assert card["technical"]["observations"] == expected
        actual = card["technical"]["market_conditioned_60d"]
        assert actual["source_state"] == conditioned["source_state"]
        assert actual["source_field"] == conditioned["source_field"]
        for benchmark, source in conditioned["value"]["benchmarks"].items():
            assert actual["value"]["benchmarks"][benchmark]["down"] == source["down"]
    detail = load_generated_selection_context(**arguments, comparison_tickers=("1234",))
    row = detail["screening"]["comparison_rows"][0]
    assert row["technical"] == next(r.technical for r in selected.result.rows if r.ticker == "1234")
    for model in models:
        assert (
            detail["model_inputs"]["securities"]["1234"]["models"][model.model_id] == model.payload
        )
    assert (
        encode_json(
            codec.dump_python(
                read_publication(path=path, selection_id="observations-fixed").selection,
                mode="json",
            )
        )
        == canonical
    )
    assert not ScreeningSelectionStore(path).publish("observations-fixed", selected)
    assert path.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=arguments["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="observations-fixed",
        selection_summary="相対超過と絶対騰落、条件日数と区間を分けて読む",
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
        projection["screening_selection"]["result"]["comparison_rows"][0]["technical"]
        == row["technical"]
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
