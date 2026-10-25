"""一度の保存、同じ入力からの復旧、三入口から既存成果までのE2E。"""

import json
import socket
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.model_generation import seed_generation
from tests.helpers.research_selection import historical_operation, selected_target, selection

from baibai_engine.investment_judgment.operation.progress import selection_binding
from baibai_engine.investment_judgment.operation.service import OperationService
from baibai_engine.investment_judgment.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.trade_proposal import TradeProposal, proposal_draft_sha256
from baibai_engine.investment_judgment.workspace import (
    ResearchWorkspaceConflictError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    scaffold_assessment,
    scaffold_review,
    selected_prepare,
)
from baibai_engine.investment_judgment.workspace_cli import main
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


def prepare(tmp_path, *, value=None, workspace=None, db=None, market=None, now=NOW):
    value = value or seed_generated_selection(runs=tmp_path / "runs", now=now, tickers=("1234",))
    return prepare_selected_workspace(
        draft=generated_draft_for(value),
        db_path=db or tmp_path / "app",
        workspace=workspace or tmp_path / "workspace",
        runs_path=tmp_path / "runs",
        now=now,
    )


def resume_historical(tmp_path, value):
    operation = historical_operation(value)
    seed_historical_operation(tmp_path / "app", operation)
    return resume_selected_workspace(
        operation_id=operation.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=NOW,
    )


def test_discover_new_preserves_selected_noncandidate_from_fixed_generation(tmp_path):
    from baibai_engine.investment_judgment.selection_query import load_generated_selection_context

    runs = tmp_path / "runs"
    value = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",))
    context = load_generated_selection_context(
        request=value.request,
        research_as_of=NOW.date(),
        generation_id=value.inputs.generation.generation_id,
        screening_selection_id=value.inputs.screening_selection_id,
        known_at=NOW,
        runs_path=runs,
        db_path=tmp_path / "app",
    )
    assert context["status"] == "ready"
    assert context["candidate_tickers"] == []
    original = runs.read_bytes()
    prepared = prepare(tmp_path, value=value)
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    assert prepared.status == "prepared"
    assert prepared.next_step == "investment-judgment status"
    assert saved.targets == value.targets
    assert saved.inputs.generation == value.inputs.generation
    assert saved.inputs.screening_selection == value.inputs.screening_selection
    assert runs.read_bytes() == original


