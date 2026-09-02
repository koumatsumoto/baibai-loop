from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

import baibai_batch.analysis.cli as analysis_cli
from baibai_batch.analysis.cli import main as analysis_main
from baibai_batch.analysis.io import (
    ensure_private_dir,
    read_json,
    redact,
    redact_argv,
    write_json_atomic,
    write_log,
)
from baibai_batch.analysis.models import AIResultEnvelope, DailyManifest
from baibai_batch.analysis.packet import (
    _bind_operation,
    check_results,
    prepare_packet,
    validate_packet,
)
from baibai_batch.analysis.workspace import (
    PipelineLock,
    StepLogger,
    Workspace,
    create_workspace,
    repository_fingerprint,
)
from baibai_batch.jobs.daily import CommandResult, StepResult


def _workspace(
    tmp_path: Path, *, review_set_id: str = "review-set-a", run_id: str = "run-a"
) -> Workspace:
    state = ensure_private_dir(tmp_path / "state")
    path = ensure_private_dir(state / f"runs/analysis/2026-09-01/{run_id}", root=state)
    for relative in (
        "inputs",
        "steps",
        "packet/shared",
        "packet/tasks",
        "ai",
        "assembled",
        "publish-intents",
    ):
        ensure_private_dir(path / relative, root=state)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "pipeline": "daily-analysis",
        "asof": "2026-09-01",
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "state": "machine_complete",
        "status": "machine_complete",
        "current_stage": "prepare",
        "publication_time": "2026-09-02T00:00:00+00:00",
        "rules_digest": "a" * 64,
        "prompt_policy_version": "analysis-v1",
        "review_set_id": review_set_id,
        "ai_task_count": 0,
        "reused_task_count": 0,
        "model_invocation_count": 0,
    }
    write_json_atomic(path / "manifest.json", manifest, root=state)
    return Workspace(state, path, path / "manifest.json")


def _entries(*, changed: bool = False) -> list[dict[str, object]]:
    return [
        {
            "ticker": "1111",
            "decision": "TODO",
            "priority": None,
            "rationale": "TODO",
            "research_question": None,
            "key_risk": None,
            "candidate_snapshot": {
                "name": "A",
                "sector_33": "Services",
                "review_position": 1,
                "nominations": [{"valuation_approach_id": "asset-value"}],
                "analysis": {"quality_flag": "changed" if changed else "current"},
            },
        },
        {
            "ticker": "2222",
            "decision": "TODO",
            "priority": None,
            "rationale": "TODO",
            "research_question": None,
            "key_risk": None,
            "candidate_snapshot": {
                "name": "B",
                "sector_33": "Retail",
                "review_position": 2,
                "nominations": [{"valuation_approach_id": "earnings-power"}],
                "analysis": {"quality_flag": "current"},
            },
        },
    ]


def _patch_sources(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, object]]) -> None:
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._bind_operation", lambda _root, _asof: "op-test"
    )
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._review_set",
        lambda _root, review_set_id: {"review_set_id": review_set_id},
    )

    def scaffold(_root: Path, workspace: Workspace, review_set_id: str) -> dict[str, object]:
        value = {
            "review_set_id": review_set_id,
            "run_revision_id": "run-revision-a",
            "as_of": "2026-09-01",
            "macro_context_id": "macro-a",
            "expected_prior_research_triage_id": "triage-prior",
            "entries": entries,
        }
        (workspace.path / "assembled/research-triage-scaffold.yaml").write_text(
            yaml.safe_dump(value, sort_keys=False), encoding="utf-8"
        )
        return value

    monkeypatch.setattr("baibai_batch.analysis.packet._scaffold", scaffold)

    def macro_projection(
        workspace: Workspace, *, root: Path, context_id: object, asof: str
    ) -> dict[str, object]:
        del root
        projection = {
            "schema_version": 1,
            "context_id": context_id,
            "as_of": asof,
            "summary": "current macro",
            "synthesis": {},
            "connection": {},
        }
        write_json_atomic(
            workspace.path / "packet/shared/macro-context.json",
            projection,
            root=workspace.state_root,
        )
        return projection

    monkeypatch.setattr(
        "baibai_batch.analysis.packet._prepare_macro_context_projection",
        macro_projection,
    )
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._macro_monitor",
        lambda _root, asof, manual: {
            "status": "review" if manual else "no_ai",
            "reason": "manual" if manual else "consumer_freshness_current",
            "asof": asof,
        },
    )


