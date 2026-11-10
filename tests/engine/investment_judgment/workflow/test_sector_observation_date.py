"""業種表の価格日を固定入力へ渡し、日付のない過去成果へ補完しない。"""

from copy import deepcopy
from datetime import UTC, date, datetime
from hashlib import sha256

import pytest
import yaml
from tests.helpers.master import insert_master_rows
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
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
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_generation
from baibai_engine.machine_analysis.generation.runners.market_snapshot import build_market_snapshot
from baibai_engine.machine_analysis.generation.store import GenerationConflictError
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

ASOF = date(2026, 10, 26)
PRICE_DATE = date(2026, 10, 23)
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _generation(tmp_path, *, legacy):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", [100.0] * 61, end_date=PRICE_DATE)
    connection = open_connection(path)
    try:
        insert_master_rows(connection, [(str(ASOF), "1234", "合成企業", "Prime", "機械", "011")])
        connection.commit()
    finally:
        connection.close()
    market = build_market_snapshot(sqlite_path=path, asof_date=ASOF, history_weeks=1)
    if legacy:
        for row in market["sectors"]:
            row.pop("observation_date", None)
    return ModelGeneration(
        schema_version=2,
        generation_id="sector-old" if legacy else "sector-new",
        as_of=ASOF,
        evaluation_time=NOW,
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
        subject_plan={
            "1234": GenerationSubject(scope="stock", model_ids=()),
            "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        },
        models=(
            GeneratedModel(
                model_id="market_context",
                subject="market",
                calculation_revision="retained-v8"
                if legacy
                else "shared-market-context-bar-timing-v9-tradingview-sector-date",
                payload={"market": market},
            ),
        ),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_sector_price_date_survives_fixed_selection_and_resume_without_backfill(tmp_path, legacy):
    generation = _generation(tmp_path, legacy=legacy)
    market = generation.models[0].payload["market"]
    row = market["sectors"][0]
    assert market["as_of"] == str(ASOF)
    assert market["points"][0]["observation_date"] is None  # Benchmark is missing.
    if legacy:
        assert "observation_date" not in row
    else:
        assert row["observation_date"] == str(PRICE_DATE)
    runs = tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    assert read_generation(generation.generation_id, path=runs) == generation
    before = runs.read_bytes()
    assert not publish_generation(generation, path=runs)
    assert runs.read_bytes() == before
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="合成の業種価格日を確認", scope="specified_stocks", requested_tickers=("1234",)
        ),
        research_as_of=ASOF,
        input_known_at=NOW,
        generation_id=generation.generation_id,
        selection_summary="固定業種表を確認",
        targets=(selected_target(),),
    )
    context = load_generated_selection_context(
        request=draft.request,
        generation_id=generation.generation_id,
        screening_selection_id=None,
        research_as_of=ASOF,
        known_at=NOW,
        runs_path=runs,
        db_path=tmp_path / "absent-app",
    )
    assert (
        context["model_inputs"]["shared_subjects"]["market"]["models"]["market_context"]["market"]
        == market
    )
    app = tmp_path / "app.sqlite"
    workspace = tmp_path / "workspace"
    prepared = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=workspace, runs_path=runs, now=NOW
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    projection = project_research_inputs(saved)
    assert projection["shared_subjects"]["market"]["models"]["market_context"]["market"] == market
    raw = saved.model_dump(mode="json")
    assert parse_selection(raw).model_dump(mode="json") == raw
    assert selection_sha256(parse_selection(raw)) == prepared.selection_sha256
    if legacy:
        assert (
            prepared.selection_sha256
            == "f24da80253b44121ae272e1010c22104c2ef1eb9f9cbb43cbb9fa229856967e0"
        )
        # 固定された日付なしの旧入力を、現在のas_of/価格日で再解釈しない。
        assert (
            sha256(canonical_json(generation.model_dump(mode="json")).encode()).hexdigest()
            == "3aa66e9f68f1c041dd25381dd61c266bfc71c9754aab3fc670201b7d790895da"
        )
    changed = deepcopy(generation)
    changed.models[0].payload["market"]["sectors"][0]["observation_date"] = str(ASOF)
    with pytest.raises(GenerationConflictError, match="different model content"):
        publish_generation(changed, path=runs)
    assert runs.read_bytes() == before
    (tmp_path / "market.sqlite").unlink()
    runs.unlink()
    app_before = app.read_bytes()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=app, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == projection
    assert app.read_bytes() == app_before