@pytest.mark.parametrize(
    "unrecorded",
    [{}, {"earnings_forecast_source": None, "dividend_forecast_source": None}],
)
def test_forecast_sources_prepare_and_resume_from_fixed_analysis(tmp_path, monkeypatch, unrecorded):
    from tests.helpers.screening_run import screening_run_payload, seed_saved_run, stock_analysis

    from baibai_engine.investment_judgment.inputs import project_research_inputs
    from baibai_engine.machine_analysis.company_valuation.query import (
        machine_valuation_from_analysis,
    )
    from baibai_engine.machine_analysis.screening.discovery.candidates import (
        build_candidate_contexts,
    )
    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader

    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("Research used network"))
    # Saved JSON is the input; expectations below are independent of the projection helper.
    earnings = """{
        "status": "partially_unresolved", "disclosure_number": "income-unknown",
        "disclosed_at": "2026-09-06", "disclosed_time": null,
        "document_type": "EarnForecastRevision", "period_start": "2026-04-01",
        "period_end": "2027-03-31", "basis": "consolidated", "target_basis": "consolidated",
        "candidate_disclosure_numbers": "income-known,income-unknown",
        "forecast_eps_source_records": "income-known,income-unknown",
        "forecast_profit_source_records": null,
        "forecast_ordinary_profit_source_records": "income-known"
    }"""
    dividend = """{
        "status": "confirmed", "disclosure_number": "dividend",
        "disclosed_at": "2026-09-07", "disclosed_time": "15:00:00",
        "document_type": "DividendForecastRevision", "period_start": "2027-04-01",
        "period_end": "2028-03-31", "basis": "issuer_dividend", "target_basis": "issuer_dividend",
        "dps_forecast_annual_source_records": "dividend"
    }"""
    numbers = {
        "per_forward_sector_gap": -0.2,
        "market_price_yen": 1000.0,
        "er_annual": 0.05,
        "er_reversion_annual": 0.01,
        "er_carry_annual": 0.04,
        "dividend_yield": 0.04,
        "dps_actual_annual": 40.0,
        "dps_forecast_annual": 170.0,
        "dividend_basis": "actual_reported",
    }
    baseline = stock_analysis("1234", per_forward=10.0, metrics=numbers)
    row = stock_analysis(
        "1234",
        per_forward=10.0,
        metrics={
            **numbers,
            "earnings_forecast_source": earnings,
            "dividend_forecast_source": dividend,
        },
    )
    rules = load_screening_rules()
    candidate_options = {
        "rules": rules.candidate_discovery,
        "required_jpx_flags": rules.universe.required_jpx_flags,
    }
    assert build_candidate_contexts([row], **candidate_options) == build_candidate_contexts(
        [baseline], **candidate_options
    )
    runs = tmp_path / "runs"
    value = selection(
        now=NOW,
        tickers=("1234", "5678"),
        runs=runs,
        rows=[row, stock_analysis("5678", metrics=unrecorded)],
    )
    stored = ScreeningRunReader(runs).get_run(value.inputs.analysis.run_revision_id)
    machine = machine_valuation_from_analysis(
        next(row for row in stored.stock_analyses if row["ticker"] == "1234")
    )
    assert machine is not None
    original_run = runs.read_bytes()
    prepared = resume_historical(tmp_path, value)
    assert runs.read_bytes() == original_run
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    assert saved is not None
    fixed_snapshot = saved.model_dump_json()
    inputs_path = prepared.workspace / "domain-inputs.yaml"
    first = inputs_path.read_bytes()
    inputs = yaml.safe_load(first)
    security = inputs["securities"]["1234"]
    assert security["forecast_sources"] == {
        "earnings_forecast_source": {
            "status": "partially_unresolved",
            "disclosure_number": "income-unknown",
            "disclosed_at": "2026-09-06",
            "disclosed_time": None,
            "document_type": "EarnForecastRevision",
            "period_start": "2026-04-01",
            "period_end": "2027-03-31",
            "basis": "consolidated",
            "target_basis": "consolidated",
            "candidate_disclosure_numbers": "income-known,income-unknown",
            "forecast_eps_source_records": "income-known,income-unknown",
            "forecast_profit_source_records": None,
            "forecast_ordinary_profit_source_records": "income-known",
        },
        "dividend_forecast_source": {
            "status": "confirmed",
            "disclosure_number": "dividend",
            "disclosed_at": "2026-09-07",
            "disclosed_time": "15:00:00",
            "document_type": "DividendForecastRevision",
            "period_start": "2027-04-01",
            "period_end": "2028-03-31",
            "basis": "issuer_dividend",
            "target_basis": "issuer_dividend",
            "dps_forecast_annual_source_records": "dividend",
        },
    }
    assert security["machine_valuation"]["run_revision_id"] == "run-synthetic"
    estimate = security["machine_valuation"]["estimate"]
    assert estimate == asdict(machine)
    estimate_keys = {
        "er_reversion_annual": "reversion_annual",
        "er_carry_annual": "carry_annual",
    }
    for key, expected in numbers.items():
        if key != "per_forward_sector_gap":
            assert estimate[estimate_keys.get(key, key)] == expected
    assert inputs["securities"]["5678"]["forecast_sources"] == {
        "earnings_forecast_source": None,
        "dividend_forecast_source": None,
    }
    assert saved.inputs.analysis.analyses["5678"]["metrics"] == unrecorded
    assert saved.inputs.analysis.analyses["1234"]["metrics"]["earnings_forecast_source"] == earnings
    assert saved.inputs.analysis.analyses["1234"]["metrics"]["dividend_forecast_source"] == dividend

    def forbidden(*_a, **_kw):
        pytest.fail("fixed forecast source projection reread a store")

    with monkeypatch.context() as projection_only:
        projection_only.setattr(sqlite3, "connect", forbidden)
        assert project_research_inputs(saved) == inputs
    monkeypatch.setattr(selected_prepare, "read_model_inputs", forbidden)
    seed_saved_run(
        runs,
        screening_run_payload(
            as_of="2026-09-08",
            stock_analyses=[
                stock_analysis(
                    "1234",
                    metrics={"er_annual": 0.99, "earnings_forecast_source": '{"status":"changed"}'},
                )
            ],
        ),
        run_revision_id="replacement",
    )
    for remove_run in (False, True):
        if remove_run:
            runs.unlink()
        inputs_path.unlink()
        resumed = resume_selected_workspace(
            operation_id=prepared.operation_id,
            db_path=tmp_path / "app",
            workspace=prepared.workspace,
            now=NOW + timedelta(days=2),
            force=True,
        )
        assert resumed.selection_sha256 == prepared.selection_sha256
        assert inputs_path.read_bytes() == first
        reread = selection_binding(
            OperationService(tmp_path / "app").get(prepared.operation_id).payload
        )
        assert reread is not None
        assert reread.model_dump_json() == fixed_snapshot
    assert saved.model_dump_json() == fixed_snapshot