def _results(index: object) -> dict[str, object]:
    assert isinstance(index, dict)
    tasks = index["tasks"]
    assert isinstance(tasks, list)
    rows: list[dict[str, object]] = []
    for task in tasks:
        assert isinstance(task, dict)
        if task["type"] != "research-triage":
            continue
        ticker = task["subject"]
        rows.append(
            {
                "task_id": task["task_id"],
                "input_digest": task["input_digest"],
                "judgment": {
                    "verdict": "research" if ticker == "1111" else "skip",
                    "rationale": "調査で仮説を確認する"
                    if ticker == "1111"
                    else "追加調査の価値が低い",
                    "research_question": "収益は持続するか" if ticker == "1111" else None,
                    "key_risk": "顧客集中" if ticker == "1111" else None,
                },
            }
        )
    return {"schema_version": 1, "packet_id": index["packet_id"], "results": rows}


def test_private_atomic_files_and_redaction(tmp_path: Path) -> None:
    state = ensure_private_dir(tmp_path / "state")
    target = state / "nested/result.json"
    write_json_atomic(target, {"ok": True}, root=state)

    assert read_json(target, root=state) == {"ok": True}
    assert state.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    assert redact("Authorization: secret API_KEY=abc cookie=x") == "Authorization: [REDACTED]"
    assert redact('Authorization: Bearer secret\n{"token":"hidden"}') == (
        'Authorization: [REDACTED]\n{"token":"[REDACTED]"}'
    )
    sentinels = (
        "AWS_SECRET_ACCESS_KEY=aws-secret",
        "R2_SECRET_ACCESS_KEY=r2-secret",
        "R2_ACCESS_KEY_ID=r2-id",
        "EDINET_API_KEY=edinet-secret",
        "JQUANTS_API_KEY=jquants-secret",
        "CLOUDFLARE_API_TOKEN=cloudflare-secret",
        '"client_secret":"oauth-secret"',
    )
    redacted = redact("\n".join(sentinels))
    assert not any(
        secret in redacted
        for secret in (
            "aws-secret",
            "r2-secret",
            "r2-id",
            "edinet-secret",
            "jquants-secret",
            "cloudflare-secret",
            "oauth-secret",
        )
    )
    assert redact_argv(["tool", "--client-secret", "oauth-secret", "--access-key-id=r2-id"]) == [
        "tool",
        "--client-secret",
        "[REDACTED]",
        "--access-key-id=[REDACTED]",
    ]


def test_workspace_write_rejects_symlink_traversal(tmp_path: Path) -> None:
    state = ensure_private_dir(tmp_path / "state")
    outside = ensure_private_dir(tmp_path / "outside")
    (state / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match=r"symlink|escapes"):
        write_json_atomic(state / "escape/result.json", {}, root=state)

    real = state / "real.json"
    real.write_text("{}", encoding="utf-8")
    alias = state / "alias.json"
    alias.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        write_json_atomic(alias, {"changed": True}, root=state)
    assert real.read_text(encoding="utf-8") == "{}"


def test_log_is_bounded_and_marks_truncation(tmp_path: Path) -> None:
    state = ensure_private_dir(tmp_path / "state")

    _digest, truncated = write_log(state / "large.log", "x" * 2_100_000, root=state)

    assert truncated is True
    assert (state / "large.log").stat().st_size == 2_000_000


def test_duplicate_pipeline_lock_is_a_noop_signal(tmp_path: Path) -> None:
    state = ensure_private_dir(tmp_path / "state")
    first = PipelineLock(state, "2026-09-01")
    second = PipelineLock(state, "2026-09-01")
    with first, second:
        assert first.acquire() is True
        assert second.acquire() is False


