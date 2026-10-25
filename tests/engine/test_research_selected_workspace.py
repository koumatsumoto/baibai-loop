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
from tests.helpers.research_selection import draft_for, selected_target, selection

from baibai_engine.investment_judgment.operation.progress import selection_binding
from baibai_engine.investment_judgment.operation.service import OperationService
from baibai_engine.investment_judgment.trade_proposal import TradeProposal, proposal_draft_sha256
from baibai_engine.investment_judgment.workspace import (
    ResearchWorkspaceConflictError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    selected_prepare,
)
from baibai_engine.investment_judgment.workspace_cli import main
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


def prepare(tmp_path, *, value=None, workspace=None, db=None, market=None, now=NOW):
    value = value or selection(now=now, tickers=("1234",))
    return prepare_selected_workspace(
        draft=draft_for(value),
        db_path=db or tmp_path / "app",
        workspace=workspace or tmp_path / "workspace",
        sqlite_path=market or tmp_path / "absent-market",
        runs_path=tmp_path / "runs",
        indicators_path=tmp_path / "absent-indicators",
        now=now,
    )


def test_discover_new_preserves_selected_noncandidate_from_fixed_run(tmp_path):
    from tests.helpers.screening_run import stock_analysis

    from baibai_engine.investment_judgment.selection import ResearchRequest
    from baibai_engine.investment_judgment.selection_query import load_selection_context

    runs = tmp_path / "runs"
    value = selection(
        now=NOW,
        tickers=("1234",),
        runs=runs,
        rows=[stock_analysis("1234", name="合成会社")],
    )
    row = value.inputs.analysis.analyses["1234"]
    assert row["candidate"]["common_eligible"] is True
    assert row["candidate"]["reasons"] == []
    context = load_selection_context(
        request=ResearchRequest(text="合成例：投資提案して", scope="discover_new"),
        research_as_of=NOW.date(),
        run_revision_id="run-synthetic",
        known_at=NOW,
        runs_path=runs,
        db_path=tmp_path / "app",
    )
    assert context["status"] == "ready"
    assert context["candidates"] == []
    assert context["run_revision_id"] == "run-synthetic"
    original_run = runs.read_bytes()

    prepared = prepare(tmp_path, value=value)
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    assert prepared.status == "prepared"
    assert prepared.next_step == "investment-judgment status"
    assert saved is not None
    assert saved.request.scope == "discover_new"
    assert saved.request.requested_tickers == ()
    assert saved.inputs.analysis.run_revision_id == context["run_revision_id"]
    assert saved.inputs.analysis.analyses["1234"] == row
    assert runs.read_bytes() == original_run


@pytest.mark.parametrize(
    "unrecorded",
    [{}, {"earnings_forecast_source": None, "dividend_forecast_source": None}],
)
def test_forecast_sources_prepare_and_resume_from_fixed_analysis(tmp_path, monkeypatch, unrecorded):
    from tests.helpers.screening_run import screening_run_payload, stock_analysis

    from baibai_engine.investment_judgment.inputs import project_research_inputs
    from baibai_engine.machine_analysis.screening.discovery.candidates import (
        build_candidate_contexts,
    )
    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunStore
    from baibai_engine.read_api import stored_machine_valuation

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
    machine = stored_machine_valuation(runs, ticker="1234", as_of=NOW.date()).valuation
    assert machine is not None
    original_run = runs.read_bytes()
    prepared = prepare(tmp_path, value=value)
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
    monkeypatch.setattr(selected_prepare, "collect_research_inputs", forbidden)
    ScreeningRunStore(runs).publish_run(
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
        rules=rules,
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
    value = selection(now=NOW, tickers=("1234",), runs=tmp_path / "runs")
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
        "collect_research_inputs",
        lambda **_kw: pytest.fail("resume reread source"),
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
        "collect_research_inputs",
        lambda **_kw: (_ for _ in ()).throw(OSError("required read failed")),
    )
    with pytest.raises(OSError, match="required read"):
        prepare(tmp_path)
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "workspace").exists()
    raw = draft_for(selection(now=NOW, tickers=("1000", "1001", "1002"))).model_dump(mode="json")
    raw["targets"].append(selected_target("1003").model_dump(mode="json"))
    from baibai_engine.investment_judgment.selection import ResearchSelectionDraft

    with pytest.raises(ResearchWorkspaceConflictError, match="capacity"):
        prepare_selected_workspace(
            draft=ResearchSelectionDraft.model_validate(raw),
            db_path=tmp_path / "app",
            workspace=tmp_path / "workspace",
            sqlite_path=tmp_path / "absent",
            runs_path=tmp_path / "absent",
            indicators_path=tmp_path / "absent",
            now=NOW,
        )
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("change", ["scope", "target", "source", "cutoff", "summary", "reason"])
def test_active_binding_cannot_be_changed_with_force(tmp_path, change):
    value = selection(now=NOW, tickers=("1234",))
    prepared = prepare(tmp_path, value=value)
    raw = draft_for(value).model_dump(mode="json")
    if change == "scope":
        raw["request"].update(scope="specified_stocks", requested_tickers=["1234"])
    if change == "target":
        raw["targets"][0]["ticker"] = "5678"
        raw["input_gaps"][0]["ticker"] = "5678"
    if change == "source":
        raw["run_revision_id"] = "other"
    if change == "cutoff":
        raw["input_known_at"] = (NOW - timedelta(seconds=1)).isoformat()
    if change == "summary":
        raw["selection_summary"] = "new summary"
    if change == "reason":
        raw["targets"][0]["rationale"] = "new rationale"
    from baibai_engine.investment_judgment.selection import ResearchSelectionDraft

    with pytest.raises(ResearchWorkspaceConflictError, match="fixed selection"):
        prepare_selected_workspace(
            draft=ResearchSelectionDraft.model_validate(raw),
            db_path=tmp_path / "app",
            workspace=prepared.workspace,
            sqlite_path=tmp_path / "absent",
            runs_path=tmp_path / "absent",
            indicators_path=tmp_path / "absent",
            now=NOW,
            force=True,
        )


