import subprocess
from pathlib import Path

import pytest
import yaml
from tools.diagnostics import check_article_cutoff as cutoff

ROOT = Path(__file__).resolve().parents[3]


def _workflow(name):
    return yaml.load((ROOT / ".github" / "workflows" / name).read_text(), Loader=yaml.BaseLoader)


def _snapshot():
    replies = {}
    for identity, path in cutoff.LEGACY_WORKFLOWS.items():
        endpoint = f"repos/{cutoff.REPO}/actions/workflows/{identity}"
        replies[endpoint] = {"id": identity, "path": path, "state": "disabled_manually"}
        replies[f"{endpoint}/runs?per_page=100&page=1"] = {
            "total_count": 0,
            "workflow_runs": [],
        }
    return replies


def test_only_disabled_empty_legacy_ids_confirm_workflow_fence():
    replies = _snapshot()
    assert cutoff.blockers(replies.__getitem__) == []


def test_active_legacy_workflow_blocks_even_when_currently_empty():
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    replies[endpoint]["state"] = "active"
    assert cutoff.blockers(replies.__getitem__) == [
        "401275764: legacy workflow is not disabled_manually"
    ]


@pytest.mark.parametrize(
    "status", ["requested", "queued", "pending", "in_progress", "waiting", "unknown"]
)
def test_disabling_workflow_never_treats_old_runs_as_cancelled(status):
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420/runs?per_page=100&page=1"
    replies[endpoint] = {
        "total_count": 1,
        "workflow_runs": [{"id": 43097164269, "workflow_id": 317890420, "status": status}],
    }
    assert cutoff.blockers(replies.__getitem__) == ["317890420: run 43097164269 is not terminal"]


def test_absent_status_or_different_workflow_path_cannot_verify_retirement():
    replies = _snapshot()
    article = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    daily = f"repos/{cutoff.REPO}/actions/workflows/317890420"
    replies[article]["path"] = ".github/workflows/unrelated.yml"
    replies[f"{daily}/runs?per_page=100&page=1"] = {"total_count": False}
    assert cutoff.blockers(replies.__getitem__) == [
        "401275764: legacy workflow identity not confirmed",
        "317890420: run inventory changed or is incomplete",
    ]


@pytest.mark.parametrize("final_id", [43097164269, 1])
def test_entire_snapshot_includes_old_queued_runs_and_rejects_duplicate_pages(final_id):
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420/runs?per_page=100"
    replies[f"{endpoint}&page=1"] = {
        "total_count": 101,
        "workflow_runs": [
            {"id": number, "workflow_id": 317890420, "status": "completed"}
            for number in range(1, 101)
        ],
    }
    replies[f"{endpoint}&page=2"] = {
        "total_count": 101,
        "workflow_runs": [{"id": final_id, "workflow_id": 317890420, "status": "queued"}],
    }
    assert cutoff.blockers(replies.__getitem__) == [
        "317890420: run 43097164269 is not terminal"
        if final_id == 43097164269
        else "317890420: run inventory contains unknown or duplicate rows"
    ]


def test_state_is_rechecked_after_reading_the_run_inventory():
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420"
    states = iter(["disabled_manually", "active"])

    def read(path):
        if path == endpoint:
            return {**replies[path], "state": next(states)}
        return replies[path]

    assert cutoff.blockers(read) == ["317890420: legacy workflow state changed during inspection"]


@pytest.mark.parametrize("legacy_path", list(cutoff.LEGACY_WORKFLOWS.values()))
def test_legacy_paths_are_inert_archives_and_cannot_use_secrets(legacy_path):
    archive = _workflow(Path(legacy_path).name)
    assert set(archive["on"]) == {"workflow_call"}
    assert archive["permissions"] == {}
    assert archive["jobs"]["retired"]["if"] == "${{ false }}"
    assert "secrets" not in str(archive)


@pytest.mark.parametrize(
    ("file", "writers"),
    [
        ("cloud-article-sources-current.yml", ("acquire",)),
        ("cloud-daily-batch-current.yml", ("daily",)),
        ("cloud-economic-sources-current.yml", ("plan", "source", "notify-plan-failure")),
    ],
)
def test_only_main_can_use_a_fresh_same_attempt_retirement_proof(file, writers):
    jobs = _workflow(file)["jobs"]
    gate = jobs["retirement"]
    assert gate["if"] == "${{ github.ref == 'refs/heads/main' }}"
    assert gate["uses"] == "./.github/workflows/cloud-writer-retirement.yml"
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    for name in writers:
        assert "retirement" in jobs[name]["needs"]
        assert "needs.retirement.outputs.attempt == github.run_attempt" in jobs[name]["if"]


@pytest.mark.parametrize(
    ("repo", "sha", "exitcode", "writes_output"),
    [
        (cutoff.REPO, "actual", 0, True),
        (cutoff.REPO, "wrong", 1, False),
        ("unrelated/repo", "actual", 1, False),
    ],
)
def test_gate_rejects_the_wrong_checkout_and_never_has_r2_credentials(
    tmp_path, repo, sha, exitcode, writes_output
):
    workflow = _workflow("cloud-writer-retirement.yml")
    assert set(workflow["on"]) == {"workflow_call"}
    assert "R2" not in str(workflow)
    step = workflow["jobs"]["check"]["steps"][-1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, script in {"git": "printf actual", "python3": "exit 0"}.items():
        command = fake_bin / name
        command.write_text(f"#!/bin/sh\n{script}\n")
        command.chmod(0o755)
    outputs = tmp_path / "output"
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", step["run"]],
        env={
            "PATH": str(fake_bin),
            "GITHUB_REPOSITORY": repo,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": sha,
            "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_OUTPUT": str(outputs),
        },
        check=False,
    )
    assert result.returncode == exitcode
    assert outputs.exists() is writes_output
    if writes_output:
        assert outputs.read_text() == "attempt=3\n"


@pytest.mark.parametrize(("group", "command"), [("fred_csv", "stage-fred"), ("estat", "stage")])
def test_only_fred_uses_the_current_compressed_receipt_entrypoint(tmp_path, group, command):
    child = _workflow("cloud-economic-source-current.yml")["jobs"]["acquire"]
    assert "cloud-economic-sources-current.yml@refs/heads/main" in child["if"]
    step = next(step for step in child["steps"] if step.get("id") == "stage")
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", step["run"]],
        env={
            "PATH": str(tmp_path),
            "GROUP": group,
            "SOURCE_ID": "fixture",
            "STAGE_ID": f"{group}-2026-11-23-1-1",
            "RUNNER_TEMP": str(tmp_path),
        },
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.splitlines()[4] == command


def test_nested_economic_jobs_cannot_reuse_a_previous_attempt_proof():
    parent = _workflow("cloud-economic-sources-current.yml")
    child = _workflow("cloud-economic-source-current.yml")
    assert parent["jobs"]["source"]["with"]["retirement_attempt"] == (
        "${{ needs.retirement.outputs.attempt }}"
    )
    assert child["on"]["workflow_call"]["inputs"]["retirement_attempt"]["required"] == "true"
    for name in ("acquire", "notify"):
        guard = child["jobs"][name]["if"]
        assert "cloud-economic-sources-current.yml@refs/heads/main" in guard
        assert "inputs.retirement_attempt == github.run_attempt" in guard