def test_active_workspace_resume_rejects_publication_identity_edit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n", encoding="utf-8"
    )
    rules = root / "method/screening/rules"
    rules.mkdir(parents=True)
    (rules / "current.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "pyproject.toml", "method"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    state = tmp_path / "state"
    workspace = create_workspace(state_dir=state, root=root, asof=date(2026, 9, 1))
    manifest = workspace.manifest()
    manifest["review_set_id"] = "review-set-edited"
    write_json_atomic(workspace.manifest_path, manifest, root=workspace.state_root)

    with pytest.raises(ValueError, match="publication identity was modified"):
        create_workspace(state_dir=state, root=root, asof=date(2026, 9, 1))


def test_prompt_policy_fingerprint_tracks_research_triage_skill_bytes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n", encoding="utf-8"
    )
    rules = root / "method/screening/rules"
    rules.mkdir(parents=True)
    (rules / "current.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    skill = root / ".agents/skills/research-triage/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("policy v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture v1"], cwd=root, check=True)
    first = repository_fingerprint(root)["prompt_policy_version"]

    skill.write_text("policy v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture v2"], cwd=root, check=True)
    second = repository_fingerprint(root)["prompt_policy_version"]

    assert first.startswith("research-triage:")
    assert second.startswith("research-triage:")
    assert first != second


def test_resumed_step_logging_appends_attempt_without_overwriting(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    step = StepResult("screening-run", ("fixed", "--token", "secret"), 0, 0.1, "a" * 64, "b" * 64)
    command = CommandResult(0, "ok", "")
    StepLogger(workspace)(step, command)

    StepLogger(workspace)(step, command)

    stages = workspace.manifest()["stages"]
    assert isinstance(stages, list)
    assert [stage["attempt"] for stage in stages] == [1, 2]
    assert (workspace.path / "steps/01-screening-run/stdout.log").is_file()
    assert (workspace.path / "steps/02-screening-run/stdout.log").is_file()
    assert stages[0]["argv"] == ["fixed", "--token", "[REDACTED]"]


def test_log_drilldown_rejects_symlinked_stream(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    step = StepResult("screening-run", ("fixed",), 0, 0.1, "a" * 64, "b" * 64)
    StepLogger(workspace)(step, CommandResult(0, "safe", ""))
    outside = tmp_path / "outside.log"
    outside.write_text("not part of the workspace", encoding="utf-8")
    stdout = workspace.path / "steps/01-screening-run/stdout.log"
    stdout.unlink()
    stdout.symlink_to(outside)

    assert (
        analysis_main(["logs", "--workspace", str(workspace.path), "--stage", "screening-run"]) == 1
    )


def test_result_schema_rejects_control_field_injection() -> None:
    payload = {
        "schema_version": 1,
        "packet_id": "packet-a",
        "results": [
            {
                "task_id": "research-triage:1111",
                "input_digest": "a" * 64,
                "judgment": {
                    "verdict": "skip",
                    "rationale": "追加調査の価値が低い",
                    "research_question": None,
                    "key_risk": None,
                    "publish": True,
                },
            }
        ],
    }

    with pytest.raises(ValidationError, match="publish"):
        AIResultEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    ("judgment", "match"),
    [
        (
            {
                "verdict": "research",
                "rationale": "調査する",
                "research_question": None,
                "key_risk": "risk",
            },
            "research requires",
        ),
        (
            {
                "verdict": "skip",
                "rationale": "見送る",
                "research_question": "不要な混入",
                "key_risk": None,
            },
            "skip forbids",
        ),
        (
            {
                "verdict": "skip",
                "rationale": "x" * 1201,
                "research_question": None,
                "key_risk": None,
            },
            "1200",
        ),
    ],
)
def test_result_schema_rejects_decision_shape_and_length(
    judgment: dict[str, object], match: str
) -> None:
    payload = {
        "schema_version": 1,
        "packet_id": "packet-a",
        "results": [
            {"task_id": "research-triage:1111", "input_digest": "a" * 64, "judgment": judgment}
        ],
    }

    with pytest.raises(ValidationError, match=match):
        AIResultEnvelope.model_validate(payload)


def test_result_schema_rejects_duplicate_task() -> None:
    result = {
        "task_id": "research-triage:1111",
        "input_digest": "a" * 64,
        "judgment": {
            "verdict": "skip",
            "rationale": "見送る",
            "research_question": None,
            "key_risk": None,
        },
    }
    with pytest.raises(ValidationError, match="unique"):
        AIResultEnvelope.model_validate(
            {"schema_version": 1, "packet_id": "packet-a", "results": [result, result]}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"review_set_id": None},
        {"run_revision_id": None},
        {"exit_code": "0"},
        {"exit_code": 3},
        {"deferred_failure_count": 1},
        {"schema_version": 2},
    ],
)
def test_daily_recovery_manifest_rejects_corrupt_business_outcome(
    changes: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "machine_complete",
        "asof": "2026-09-01",
        "exit_code": 0,
        "run_revision_id": "run-a",
        "review_set_id": "review-a",
        "deferred_failure_count": 0,
        "steps": [],
    }
    payload.update(changes)

    with pytest.raises(ValidationError):
        DailyManifest.model_validate(payload)