def test_commit_file_failure_recovers_same_operation_without_reading_source(tmp_path, monkeypatch):
    value = seed_generated_selection(runs=tmp_path / "runs", now=NOW, tickers=("1234",))
    writer = selected_prepare._write_workspace_file
    monkeypatch.setattr(
        selected_prepare,
        "_write_workspace_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("synthetic file failure")),
    )
    with pytest.raises(ResearchWorkspaceConflictError, match=r"research-op-.*is saved"):
        prepare(tmp_path, value=value)
    saved = OperationService(tmp_path / "app").active()
    assert saved is not None
    snapshot = selection_binding(saved.payload)
    (tmp_path / "runs").unlink()
    monkeypatch.setattr(selected_prepare, "_write_workspace_file", writer)
    monkeypatch.setattr(
        selected_prepare,
        "read_model_inputs",
        lambda *_a, **_kw: pytest.fail("resume reread source"),
    )
    repaired = prepare(tmp_path, value=value)
    assert repaired.status == "resumed"
    assert repaired.operation_id == saved.operation_id
    first = (tmp_path / "workspace/domain-inputs.yaml").read_bytes()
    authored = tmp_path / "workspace/1234/assessment-draft.yaml"
    authored.parent.mkdir()
    authored.write_text("authored draft remains")
    (tmp_path / "workspace/domain-inputs.yaml").unlink()
    resumed = resume_selected_workspace(
        operation_id=saved.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=NOW + timedelta(days=1),
        force=True,
    )
    assert resumed.operation_id == saved.operation_id
    assert (tmp_path / "workspace/domain-inputs.yaml").read_bytes() == first
    assert authored.read_text() == "authored draft remains"
    assert (
        selection_binding(OperationService(tmp_path / "app").get(saved.operation_id).payload)
        == snapshot
    )


