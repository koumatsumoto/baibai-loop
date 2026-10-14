"""一度の保存、同じ入力からの復旧、三入口から既存成果までのE2E。"""

import json
import socket
from datetime import datetime, timedelta

import pytest
import yaml
from tests.helpers.research_selection import draft_for, selected_target, selection
from tests.helpers.research_v4 import pair_payload

from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.operation.progress import selection_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.workspace import (
    ResearchWorkspaceConflictError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    selected_prepare,
)
from baibai_engine.research.workspace_cli import main

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
    authored = tmp_path / "workspace/1234/thesis-draft.yaml"
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
    from baibai_engine.research.selection import ResearchSelectionDraft

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
    from baibai_engine.research.selection import ResearchSelectionDraft

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
def test_selected_promote_caa_and_completion_use_current_scope(tmp_path, scope, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("Research called network"))
    value = selection(now=NOW, tickers=("1234",), scope=scope, runs=tmp_path / "runs")
    prepared = prepare(tmp_path, value=value)
    workspace = prepared.workspace
    assert not (workspace / "research-workspace.yaml").exists()
    directory = workspace / "1234"
    directory.mkdir()
    thesis, review = pair_payload(ticker="1234")
    (directory / "thesis-draft.yaml").write_text(yaml.safe_dump(thesis))
    (directory / "2026-09-07-1234-decision-review.yaml").write_text(yaml.safe_dump(review))
    before = (workspace / "domain-inputs.yaml").read_bytes()
    published = promote(
        workspace=workspace,
        ticker="1234",
        db_path=tmp_path / "app",
        thesis_id="thesis-selected",
        supersedes_id=None,
        now=NOW,
    )
    assert published.thesis_id == "thesis-selected"
    assert compute_status(workspace, db_path=tmp_path / "app", now=NOW)["request_scope"] == scope
    raw = scaffold_capital_allocation(
        db_path=tmp_path / "app",
        capital_allocation_assessment_id="capital-allocation-assessment-20260907-selected",
        operation_id=prepared.operation_id,
        as_of=NOW.date(),
        published_at=NOW,
    )
    raw.update(
        headline="合成見送り",
        comparison="合成比較",
        forgone="cash",
    )
    raw["alternatives"][0]["rationale"] = "合成比較の結論"
    raw["review"]["reviewer_identity"] = "independent"
    draft = CapitalAllocationAssessment.model_validate(raw)
    draft = draft.model_copy(
        update={
            "review": draft.review.model_copy(
                update={"draft_sha256": capital_allocation_draft_sha256(draft)}
            )
        }
    )
    assessment = CapitalAllocationAssessmentService(tmp_path / "app", clock=lambda: NOW).publish(
        draft
    )
    assert assessment.research_triage_id is None
    service = OperationService(tmp_path / "app")
    operation = service.get(prepared.operation_id)
    complete = service.complete(
        prepared.operation_id,
        operation.payload.model_copy(
            update={
                "artifacts": (
                    *operation.payload.artifacts,
                    {
                        "kind": "capital_allocation_assessment",
                        "ref": assessment.capital_allocation_assessment_id,
                    },
                ),
                "result": "proposal complete",
                "next": "human decision",
            }
        ),
        completed_at=NOW,
    )
    assert complete.payload.human_confirmation is None
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


def test_previous_analysis_day_current_research_and_formal_thesis_are_separate(tmp_path):
    from tests.helpers.screening_run import screening_run_payload, security_analysis

    from baibai_engine.research.inputs import project_research_inputs
    from baibai_engine.research.selection_query import load_analysis_snapshot
    from baibai_engine.screening.run_store import ScreeningRunStore

    runs = tmp_path / "runs"
    row = security_analysis("1234", name="合成会社", metrics={"er_annual": 0.13})
    payload = screening_run_payload(
        as_of="2026-09-06", run_at="2026-09-06T18:00:00+09:00", security_analyses=[row]
    )
    payload.pop("er_model_version")
    ScreeningRunStore(runs).publish_run(payload, run_revision_id="previous-analysis")
    analysis = load_analysis_snapshot(
        run_revision_id="previous-analysis",
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
    assert saved.inputs.analysis.analyses["1234"] == row
    projected = project_research_inputs(saved)
    assert projected["securities"]["1234"]["machine_valuation"]["as_of"] == "2026-09-06"
    directory = prepared.workspace / "1234"
    directory.mkdir()
    thesis, review = pair_payload(ticker="1234")
    (directory / "thesis-draft.yaml").write_text(yaml.safe_dump(thesis))
    (directory / "2026-09-07-1234-decision-review.yaml").write_text(yaml.safe_dump(review))
    assert (
        promote(
            workspace=prepared.workspace,
            ticker="1234",
            db_path=tmp_path / "app",
            thesis_id="current-formal",
            supersedes_id=None,
            now=NOW,
        ).thesis_id
        == "current-formal"
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
    from baibai_engine.research.selection_policy import ResearchCapacity

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


pytest_plugins = ["tests.engine.test_position_review_v3"]