def test_operation_binding_resumes_exact_capital_allocation_and_rejects_other_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {
            "operations": [
                {
                    "operation_id": "op-20260901-capital-allocation-1",
                    "session_kind": "capital-allocation",
                    "as_of": "2026-09-01",
                }
            ]
        },
    )
    assert _bind_operation(tmp_path, "2026-09-01") == "op-20260901-capital-allocation-1"

    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {
            "operations": [
                {
                    "operation_id": "op-20260901-position-review-1",
                    "session_kind": "position-review",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="non-capital-allocation"):
        _bind_operation(tmp_path, "2026-09-01")

    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {
            "operations": [
                {
                    "operation_id": "op-20260831-capital-allocation-1",
                    "session_kind": "capital-allocation",
                    "as_of": "2026-08-31",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="different asof"):
        _bind_operation(tmp_path, "2026-09-01")


def test_packet_digest_ignores_volatile_review_set_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _workspace(tmp_path / "one", review_set_id="review-set-a")
    second = _workspace(tmp_path / "two", review_set_id="review-set-b")
    _patch_sources(monkeypatch, _entries())

    first_index = prepare_packet(first, root=tmp_path)
    second_index = prepare_packet(second, root=tmp_path)

    assert [task.input_digest for task in first_index.tasks] == [
        task.input_digest for task in second_index.tasks
    ]


def test_non_business_day_packet_is_explicit_no_ai(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.update(status="skipped_non_business_day")

    index = prepare_packet(workspace, root=tmp_path)

    assert index.status == "no_ai"
    assert index.tasks == ()
    assert (workspace.path / "packet/index.json").is_file()


def test_exact_cache_reuses_all_tasks_and_changed_candidate_invalidates_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    first = prepare_packet(workspace, root=tmp_path)
    raw = first.model_dump(mode="json")
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(raw)), encoding="utf-8")
    check_results(workspace, results_path=results_path, root=tmp_path)

    unchanged = _workspace(tmp_path, run_id="run-b")
    _patch_sources(monkeypatch, _entries())
    second = prepare_packet(unchanged, root=tmp_path)

    changed = _workspace(tmp_path, run_id="run-c")
    _patch_sources(monkeypatch, _entries(changed=True))
    third = prepare_packet(changed, root=tmp_path)

    assert second.status == "no_ai"
    assert sum(task.reused for task in second.tasks) == 2
    assert [task.reused for task in third.tasks] == [False, True]

    published: list[str] = []
    monkeypatch.setattr(
        analysis_cli,
        "_publish_locked",
        lambda _args, current: published.append(current.run_id) or 0,
    )
    assert (
        analysis_cli._publish_reused_tasks(
            unchanged,
            index=second,
            args=SimpleNamespace(repo_root=tmp_path),
        )
        == 0
    )
    assert published == ["run-b"]
    assert unchanged.manifest()["state"] == "checked"
    assert unchanged.manifest()["model_invocation_count"] == 0


def test_prompt_policy_version_change_invalidates_result_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_workspace = _workspace(tmp_path, run_id="run-first")
    _patch_sources(monkeypatch, _entries())
    first = prepare_packet(first_workspace, root=tmp_path)
    results_path = first_workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(first.model_dump(mode="json"))), encoding="utf-8")
    check_results(first_workspace, results_path=results_path, root=tmp_path)

    changed_workspace = _workspace(tmp_path, run_id="run-policy-v2")
    changed_workspace.update(prompt_policy_version="analysis-v2")
    changed = prepare_packet(changed_workspace, root=tmp_path)

    assert all(not task.reused for task in changed.tasks)
    assert [batch.task_ids for batch in changed.batches] == [
        tuple(task.task_id for task in changed.tasks)
    ]


def test_cache_result_with_different_task_identity_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, run_id="run-first")
    _patch_sources(monkeypatch, _entries())
    first = prepare_packet(workspace, root=tmp_path)
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(first.model_dump(mode="json"))), encoding="utf-8")
    check_results(workspace, results_path=results_path, root=tmp_path)
    task = first.tasks[0]
    cache_path = workspace.state_root / f"cache/analysis-results/{task.input_digest}.json"
    cache = read_json(cache_path, root=workspace.state_root)
    assert isinstance(cache, dict)
    cache["task_id"] = "research-triage:9999"
    write_json_atomic(cache_path, cache, root=workspace.state_root)

    next_workspace = _workspace(tmp_path, run_id="run-next")
    next_index = prepare_packet(next_workspace, root=tmp_path)

    assert next_index.tasks[0].reused is False
    assert next_index.tasks[1].reused is True