def test_required_read_failure_and_over_capacity_do_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        selected_prepare,
        "read_model_inputs",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("required read failed")),
    )
    with pytest.raises(OSError, match="required read"):
        prepare(tmp_path)
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "workspace").exists()
    raw = generated_draft_for(
        seed_generated_selection(runs=tmp_path / "runs", now=NOW, tickers=("1000", "1001", "1002"))
    ).model_dump(mode="json")
    raw["targets"].append(selected_target("1003").model_dump(mode="json"))
    from baibai_engine.investment_judgment.selection import GeneratedResearchSelectionDraft

    with pytest.raises(ResearchWorkspaceConflictError, match="capacity"):
        prepare_selected_workspace(
            draft=GeneratedResearchSelectionDraft.model_validate(raw),
            db_path=tmp_path / "app",
            workspace=tmp_path / "workspace",
            runs_path=tmp_path / "absent",
            now=NOW,
        )
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize(
    "change", ["scope", "target", "generation", "policy", "cutoff", "summary", "reason"]
)
def test_active_binding_cannot_be_changed_with_force(tmp_path, change):
    value = seed_generated_selection(runs=tmp_path / "runs", now=NOW, tickers=("1234",))
    prepared = prepare(tmp_path, value=value)
    raw = generated_draft_for(value).model_dump(mode="json")
    if change == "scope":
        raw["request"].update(scope="specified_stocks", requested_tickers=["1234"])
    if change == "target":
        raw["targets"][0]["ticker"] = "5678"
    if change == "generation":
        raw["generation_id"] = "other"
    if change == "policy":
        raw["screening_selection_id"] = "other"
    if change == "cutoff":
        raw["input_known_at"] = (NOW - timedelta(seconds=1)).isoformat()
    if change == "summary":
        raw["selection_summary"] = "new summary"
    if change == "reason":
        raw["targets"][0]["rationale"] = "new rationale"
    from baibai_engine.investment_judgment.selection import GeneratedResearchSelectionDraft

    with pytest.raises(ResearchWorkspaceConflictError, match="fixed selection"):
        prepare_selected_workspace(
            draft=GeneratedResearchSelectionDraft.model_validate(raw),
            db_path=tmp_path / "app",
            workspace=prepared.workspace,
            runs_path=tmp_path / "absent",
            now=NOW,
            force=True,
        )


def test_completed_retry_and_real_same_text_request_have_different_identities(tmp_path):
    value = seed_generated_selection(runs=tmp_path / "runs", now=NOW)
    first = prepare(tmp_path, value=value)
    service = OperationService(tmp_path / "app")
    operation = service.get(first.operation_id)
    service.complete(
        first.operation_id,
        operation.payload.model_copy(
            update={"result": "no targets with reason", "next": "new request"}
        ),
        completed_at=NOW,
    )
    assert prepare(tmp_path, value=value).status == "already_completed"
    second = prepare(tmp_path, value=value, workspace=tmp_path / "new-workspace")
    assert second.operation_id != first.operation_id
    assert second.status == "prepared"
    with pytest.raises(ResearchWorkspaceConflictError, match="another Operation"):
        resume_selected_workspace(
            operation_id=second.operation_id,
            db_path=tmp_path / "app",
            workspace=first.workspace,
            now=NOW,
            force=True,
        )


