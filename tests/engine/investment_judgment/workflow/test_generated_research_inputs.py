"""保存モデル→Selection→再開・提案入力の経路。投資成績やAI判断の証明ではない。"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import yaml
from tests.helpers.research_selection import selected_target, selection
from tests.helpers.retained_model_generation import seed_generation

from baibai_engine.investment_judgment.trade_proposal.contracts import GeneratedLiquidityContext
from baibai_engine.investment_judgment.trade_proposal.inputs import proposal_liquidity_context
from baibai_engine.investment_judgment.workflow.calibration_context import (
    research_calibration_context,
)
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
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
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore

NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
AS_OF = date(2026, 10, 23)


def test_generated_context_does_not_reselect_and_keeps_non_candidates(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    seed_generation(path, now=NOW, as_of=AS_OF)
    request = ResearchRequest(text="比較する", scope="discover_new")

    def forbidden(*args, **kwargs):
        raise AssertionError("must not calculate candidates or live model inputs")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.economic_path_selection.select_saved_economic_generation",
        forbidden,
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


@pytest.mark.parametrize("with_price_path", [False, True])
def test_prepare_resume_and_proposal_use_saved_generation_not_mutable_inputs(
    tmp_path, monkeypatch, with_price_path
):
    runs = tmp_path / "runs.sqlite"
    generation = seed_generation(runs, now=NOW, as_of=AS_OF, with_price_path=with_price_path)
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

    workspace = tmp_path / "research"
    db = tmp_path / "app.sqlite"
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=db,
        workspace=workspace,
        runs_path=runs,
        now=NOW,
    )
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    assert saved.schema_version == 4
    assert saved.inputs.screening_selection.result.policy.earnings_limit == 20
    digest = selection_sha256(saved)
    original = project_research_inputs(saved)
    assert "machine_valuation" not in original["securities"]["1234"]
    assert original["screening_selection"]["result"]["policy"]["earnings_limit"] == 20
    assert "not_generated" in original["securities"]["1234"]
    assert ("price_path" in original["securities"]["1234"]["technical_analysis"]) == with_price_path
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
    seed_generation(runs, now=NOW, as_of=AS_OF)
    saved = ModelGenerationStore(runs).read_subjects("fixed-synthetic", subjects=("1234", "market"))
    before = saved.model_dump_json()
    projection = project_generated_inputs(saved)
    projection["securities"]["1234"]["company_financials"]["earnings_price"]["earnings_yield"] = 999
    assert saved.model_dump_json() == before
    legacy = selection(now=NOW, tickers=("1234",))
    raw = legacy.model_dump(mode="json")
    restored = parse_selection(raw)
    assert restored.model_dump(mode="json") == raw
    assert selection_sha256(restored) == selection_sha256(legacy)


def test_generation_after_cutoff_is_rejected(tmp_path):
    path = tmp_path / "runs"
    seed_generation(path, now=NOW, as_of=AS_OF)
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


def test_context_cli_uses_fixed_generation_and_keeps_comparison_subjects(tmp_path, capsys):
    import json

    from baibai_engine.cli.investment_judgment import main

    runs = tmp_path / "runs.sqlite"
    seed_generation(runs, now=NOW, as_of=AS_OF)
    before = runs.read_bytes()
    assert (
        main(
            [
                "selection-context",
                "--request",
                "合成比較",
                "--scope",
                "discover_new",
                "--generation-id",
                "fixed-synthetic",
                "--screening-selection-id",
                "policy-1",
                "--runs-path",
                str(runs),
                "--db",
                str(tmp_path / "absent-app"),
                "--compare-ticker",
                "5678",
            ],
            now=NOW,
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["request"]["scope"] == "discover_new"
    assert result["generation_id"] == "fixed-synthetic"
    assert result["candidate_tickers"] == ["1234"]
    assert result["model_inputs"]["securities"]["5678"]["not_generated"]
    assert runs.read_bytes() == before


@pytest.mark.parametrize("explicit", [False, True])
def test_policy_published_after_cutoff_is_not_admitted(tmp_path, explicit):
    runs = tmp_path / "runs.sqlite"
    seed_generation(runs, now=NOW, as_of=AS_OF)
    kwargs = dict(
        request=ResearchRequest(text="調査", scope="discover_new"),
        generation_id=None,
        screening_selection_id="policy-1" if explicit else None,
        research_as_of=AS_OF,
        known_at=NOW - timedelta(seconds=45),
        runs_path=runs,
        db_path=tmp_path / "absent",
    )
    if explicit:
        with pytest.raises(ValueError, match="not yet published"):
            load_generated_selection_context(**kwargs)
    else:
        assert load_generated_selection_context(**kwargs)["status"] == "inputs_required"


def test_scoped_additional_models_reach_fixed_selection_and_resume(tmp_path):
    """追加モデル・業種・複数市場を保存から再開まで通し、候補政策とは分離する。"""
    from baibai_engine.machine_analysis.generation.contracts import (
        GeneratedModel,
        GenerationSubject,
        ModelGeneration,
    )
    from baibai_engine.machine_analysis.generation.query import read_research_model_inputs

    runs = tmp_path / "runs.sqlite"
    original = seed_generation(runs, now=NOW, as_of=AS_OF)
    stock_models = tuple(model.model_id for model in original.models if model.subject == "1234")
    plan = {
        "1234": GenerationSubject(
            scope="stock",
            model_ids=(*stock_models, "stress_probe"),
            related_subjects=("sector:food",),
        ),
        "5678": GenerationSubject(
            scope="stock",
            model_ids=("stress_probe",),
            related_subjects=("sector:food",),
        ),
        "9999": GenerationSubject(scope="stock", model_ids=("stress_probe",)),
        "sector:food": GenerationSubject(scope="sector", model_ids=("sector_probe",)),
        "sector:unrelated": GenerationSubject(scope="sector", model_ids=("sector_probe",)),
        "market": GenerationSubject(scope="market", model_ids=("market_context", "macro_probe")),
        "market:world": GenerationSubject(scope="market", model_ids=("macro_probe",)),
    }
    # A test-only sixth model is explicitly composed from shared observations.
    # It is not registered in production and does not add a candidate-policy vote.
    from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

    def stress_probe(bars):
        if len(bars) < 2:
            return {"value": None, "reason": "not_observed"}
        return {"value": (bars[-1].price / bars[0].price - 1) * 100}

    shared_prices = {
        "1234": (PriceBar(AS_OF, 100.0, None),),
        "9999": (
            PriceBar(AS_OF - timedelta(days=1), 100.0, None),
            PriceBar(AS_OF, 110.0, None),
        ),
    }
    extra = tuple(
        GeneratedModel(
            model_id=model, subject=subject, calculation_revision="probe-v1", payload=payload
        )
        for model, subject, payload in (
            ("stress_probe", "1234", stress_probe(shared_prices["1234"])),
            ("stress_probe", "9999", stress_probe(shared_prices["9999"])),
            ("sector_probe", "sector:food", {"value": 0.7}),
            ("sector_probe", "sector:unrelated", {"value": 0.9}),
            ("macro_probe", "market", {"value": 0}),
            ("macro_probe", "market:world", {"value": None, "reason": "not_observed"}),
        )
    )
    generation = ModelGeneration.model_validate(
        {
            **original.model_dump(),
            "schema_version": 2,
            "generation_id": "scoped-synthetic",
            "subject_plan": plan,
            "models": (*original.models, *extra),
        }
    )
    ModelGenerationStore(runs).publish(generation)
    fixed = read_research_model_inputs(
        generation.generation_id, targets=("1234", "5678", "7777"), path=runs
    )
    assert set(fixed.subjects) == {"1234", "5678", "7777", "sector:food", "market", "market:world"}
    assert set(fixed.subject_plan) == set(fixed.subjects) - {"7777"}
    from baibai_engine.investment_judgment.workflow.generated_inputs import verify_generated_inputs

    # A canonical stock-only slice is insufficient for starting Research: shared
    # requirements must be derived from the full generation, not that slice.
    truncated = ModelGenerationStore(runs).read_subjects(
        generation.generation_id, subjects=("1234", "5678", "sector:food")
    )
    with pytest.raises(ValueError, match="differs from its canonical generation"):
        verify_generated_inputs(truncated, targets=("1234", "5678"), runs_path=runs)
    verify_generated_inputs(fixed, targets=("1234", "5678", "7777"), runs_path=runs)
    projected = project_generated_inputs(fixed)
    assert projected["securities"]["1234"]["models"]["stress_probe"] == {
        "value": None,
        "reason": "not_observed",
    }
    assert "stress_probe" not in projected["securities"]["1234"]["not_generated"]
    assert projected["securities"]["5678"]["not_generated"] == ["stress_probe"]
    assert projected["shared_subjects"]["sector:food"]["models"]["sector_probe"]["value"] == 0.7
    assert projected["shared_subjects"]["market"]["models"]["macro_probe"]["value"] == 0
    assert projected["shared_subjects"]["market:world"]["models"]["macro_probe"]["value"] is None
    assert projected["securities"]["7777"]["generation_scope_status"] == "outside_generation_scope"
    assert projected["securities"]["7777"]["models"] == {}
    assert projected["securities"]["7777"]["not_generated"] == []
    assert "7777" not in generation.subject_plan
    assert "9999" not in projected["securities"]
    assert "sector:unrelated" not in projected["shared_subjects"]

    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="固定した複数モデルを比較",
            scope="specified_stocks",
            requested_tickers=("1234", "5678", "7777"),
        ),
        research_as_of=AS_OF,
        input_known_at=NOW,
        generation_id=generation.generation_id,
        selection_summary="候補政策を変えず追加モデルを調査する",
        targets=(selected_target("1234"), selected_target("5678"), selected_target("7777")),
    )
    workspace, database = tmp_path / "research", tmp_path / "application.sqlite"
    prepared = prepare_selected_workspace(
        draft=draft, db_path=database, workspace=workspace, runs_path=runs, now=NOW
    )
    saved = selection_binding(OperationService(database).get(prepared.operation_id).payload)
    before = project_research_inputs(saved)
    digest = selection_sha256(saved)
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=database, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == digest
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == before
    assert project_research_inputs(parse_selection(saved.model_dump(mode="json"))) == before


def test_v1_generation_metadata_and_fixed_slice_do_not_gain_new_default_fields(tmp_path):
    import json
    import sqlite3
    from contextlib import closing

    from baibai_engine.machine_analysis.generation.contracts import (
        GenerationMetadata,
        ModelInputSlice,
    )
    from baibai_engine.primitives.json import canonical_json

    runs = tmp_path / "runs.sqlite"
    original = seed_generation(runs, now=NOW, as_of=AS_OF)
    expected = {
        "schema_version": 1,
        "generation_id": "fixed-synthetic",
        "as_of": "2026-10-23",
        "evaluation_time": "2026-10-25T08:59:00Z",
        "inputs": {
            "market": {"kind": "lake_release", "reference": "synthetic", "sha256": "a" * 64}
        },
    }
    with closing(sqlite3.connect(runs)) as connection:
        header = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (original.generation_id,)
        ).fetchone()[0]
    assert json.loads(header) == expected
    assert (
        canonical_json(GenerationMetadata.model_validate(expected).model_dump(mode="json"))
        == header
    )
    fixed = ModelGenerationStore(runs).read_subjects(
        original.generation_id, subjects=("1234", "market")
    )
    raw = fixed.model_dump(mode="json")
    assert "subject_plan" not in raw
    assert ModelInputSlice.model_validate(raw).model_dump(mode="json") == raw


def test_shared_trend_payloads_survive_selection_and_operation_resume(tmp_path):
    import sqlite3
    from contextlib import closing

    from tests.trend_fixtures import BOJ, document, fixed

    from baibai_engine.machine_analysis.generation.contracts import (
        GeneratedModel,
        GenerationSubject,
    )
    from baibai_engine.machine_analysis.models.social_trends.calculations import (
        calculate_social_trends,
    )
    from baibai_engine.machine_analysis.observations.trends.parsers import extract_boj_decision

    runs = tmp_path / "runs.sqlite"
    old = seed_generation(runs, now=NOW, as_of=AS_OF)

    def stored_old_rows():
        with closing(sqlite3.connect(runs)) as connection:
            header = (
                connection.execute(
                    "SELECT header FROM model_generation WHERE generation_id=?",
                    (old.generation_id,),
                )
                .fetchone()[0]
                .encode("utf-8")
            )
            models = tuple(
                connection.execute(
                    "SELECT model_id,subject,calculation_revision,payload_gzip FROM generated_model "
                    "WHERE generation_id=? ORDER BY model_id,subject",
                    (old.generation_id,),
                ).fetchall()
            )
            return header, models

    # Compare the canonical bytes actually retained in SQLite, including compressed
    # payloads. Python dict insertion order before publication is not saved identity.
    old_bytes = stored_old_rows()
    old_decoded = ModelGenerationStore(runs).read(old.generation_id).model_dump_json()
    artifact = extract_boj_decision(document(BOJ, source="boj"), created_at=NOW)
    social = calculate_social_trends(fixed(artifact, cutoff=NOW), as_of=AS_OF)
    planned = tuple(model.model_id for model in old.models if model.subject == "1234")
    new = old.model_copy(
        update={
            "generation_id": "shared-trends",
            "evaluation_time": NOW,
            "schema_version": 2,
            "subject_plan": {
                "1234": GenerationSubject(scope="stock", model_ids=planned),
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
                "market:global": GenerationSubject(scope="market", model_ids=("social_trends",)),
            },
            "models": (
                *old.models,
                GeneratedModel(
                    model_id="social_trends",
                    subject="market:global",
                    calculation_revision="social-trends-v1",
                    payload=social,
                ),
            ),
        }
    )
    ModelGenerationStore(runs).publish(new)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="保存された政策主張を調べる", scope="specified_stocks", requested_tickers=("1234",)
        ),
        research_as_of=AS_OF,
        input_known_at=NOW,
        generation_id=new.generation_id,
        screening_selection_id=None,
        selection_summary="合成の予定と事実を区別する",
        targets=(selected_target(),),
    )
    workspace, db = tmp_path / "research", tmp_path / "app.sqlite"
    prepared = prepare_selected_workspace(
        draft=draft, db_path=db, workspace=workspace, runs_path=runs, now=NOW
    )
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    before = project_research_inputs(saved)
    shared = before["shared_subjects"]["market:global"]["models"]["social_trends"]
    assert shared["input_manifest"]["selected"] == [artifact.ref.model_dump(mode="json")]
    assert any(row["claim"]["kind"] == "announced_plan" for row in shared["observations"])
    assert stored_old_rows() == old_bytes
    assert ModelGenerationStore(runs).read(old.generation_id).model_dump_json() == old_decoded
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id, db_path=db, workspace=workspace, now=NOW
    )
    assert resumed.selection_sha256 == selection_sha256(saved)
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == before