def test_stale_or_unknown_task_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _entries()
    snapshot = entries[0]["candidate_snapshot"]
    assert isinstance(snapshot, dict)
    analysis = snapshot["analysis"]
    assert isinstance(analysis, dict)
    analysis["data_quality"] = {"stale_fin_flag": True}
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, entries)
    first = prepare_packet(workspace, root=tmp_path)
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(first.model_dump(mode="json"))), encoding="utf-8")
    check_results(workspace, results_path=results_path, root=tmp_path)

    second = _workspace(tmp_path, run_id="run-b")
    index = prepare_packet(second, root=tmp_path)

    assert [task.reused for task in index.tasks] == [False, True]


def test_check_rejects_digest_mismatch_and_assembles_current_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    index = prepare_packet(workspace, root=tmp_path).model_dump(mode="json")
    results = _results(index)
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(results), encoding="utf-8")

    status, draft_path = check_results(workspace, results_path=results_path, root=tmp_path)

    assert status == "checked"
    assert draft_path is not None
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert [
        (entry["ticker"], entry["decision"], entry["priority"]) for entry in draft["entries"]
    ] == [
        ("1111", "research", 1),
        ("2222", "skip", None),
    ]

    bad_workspace = _workspace(tmp_path, run_id="run-bad")
    _patch_sources(monkeypatch, _entries())
    bad_index = prepare_packet(bad_workspace, root=tmp_path, re_evaluate=True).model_dump(
        mode="json"
    )
    bad = _results(bad_index)
    assert isinstance(bad["results"], list)
    bad["results"][0]["input_digest"] = "f" * 64
    bad_results_path = bad_workspace.path / "ai/results.json"
    bad_results_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="input_digest mismatch"):
        check_results(bad_workspace, results_path=bad_results_path, root=tmp_path)

    missing_workspace = _workspace(tmp_path, run_id="run-missing")
    _patch_sources(monkeypatch, _entries())
    missing_index = prepare_packet(missing_workspace, root=tmp_path, re_evaluate=True).model_dump(
        mode="json"
    )
    missing = _results(missing_index)
    assert isinstance(missing["results"], list)
    missing["results"].pop()
    missing_results_path = missing_workspace.path / "ai/results.json"
    missing_results_path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="missing task"):
        check_results(missing_workspace, results_path=missing_results_path, root=tmp_path)

    outside = tmp_path / "outside-results.json"
    outside_workspace = _workspace(tmp_path, run_id="run-outside")
    _patch_sources(monkeypatch, _entries())
    outside_index = prepare_packet(outside_workspace, root=tmp_path, re_evaluate=True).model_dump(
        mode="json"
    )
    outside.write_text(json.dumps(_results(outside_index)), encoding="utf-8")
    with pytest.raises(ValueError, match="exact workspace"):
        check_results(outside_workspace, results_path=outside, root=tmp_path)