@pytest.mark.parametrize("scope", ["discover_new", "specified_stocks"])
@pytest.mark.parametrize("generated", [False, True], ids=["legacy", "five_models"])
def test_selected_promote_proposal_and_completion_use_current_scope(
    tmp_path, scope, generated, monkeypatch, capsys
):
    from tests.helpers.db_seed import seed_ledger

    from baibai_engine.investment_judgment.operation.cli import main as operation_main
    from baibai_engine.position.ledger import PortfolioLedgerDocument

    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("Research called network"))
    seed_ledger(
        tmp_path / "app",
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": NOW.isoformat(),
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic-opening",
                        "occurred_at": (NOW - timedelta(hours=1)).isoformat(),
                        "amount_yen": 300000,
                    }
                ],
            }
        ),
    )
    if generated:
        generation = seed_generation(tmp_path / "runs", now=NOW, as_of=NOW.date())
        prepared = prepare_selected_workspace(
            draft=GeneratedResearchSelectionDraft(
                schema_version=2,
                request=ResearchRequest(
                    text="合成例：投資提案して",
                    scope=scope,
                    requested_tickers=("1234",) if scope == "specified_stocks" else (),
                ),
                research_as_of=NOW.date(),
                input_known_at=NOW,
                generation_id=generation.generation_id,
                screening_selection_id="policy-1",
                selection_summary="合成五モデルから判断経路を検証する",
                targets=(selected_target(),),
            ),
            db_path=tmp_path / "app",
            workspace=tmp_path / "workspace",
            runs_path=tmp_path / "runs",
            now=NOW,
        )
        # The entire judgment path must work after the rebuildable cache disappears.
        (tmp_path / "runs").unlink()
    else:
        value = selection(now=NOW, tickers=("1234",), scope=scope, runs=tmp_path / "runs")
        prepared = resume_historical(tmp_path, value)
    saved_selection = selection_binding(
        OperationService(tmp_path / "app").get(prepared.operation_id).payload
    )
    assert saved_selection.schema_version == (3 if generated else 2)
    fixed_hash = selection_sha256(saved_selection)
    workspace = prepared.workspace
    assert not (workspace / "research-workspace.yaml").exists()
    scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=tmp_path / "absent-market",
        as_of=NOW.date(),
        retrieved_at=NOW,
        db_path=tmp_path / "app",
    )
    directory = workspace / "1234"
    assessment, review = pair_payload(ticker="1234", as_of="2026-09-07")
    (directory / "assessment-draft.yaml").write_text(yaml.safe_dump(assessment))
    scaffold_review(workspace=workspace, ticker="1234", db_path=tmp_path / "app")
    (directory / "assessment-review.yaml").write_text(yaml.safe_dump(review))
    (directory / "assessment-publication.yaml").write_text(
        yaml.safe_dump(
            {
                "assessment_id": assessment_id(),
                "supersedes_id": None,
            }
        )
    )
    before = (workspace / "domain-inputs.yaml").read_bytes()
    published = promote(
        workspace=workspace,
        ticker="1234",
        db_path=tmp_path / "app",
        assessment_id=assessment_id(),
        supersedes_id=None,
        now=NOW,
    )
    assert published.assessment_id == assessment_id()
    assert compute_status(workspace, db_path=tmp_path / "app", now=NOW)["request_scope"] == scope
    proposal_id = "investment-proposal-00000000-0000-4000-8000-000000000003"
    common_args = [
        "--db",
        str(tmp_path / "app"),
        "--sqlite-path",
        str(tmp_path / "absent-market"),
    ]
    assert (
        main(
            ["trade-proposal-context", "--operation-id", prepared.operation_id, *common_args],
            now=NOW,
        )
        == 0
    )
    context = json.loads(capsys.readouterr().out)
    assert context["research_scope"]["operation_id"] == prepared.operation_id
    draft_path = tmp_path / "proposal-draft.yaml"
    assert (
        main(
            [
                "trade-proposal-scaffold",
                "--operation-id",
                prepared.operation_id,
                "--proposal-id",
                proposal_id,
                "--out",
                str(draft_path),
                *common_args,
            ],
            now=NOW,
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["proposal_id"] == proposal_id
    raw = yaml.safe_load(draft_path.read_text())
    raw.update(headline="合成見送り", comparison="合成比較", forgone="cash")
    raw["items"][0]["action"] = "no_action"
    raw["items"][0].pop("blockers")
    raw["items"][0].pop("resume_conditions")
    raw["items"][0]["rationale"] = "合成比較の経済的見送り"
    raw["review"]["reviewer_identity"] = "independent"
    raw["review"]["reviewed_at"] = NOW.isoformat()
    draft_path.write_text(yaml.safe_dump(raw))
    unchanged = draft_path.read_bytes(), (workspace / "domain-inputs.yaml").read_bytes()
    assert main(["trade-proposal-check", str(draft_path), *common_args], now=NOW) == 0
    checked = yaml.safe_load(capsys.readouterr().out)
    assert checked["valid"] is True
    assert checked["review_binding"] == "stale"
    assert unchanged == (draft_path.read_bytes(), (workspace / "domain-inputs.yaml").read_bytes())
    draft = TradeProposal.model_validate(raw)
    draft = draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={
                    "draft_sha256": proposal_draft_sha256(draft),
                }
            )
        }
    )
    draft_path.write_text(yaml.safe_dump(draft.model_dump(mode="json")))
    assert main(["trade-proposal-publish", str(draft_path), *common_args], now=NOW) == 0
    proposal = TradeProposal.model_validate(yaml.safe_load(capsys.readouterr().out))
    assert proposal.request_scope == scope
    service = OperationService(tmp_path / "app")
    assert (
        operation_main(
            [
                "--db",
                str(tmp_path / "app"),
                "--format",
                "json",
                "record-proposal",
                prepared.operation_id,
                "--proposal-id",
                proposal.proposal_id,
            ],
            now=NOW,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["operation_id"] == prepared.operation_id
    operation = service.get(prepared.operation_id)
    complete = service.complete(
        prepared.operation_id,
        operation.payload.model_copy(
            update={
                "artifacts": (
                    *operation.payload.artifacts,
                    {
                        "kind": "investment_proposal",
                        "ref": proposal.proposal_id,
                    },
                ),
                "result": "proposal complete",
                "next": "human decision",
            }
        ),
        completed_at=NOW,
    )
    assert complete.status == "completed"
    assert service.get(prepared.operation_id) == complete
    assert "human_confirmation" not in complete.payload.model_dump()
    assert (
        main(["trade-proposal-preview", "--proposal-id", proposal_id, *common_args], now=NOW) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["saved_proposal_id"] == proposal_id
    assert preview["status"] == "unchanged"
    assert preview["items"][0]["saved_terms"] is None
    assert before == (workspace / "domain-inputs.yaml").read_bytes()
    assert selection_sha256(selection_binding(complete.payload)) == fixed_hash
    if generated:
        assert proposal.liquidity_context[0].generation_id == "fixed-synthetic"
        assert proposal.liquidity_context[0].adv_yen == 125000000


def test_cli_inputs_required_and_old_flags_are_removed(tmp_path, capsys):
    result = main(
        [
            "selection-context",
            "--request",
            "投資提案して",
            "--scope",
            "discover_new",
            "--runs-path",
            str(tmp_path / "absent"),
            "--db",
            str(tmp_path / "absent-app"),
        ],
        now=NOW,
    )
    assert result == 3
    assert json.loads(capsys.readouterr().out)["status"] == "inputs_required"
    with pytest.raises(SystemExit):
        main(["prepare", "--request", "old", "--workspace", str(tmp_path / "workspace")], now=NOW)
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("command", ["status", "trade-proposal-check", "trade-proposal-publish"])
def test_malformed_yaml_is_a_cli_data_error_without_writes(tmp_path, capsys, command):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    malformed = workspace / "manifest.yaml"
    malformed.write_text("field: [\n")
    db = tmp_path / "absent-app"
    source = malformed.read_bytes()
    arguments = (
        ["status", "--workspace", str(workspace)]
        if command == "status"
        else [command, str(malformed)]
    )
    assert main([*arguments, "--db", str(db)], now=NOW) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "invalid_source"
    assert result["reason"]
    assert malformed.read_bytes() == source
    assert not db.exists()


def test_previous_analysis_day_current_research_and_formal_assessment_are_separate(tmp_path):
    from tests.helpers.screening_run import screening_run_payload, seed_saved_run, stock_analysis

    from baibai_engine.investment_judgment.inputs import project_research_inputs
    from baibai_engine.investment_judgment.selection import AnalysisInputSnapshot
    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader

    runs = tmp_path / "runs"
    row = stock_analysis("1234", name="合成会社", metrics={"er_annual": 0.13})
    payload = screening_run_payload(
        as_of="2026-09-06", run_at="2026-09-06T18:00:00+09:00", stock_analyses=[row]
    )
    payload.pop("er_model_version")
    seed_saved_run(runs, payload, run_revision_id="run-revision-" + "a" * 32)
    run = ScreeningRunReader(runs).get_run("run-revision-" + "a" * 32)
    analysis = AnalysisInputSnapshot(
        run_revision_id=run.run_revision_id,
        analysis_as_of=datetime.fromisoformat(run.as_of_date).date(),
        run_at=datetime.fromisoformat(run.run_at),
        screening_rules_hash=run.payload.get("screening_rules_hash"),
        er_model_version=run.payload.get("er_model_version"),
        analyses={row["ticker"]: row for row in run.stock_analyses},
    )
    value = selection(now=NOW, tickers=("1234",))
    value = value.model_copy(
        update={"inputs": value.inputs.model_copy(update={"analysis": analysis, "input_gaps": ()})}
    )
    prepared = resume_historical(tmp_path, value)
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    assert saved.research_as_of.isoformat() == "2026-09-07"
    assert saved.inputs.analysis.analysis_as_of.isoformat() == "2026-09-06"
    assert saved.inputs.analysis.er_model_version is None
    canonical_row = dict(saved.inputs.analysis.analyses["1234"])
    candidate = canonical_row.pop("candidate")
    assert candidate["reasons"] == []
    assert canonical_row == row
    projected = project_research_inputs(saved)
    assert projected["securities"]["1234"]["machine_valuation"]["as_of"] == "2026-09-06"
    assert projected["analysis_source"] == {
        "run_revision_id": "run-revision-" + "a" * 32,
        "analysis_as_of": "2026-09-06",
        "run_at": "2026-09-06T18:00:00+09:00",
        "screening_rules_hash": analysis.screening_rules_hash,
        "er_model_version": None,
    }
    directory = prepared.workspace / "1234"
    directory.mkdir()
    assessment, review = pair_payload(ticker="1234", as_of="2026-09-07")
    (directory / "assessment-draft.yaml").write_text(yaml.safe_dump(assessment))
    (directory / "assessment-review.yaml").write_text(yaml.safe_dump(review))
    (directory / "assessment-publication.yaml").write_text(
        yaml.safe_dump(
            {
                "assessment_id": assessment_id(),
                "supersedes_id": None,
            }
        )
    )
    assert (
        promote(
            workspace=prepared.workspace,
            ticker="1234",
            db_path=tmp_path / "app",
            assessment_id=assessment_id(),
            supersedes_id=None,
            now=NOW,
        ).assessment_id
        == assessment_id()
    )
    assert OperationService(tmp_path / "app").get(prepared.operation_id).as_of == NOW.date()


def test_held_new_is_a_conflict_before_input_collection_and_cli_exit4(
    tmp_path, holding_case, monkeypatch, capsys
):
    db, _market, _ = holding_case
    value = seed_generated_selection(runs=tmp_path / "runs", now=NOW, tickers=("2331",))
    monkeypatch.setattr(
        selected_prepare,
        "read_model_inputs",
        lambda *_a, **_kw: pytest.fail("purpose conflict read inputs"),
    )
    file = tmp_path / "selection.json"
    file.write_text(generated_draft_for(value).model_dump_json())
    assert (
        main(
            [
                "prepare",
                "--selection-file",
                str(file),
                "--workspace",
                str(tmp_path / "workspace"),
                "--db",
                str(db),
                "--runs-path",
                str(tmp_path / "runs"),
            ],
            now=NOW,
        )
        == 4
    )
    assert "purpose_conflict" in capsys.readouterr().err
    assert OperationService(db).active() is None
    assert not (tmp_path / "workspace").exists()


def test_saved_capacity_controls_resume_after_current_policy_changes(tmp_path, monkeypatch):
    from baibai_engine.investment_judgment.selection_policy import ResearchCapacity

    value = seed_generated_selection(
        runs=tmp_path / "runs", now=NOW, tickers=("1000", "1001", "1002")
    )
    prepared = prepare(tmp_path, value=value)
    monkeypatch.setattr(
        selected_prepare, "CURRENT_RESEARCH_CAPACITY", ResearchCapacity(max_deep_research_targets=1)
    )
    resumed = prepare(tmp_path, value=value)
    assert resumed.operation_id == prepared.operation_id
    assert resumed.selected_count == 3


def test_tampered_projection_and_manifest_are_rejected_and_force_keeps_authored_files(tmp_path):
    prepared = prepare(tmp_path)
    file = prepared.workspace / "domain-inputs.yaml"
    original = file.read_bytes()
    file.write_text("modified: true\n")
    with pytest.raises(ResearchWorkspaceConflictError, match="fixed snapshot"):
        compute_status(prepared.workspace, db_path=tmp_path / "app", now=NOW)
    resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=tmp_path / "app",
        workspace=prepared.workspace,
        now=NOW,
        force=True,
    )
    assert file.read_bytes() == original
    manifest = prepared.workspace / "manifest.yaml"
    raw = yaml.safe_load(manifest.read_text())
    raw["purpose"] = "position_review"
    manifest.write_text(yaml.safe_dump(raw))
    with pytest.raises(ResearchWorkspaceConflictError, match="purpose"):
        resume_selected_workspace(
            operation_id=prepared.operation_id,
            db_path=tmp_path / "app",
            workspace=prepared.workspace,
            now=NOW,
            force=True,
        )


@pytest.fixture
def holding_case(tmp_path):
    from tests.helpers.db_seed import seed_ledger
    from tests.helpers.ledger import load_portfolio_ledger

    db = tmp_path / "holding-app"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger)
    return db, tmp_path / "absent-market", ledger


def test_legacy_draft_cli_cannot_start_new_operation(tmp_path, capsys):
    source = tmp_path / "legacy-draft.json"
    historical = selection(now=NOW, tickers=("1234",))
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request": historical.request.model_dump(mode="json"),
                "research_as_of": historical.research_as_of.isoformat(),
                "input_known_at": historical.input_known_at.isoformat(),
                "run_revision_id": None,
                "selection_summary": historical.selection_summary,
                "targets": [target.model_dump(mode="json") for target in historical.targets],
                "input_gaps": [gap.model_dump(mode="json") for gap in historical.inputs.input_gaps],
            }
        )
    )
    assert (
        main(
            [
                "prepare",
                "--selection-file",
                str(source),
                "--workspace",
                str(tmp_path / "workspace"),
                "--db",
                str(tmp_path / "app"),
                "--runs-path",
                str(tmp_path / "absent-runs"),
            ],
            now=NOW,
        )
        == 4
    )
    assert capsys.readouterr().err
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "workspace").exists()
    assert not (tmp_path / "absent-runs").exists()


def test_historical_capacity_and_hash_survive_current_policy_change(tmp_path, monkeypatch):
    from baibai_engine.investment_judgment.selection_policy import ResearchCapacity

    saved = selection(now=NOW, tickers=("1000", "1001", "1002"))
    operation = historical_operation(saved)
    seed_historical_operation(tmp_path / "app", operation)
    monkeypatch.setattr(
        selected_prepare, "CURRENT_RESEARCH_CAPACITY", ResearchCapacity(max_deep_research_targets=1)
    )
    resumed = resume_selected_workspace(
        operation_id=operation.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=NOW,
    )
    assert resumed.selected_count == 3
    assert resumed.selection_sha256 == selection_sha256(saved)
    assert (
        selection_binding(OperationService(tmp_path / "app").get(operation.operation_id).payload)
        == saved
    )