def test_completed_retry_and_real_same_text_request_have_different_identities(tmp_path):
    value = selection(now=NOW)
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
def test_selected_promote_proposal_and_completion_use_current_scope(
    tmp_path, scope, monkeypatch, capsys
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
    value = selection(now=NOW, tickers=("1234",), scope=scope, runs=tmp_path / "runs")
    prepared = prepare(tmp_path, value=value)
    workspace = prepared.workspace
    assert not (workspace / "research-workspace.yaml").exists()
    directory = workspace / "1234"
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
    assert "human_confirmation" not in complete.payload.model_dump()
    assert (
        main(["trade-proposal-preview", "--proposal-id", proposal_id, *common_args], now=NOW) == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["saved_proposal_id"] == proposal_id
    assert preview["status"] == "unchanged"
    assert preview["items"][0]["saved_terms"] is None
    assert before == (workspace / "domain-inputs.yaml").read_bytes()


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
    from tests.helpers.screening_run import screening_run_payload, stock_analysis

    from baibai_engine.investment_judgment.inputs import project_research_inputs
    from baibai_engine.investment_judgment.selection_query import load_analysis_snapshot
    from baibai_engine.machine_analysis.screening.run_store import ScreeningRunStore

    runs = tmp_path / "runs"
    row = stock_analysis("1234", name="合成会社", metrics={"er_annual": 0.13})
    payload = screening_run_payload(
        as_of="2026-09-06", run_at="2026-09-06T18:00:00+09:00", stock_analyses=[row]
    )
    payload.pop("er_model_version")
    ScreeningRunStore(runs).publish_run(
        payload, run_revision_id="run-revision-" + "a" * 32, rules=load_screening_rules()
    )
    analysis = load_analysis_snapshot(
        run_revision_id="run-revision-" + "a" * 32,
        research_as_of=NOW.date(),
        tickers=("1234",),
        gaps=(),
        known_at=NOW,
        runs_path=runs,
    )
    value = selection(now=NOW, tickers=("1234",))
    value = value.model_copy(
        update={"inputs": value.inputs.model_copy(update={"analysis": analysis, "input_gaps": ()})}
    )
    prepared = prepare(tmp_path, value=value)
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
    db, market, _ = holding_case
    value = selection(now=NOW, tickers=("2331",))
    monkeypatch.setattr(
        selected_prepare,
        "collect_research_inputs",
        lambda **_kw: pytest.fail("purpose conflict read inputs"),
    )
    file = tmp_path / "selection.json"
    file.write_text(draft_for(value).model_dump_json())
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
                "--sqlite-path",
                str(market),
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

    value = selection(now=NOW, tickers=("1000", "1001", "1002"))
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