def test_check_rejects_workspace_task_and_scaffold_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    index = prepare_packet(workspace, root=tmp_path).model_dump(mode="json")
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(index)), encoding="utf-8")
    task_path = workspace.path / "packet/tasks/1111.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["observed_facts"]["review_position"] = 99
    task_path.write_text(json.dumps(task), encoding="utf-8")

    with pytest.raises(ValueError, match="task payload digest mismatch"):
        check_results(workspace, results_path=results_path, root=tmp_path)

    _patch_sources(monkeypatch, _entries())
    prepare_packet(workspace, root=tmp_path, re_evaluate=True)
    scaffold = workspace.path / "assembled/research-triage-scaffold.yaml"
    scaffold.write_text(scaffold.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="scaffold digest changed"):
        check_results(workspace, results_path=results_path, root=tmp_path)
    cache_dir = workspace.state_root / "cache/analysis-results"
    assert not cache_dir.exists() or not list(cache_dir.iterdir())


def test_status_validation_rejects_packet_index_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    prepare_packet(workspace, root=tmp_path)
    path = workspace.path / "packet/index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    index["warning"] = "untrusted edit"
    path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ValueError, match="packet index digest"):
        validate_packet(workspace)


def test_manual_macro_trigger_adds_isolated_phase_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {"asof": "2026-09-01", "series": []},
    )

    index = prepare_packet(workspace, root=tmp_path, macro_review=True)

    macro = index.tasks[-1]
    payload = read_json(workspace.path / "packet" / macro.payload_path, root=workspace.state_root)
    assert macro.type == "macro-context"
    assert payload["prior_context_access"] == "forbidden_in_this_phase"
    assert payload["market_snapshot_path"] == "shared/market-snapshot.json"

    reading = workspace.path / "packet/shared/macro-reading.json"
    reading.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="reading digest mismatch"):
        validate_packet(workspace)


def test_mixed_macro_and_triage_check_publishes_triage_then_awaits_macro_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {"asof": "2026-09-01", "series": []},
    )
    index = prepare_packet(workspace, root=tmp_path, macro_review=True).model_dump(mode="json")
    results = _results(index)
    macro_task = next(task for task in index["tasks"] if task["type"] == "macro-context")
    assert isinstance(results["results"], list)
    results["results"].append(
        {
            "task_id": macro_task["task_id"],
            "input_digest": macro_task["input_digest"],
            "judgment": {
                "phase": "independent_current",
                "current_assessment": "現在の主要forceと反証を独立に評価した",
                "counter_evidence": ["反対方向の一次情報を確認した"],
                "source_ids": ["primary-source-1"],
            },
        }
    )
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(results), encoding="utf-8")

    status, _draft = check_results(workspace, results_path=results_path, root=tmp_path)

    assert status == "checked"
    assert workspace.manifest()["macro_phase"] == "independent_complete"
    monkeypatch.setattr(analysis_cli, "validate_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        analysis_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    exit_code = analysis_cli._publish_locked(
        SimpleNamespace(repo_root=tmp_path, format="json"), workspace
    )

    assert exit_code == 0
    assert workspace.manifest()["state"] == "awaiting_human"
    assert workspace.manifest()["macro_phase"] == "independent_complete"


