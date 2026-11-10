"""価格入力集計は選定時の値を渡し、未記録の旧Selectionを補完しない。"""

import gzip
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_economic_path_inputs import (
    metadata,
    models,
    payloads,
)
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.model_generation import publish_generation

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
from baibai_engine.machine_analysis.generation.contracts import ModelGeneration
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    evaluate_economic_coordinates,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


@pytest.mark.parametrize("kind", ["missing_price", "nonpositive_business", "empty_universe"])
def test_zero_candidate_price_inputs_reach_initial_and_fixed_research(tmp_path, kind):
    data = payloads()
    if kind == "missing_price":
        data["company_financials"]["earnings_price"]["price_date"] = None
    elif kind == "nonpositive_business":
        earnings = data["company_financials"]["earnings_price"]
        earnings.update(earnings_yield=-0.1, profit_ttm_yen=-1000.0)
        earnings["profit_components"][0]["amount_yen"] = -1000.0
        for period in data["company_growth"]["periods"]:
            for observation in period["observations"]:
                observation["metrics"]["operating_margin"]["value"] = -0.1
                observation["metrics"]["operating_profit"]["value"] = -100.0
        for change in data["company_growth"]["changes"]:
            change["difference"] = -10.0
            change["rate"] = -0.1 if change["metric"] == "sales" else None
    tickers = () if kind == "empty_universe" else ("1234",)
    generation = ModelGeneration(**metadata().model_dump(), models=models(data) if tickers else ())
    runs, app = tmp_path / "runs", tmp_path / "app"
    publish_generation(generation, path=runs)
    evaluation = evaluate_economic_coordinates(
        metadata(),
        tickers=tickers,
        coordinates={
            ticker: project_company(ticker, models=generation.models, metadata=metadata())
            for ticker in tickers
        },
        policy=EconomicPathPolicy(),
    )
    assert evaluation.selection.result.tickers == ()
    summary = evaluation.price_inputs
    assert summary["universe_count"] == len(tickers)
    assert summary["valid_count"] == (1 if kind == "nonpositive_business" else 0)
    assert summary["invalid_count"] == (1 if kind == "missing_price" else 0)
    store = ScreeningSelectionStore(runs)
    store.publish("policy", evaluation.selection)
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute("UPDATE screening_selection SET published_at='2026-01-06T00:00:00Z'")
    known = datetime(2026, 1, 8, tzinfo=UTC)
    request = ResearchRequest(text="合成の候補調査", scope="discover_new")
    context = load_generated_selection_context(
        request=request,
        generation_id=None,
        screening_selection_id=None,
        research_as_of=None,
        known_at=known,
        runs_path=runs,
        db_path=app,
    )
    assert context["status"] == "ready"
    assert context["candidate_tickers"] == []
    assert context["screening"]["eligible_count"] == 0
    assert context["screening"]["price_inputs"] == summary
    assert summary["price_dates"] == (
        {} if not tickers else {"missing" if kind == "missing_price" else "2026-01-05": 1}
    )
    assert store.read("policy") == evaluation.selection
    assert not store.publish("policy", evaluation.selection)
    with pytest.raises(ValueError, match="identity has different content"):
        store.publish("policy", replace(evaluation.selection, price_inputs=None))
    prepared = prepare_selected_workspace(
        draft=GeneratedResearchSelectionDraft(
            schema_version=2,
            request=request,
            research_as_of=known.date(),
            input_known_at=known,
            generation_id=generation.generation_id,
            screening_selection_id="policy",
            selection_summary="合成の不足確認",
            targets=(),
        ),
        db_path=app,
        workspace=tmp_path / "workspace",
        runs_path=runs,
        now=known,
    )
    selected = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert selected.inputs.screening_selection == evaluation.selection
    projected = project_research_inputs(selected)
    assert projected["screening_selection"]["price_inputs"] == summary
    inputs_before = (prepared.workspace / "domain-inputs.yaml").read_bytes()
    app_before = app.read_bytes()
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=prepared.workspace,
        now=known,
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert (resumed.workspace / "domain-inputs.yaml").read_bytes() == inputs_before
    assert app.read_bytes() == app_before


def test_legacy_selection_keeps_hash_payload_and_resume_without_price_summary(tmp_path):
    now = datetime(2026, 11, 1, tzinfo=UTC)
    runs, app = tmp_path / "runs", tmp_path / "app"
    original = seed_generated_selection(runs=runs, now=now)
    # 追加前のmain 18135aeで同じ合成fixtureを保存して取得したhash。
    expected_hash = "aaed9564f60b8fa9e0f6cd23d407ed09b1a67a5513fd303a59442242d0879e56"
    assert selection_sha256(original) == expected_hash
    identity = original.inputs.screening_selection_id
    store = ScreeningSelectionStore(runs)
    saved = store.read(identity)
    assert saved.price_inputs is None
    with closing(sqlite3.connect(runs)) as connection:
        before = connection.execute("SELECT * FROM screening_selection").fetchall()
    raw = gzip.decompress(before[0][3])
    assert b'"price_inputs"' not in raw
    assert (
        canonical_json(TypeAdapter(ScreeningSelection).dump_python(saved, mode="json")).encode()
        == raw
    )
    assert not store.publish(identity, saved)
    with pytest.raises(ValueError, match="identity has different content"):
        store.publish(identity, replace(saved, price_inputs={"universe_count": 0}))
    with closing(sqlite3.connect(runs)) as connection:
        assert connection.execute("SELECT * FROM screening_selection").fetchall() == before
    context = load_generated_selection_context(
        request=original.request,
        generation_id=None,
        screening_selection_id=None,
        research_as_of=None,
        known_at=now,
        runs_path=runs,
        db_path=app,
    )
    assert "price_inputs" not in context["screening"]
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(original),
        db_path=app,
        workspace=tmp_path / "workspace",
        runs_path=runs,
        now=now,
    )
    assert prepared.selection_sha256 == expected_hash
    selected = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert selected.model_dump(mode="json") == original.model_dump(mode="json")
    assert "price_inputs" not in project_research_inputs(selected)["screening_selection"]
    inputs_before = (prepared.workspace / "domain-inputs.yaml").read_bytes()
    app_before = app.read_bytes()
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=prepared.workspace,
        now=now,
    )
    assert resumed.selection_sha256 == expected_hash
    assert (resumed.workspace / "domain-inputs.yaml").read_bytes() == inputs_before
    assert app.read_bytes() == app_before
