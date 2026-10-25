"""保存モデル→Selection→再開・提案入力の経路。投資成績やAI判断の証明ではない。"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import yaml
from tests.helpers.research_selection import selected_target, selection

from baibai_engine.investment_judgment.calibration_context import research_calibration_context
from baibai_engine.investment_judgment.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.inputs import project_research_inputs
from baibai_engine.investment_judgment.operation.progress import selection_binding
from baibai_engine.investment_judgment.operation.service import OperationService
from baibai_engine.investment_judgment.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.selection_query import load_generated_selection_context
from baibai_engine.investment_judgment.trade_proposal import GeneratedLiquidityContext
from baibai_engine.investment_judgment.trade_proposal_inputs import proposal_liquidity_context
from baibai_engine.investment_judgment.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.company_growth.models import CompanyGrowth
from baibai_engine.machine_analysis.generation_store import ModelGenerationStore
from baibai_engine.machine_analysis.model_generation import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy

NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
AS_OF = date(2026, 10, 23)


def seed_generation(path):
    payloads = {
        "company_valuation": {"earnings_price": {"earnings_yield": 0.12}, "financial": None},
        "technical_analysis": {"price_momentum": {"value": None, "reason": "missing_endpoint"}},
        "company_growth": CompanyGrowth(
            ticker="1234", status="unavailable", requested_start=None, requested_end=AS_OF
        ).model_dump(mode="json"),
        "external_assessment": {"forecast": None, "reason": "not_covered"},
        "market_context": {"price": {"avg_turnover_20d_oku": 1.25}},
    }
    models = [
        GeneratedModel(
            model_id=name, subject="1234", calculation_revision="synthetic-v1", payload=payload
        )
        for name, payload in payloads.items()
    ]
    models.append(
        GeneratedModel(
            model_id="market_context",
            subject="market",
            calculation_revision="synthetic-v1",
            payload={"reading": {"rates": 0.02}},
        )
    )
    generation = ModelGeneration(
        generation_id="fixed-synthetic",
        as_of=AS_OF,
        evaluation_time=NOW - timedelta(minutes=1),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="synthetic", sha256="a" * 64)
        },
        models=tuple(models),
    )
    ModelGenerationStore(path).publish(generation)
    ScreeningSelectionStore(path).publish(
        "policy-1",
        select_generation(
            generation,
            tickers=("1234", "5678"),
            policy=TwoViewPolicy(earnings_limit=20, combined_limit=20),
        ),
    )
    return generation


def test_generated_context_does_not_reselect_and_keeps_non_candidates(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    seed_generation(path)
    request = ResearchRequest(text="比較する", scope="discover_new")

    def forbidden(*args, **kwargs):
        raise AssertionError("must not calculate candidates or live model inputs")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.model_projection.select_generation", forbidden
    )
    result = load_generated_selection_context(
        request=request,
        generation_id=None,
        screening_selection_id="policy-1",
        research_as_of=AS_OF,
        known_at=NOW,
        runs_path=path,
        db_path=tmp_path / "absent",
        comparison_tickers=("5678",),
    )
    assert result["generation_id"] == "fixed-synthetic"
    assert result["model_inputs"]["securities"]["5678"]["not_generated"]
    assert result["candidate_tickers"] == ["1234"]
    assert "machine_valuation" not in result["model_inputs"]["securities"]["1234"]


def test_prepare_resume_and_proposal_use_saved_generation_not_mutable_inputs(tmp_path, monkeypatch):
    runs = tmp_path / "runs.sqlite"
    generation = seed_generation(runs)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="調査する", scope="specified_stocks", requested_tickers=("1234",)
        ),
        research_as_of=AS_OF,
        input_known_at=NOW,
        generation_id=generation.generation_id,
        screening_selection_id="policy-1",
        selection_summary="根拠を比較",
        targets=(selected_target(),),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("new preparation must not read legacy or mutable model inputs")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workspace.selected_prepare.collect_research_inputs",
        forbidden,
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workspace.selected_prepare.load_analysis_snapshot",
        forbidden,
    )
    workspace = tmp_path / "research"
    db = tmp_path / "app.sqlite"
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=db,
        workspace=workspace,
        runs_path=runs,
        sqlite_path=tmp_path / "missing-market",
        indicators_path=tmp_path / "missing-indicators",
        now=NOW,
    )
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    assert saved.schema_version == 3
    assert saved.inputs.screening_selection.result.policy.earnings_limit == 20
    digest = selection_sha256(saved)
    original = project_research_inputs(saved)
    assert "machine_valuation" not in original["securities"]["1234"]
    assert original["screening_selection"]["result"]["policy"]["earnings_limit"] == 20
    assert "not_generated" in original["securities"]["1234"]
    (adv,) = proposal_liquidity_context(saved)
    assert isinstance(adv, GeneratedLiquidityContext)
    assert adv.adv_yen == Decimal("125000000")
    assert (
        research_calibration_context(saved, context_path=tmp_path / "absent", as_of=AS_OF)["status"]
        == "not_applicable"
    )
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=db, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == digest
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == original
    assert project_research_inputs(parse_selection(saved.model_dump(mode="json"))) == original


def test_projection_is_detached_and_old_selection_hash_is_unchanged(tmp_path):
    runs = tmp_path / "runs"
    seed_generation(runs)
    saved = ModelGenerationStore(runs).read_subjects("fixed-synthetic", subjects=("1234", "market"))
    before = saved.model_dump_json()
    projection = project_generated_inputs(saved)
    projection["securities"]["1234"]["company_valuation"]["earnings_price"]["earnings_yield"] = 999
    assert saved.model_dump_json() == before
    legacy = selection(now=NOW, tickers=("1234",))
    raw = legacy.model_dump(mode="json")
    restored = parse_selection(raw)
    assert restored.model_dump(mode="json") == raw
    assert selection_sha256(restored) == selection_sha256(legacy)


def test_generation_after_cutoff_is_rejected(tmp_path):
    path = tmp_path / "runs"
    seed_generation(path)
    with pytest.raises(ValueError, match="after the research cutoff"):
        load_generated_selection_context(
            request=ResearchRequest(
                text="調査", scope="specified_stocks", requested_tickers=("1234",)
            ),
            generation_id="fixed-synthetic",
            screening_selection_id=None,
            research_as_of=AS_OF,
            known_at=NOW - timedelta(days=1),
            runs_path=path,
            db_path=tmp_path / "missing",
        )