def test_zero_research_publish_completes_bound_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    index = prepare_packet(workspace, root=tmp_path).model_dump(mode="json")
    results = _results(index)
    assert isinstance(results["results"], list)
    for result in results["results"]:
        result["judgment"] = {
            "verdict": "skip",
            "rationale": "追加調査の価値が低い",
            "research_question": None,
            "key_risk": None,
        }
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(results), encoding="utf-8")
    check_results(workspace, results_path=results_path, root=tmp_path)
    completed: list[tuple[object, str]] = []
    monkeypatch.setattr(analysis_cli, "validate_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        analysis_cli,
        "_complete_no_research_operation",
        lambda _root, _workspace, *, operation_id, research_triage_id: completed.append(
            (operation_id, research_triage_id)
        ),
    )
    monkeypatch.setattr(
        analysis_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    exit_code = analysis_cli._publish_locked(
        SimpleNamespace(repo_root=tmp_path, format="json"), workspace
    )

    assert exit_code == 0
    assert workspace.manifest()["state"] == "published"
    assert completed == [
        (
            "op-test",
            yaml.safe_load(
                (workspace.path / "assembled/research-triage.yaml").read_text(encoding="utf-8")
            )["research_triage_id"],
        )
    ]


def test_stale_macro_source_marks_machine_incomplete_without_ai_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {"series": [{"status": "stale"}]},
    )

    index = prepare_packet(workspace, root=tmp_path, macro_review=True)

    assert index.status == "machine_incomplete"
    assert workspace.manifest()["ai_task_count"] == 0
    assert all(task.type == "research-triage" for task in index.tasks)

    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(index.model_dump(mode="json"))), encoding="utf-8")
    with pytest.raises(ValueError, match="state does not permit"):
        check_results(workspace, results_path=results_path, root=tmp_path)


def test_macro_shared_input_over_budget_is_explicitly_machine_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    monkeypatch.setattr(
        "baibai_batch.analysis.packet._run_json",
        lambda _argv, root: {"oversized": "x" * 512_001},
    )

    index = prepare_packet(workspace, root=tmp_path, macro_review=True)

    assert index.status == "machine_incomplete"
    assert index.warning is not None
    assert "packet_budget_exceeded" in index.warning


def test_checked_workspace_cannot_be_reassembled_with_a_new_publication_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _patch_sources(monkeypatch, _entries())
    index = prepare_packet(workspace, root=tmp_path).model_dump(mode="json")
    results_path = workspace.path / "ai/results.json"
    results_path.write_text(json.dumps(_results(index)), encoding="utf-8")

    _status, draft_path = check_results(workspace, results_path=results_path, root=tmp_path)
    assert draft_path is not None
    first_bytes = draft_path.read_bytes()
    with pytest.raises(ValueError, match="state does not permit"):
        check_results(workspace, results_path=results_path, root=tmp_path)
    assert draft_path.read_bytes() == first_bytes
    draft = yaml.safe_load(first_bytes)
    assert draft["published_at"] == "2026-09-02T09:00:00+09:00"


@pytest.mark.parametrize("active_state", ["ai_required", "machine_incomplete", "failed"])
def test_prune_preserves_active_incomplete_workspace(tmp_path: Path, active_state: str) -> None:
    completed = _workspace(tmp_path, run_id="completed")
    active = _workspace(tmp_path, run_id="active")
    completed.update(state="published", status="published")
    completed_manifest = completed.manifest()
    completed_manifest["updated_at"] = "2020-01-01T00:00:00+00:00"
    write_json_atomic(completed.manifest_path, completed_manifest, root=completed.state_root)
    active.update(state=active_state, status=active_state)
    active_manifest = active.manifest()
    active_manifest["updated_at"] = "2020-01-01T00:00:00+00:00"
    write_json_atomic(active.manifest_path, active_manifest, root=active.state_root)
    write_json_atomic(
        active.path.parent / "active.json",
        {"schema_version": 1, "run_id": active.run_id},
        root=active.state_root,
    )

    assert (
        analysis_main(
            [
                "prune-runs",
                "--state-dir",
                str(active.state_root),
                "--older-than-days",
                "0",
                "--failed-older-than-days",
                "0",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert not completed.path.exists()
    assert active.path.exists()
