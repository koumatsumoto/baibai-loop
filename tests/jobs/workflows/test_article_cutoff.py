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
    assert cutoff.check(replies.__getitem__).problems == []


def test_active_legacy_workflow_blocks_even_when_currently_empty():
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    replies[endpoint]["state"] = "active"
    assert cutoff.check(replies.__getitem__).problems == [
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
    assert cutoff.check(replies.__getitem__).problems == [
        "317890420: run 43097164269 is not terminal"
    ]


def test_absent_status_or_different_workflow_path_cannot_verify_retirement():
    replies = _snapshot()
    article = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    daily = f"repos/{cutoff.REPO}/actions/workflows/317890420"
    replies[article]["path"] = ".github/workflows/unrelated.yml"
    replies[f"{daily}/runs?per_page=100&page=1"] = {"total_count": False}
    assert cutoff.check(replies.__getitem__).problems == [
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
    assert cutoff.check(replies.__getitem__).problems == [
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

    assert cutoff.check(read).problems == [
        "317890420: legacy workflow state changed during inspection"
    ]


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
        guard = jobs[name]["if"]
        assert "needs.retirement.outputs.attempt == github.run_attempt" in guard
        assert "needs.retirement.result == 'success'" in guard
        assert "needs.retirement.outputs.requires_v2 == 'true'" in guard
        assert "needs.retirement.outputs.requires_v2 == 'false'" in guard


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
    step = next(
        item for item in workflow["jobs"]["check"]["steps"] if item.get("id") == "retirement"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    checker = (
        '[ "$*" = "-m tools.diagnostics.check_article_cutoff --report-v2-condition" ] || exit 6\n'
        'printf "requires_v2=false\\n" >> "$GITHUB_OUTPUT"'
    )
    for name, script in {"git": "printf actual", "python3": checker}.items():
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
        assert outputs.read_text() == "requires_v2=false\n"  # No attempt without output binding.


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
            "REQUIRES_V2": "false",
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
    assert parent["jobs"]["source"]["with"]["requires_v2"] == (
        "${{ needs.retirement.outputs.requires_v2 }}"
    )
    assert child["on"]["workflow_call"]["inputs"]["retirement_attempt"]["required"] == "true"
    assert child["on"]["workflow_call"]["inputs"]["requires_v2"] == {
        "type": "string",
        "required": "true",
    }
    for name in ("acquire", "notify"):
        guard = child["jobs"][name]["if"]
        assert "cloud-economic-sources-current.yml@refs/heads/main" in guard
        assert "inputs.retirement_attempt == github.run_attempt" in guard
        assert "inputs.requires_v2 == 'true'" in guard
        assert "inputs.requires_v2 == 'false'" in guard


@pytest.mark.parametrize(
    ("condition", "writes_proof"),
    [("false", True), ("true", True), ("", False), ("True", False), ("needed", False)],
)
def test_only_literal_conditions_are_bound_to_this_attempt(tmp_path, condition, writes_proof):
    workflow = _workflow("cloud-writer-retirement.yml")
    check = workflow["jobs"]["check"]
    bound = next(item for item in check["steps"] if item.get("id") == "bound")
    assert "if" not in bound  # A failed inventory step cannot produce proof.
    assert check["outputs"] == {
        "attempt": "${{ steps.bound.outputs.attempt }}",
        "requires_v2": "${{ steps.bound.outputs.requires_v2 }}",
    }
    outputs = tmp_path / "outputs"
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", bound["run"]],
        env={
            "REQUIRES_V2": condition,
            "GITHUB_RUN_ATTEMPT": "3",
            "GITHUB_OUTPUT": str(outputs),
        },
        check=False,
    )
    assert (result.returncode == 0) is writes_proof
    assert outputs.exists() is writes_proof
    if writes_proof:
        assert outputs.read_text() == f"attempt=3\nrequires_v2={condition}\n"


WRITER_STEPS = [
    (
        "cloud-daily-batch-current.yml",
        "daily",
        "Acquire publication lease",
        "",
        ("baibai_jobs.publication.lease", "acquire"),
    ),
    *(
        (
            "cloud-article-sources-current.yml",
            "acquire",
            "Acquire due sources and save each partial privately",
            group,
            ("baibai_jobs.acquisition.articles", "--group"),
        )
        for group in ("scheduled", "public", "world", "rakuten")
    ),
    *(
        (
            "cloud-economic-source-current.yml",
            "acquire",
            "Save confirmed receipts in private R2 staging",
            group,
            ("baibai_jobs.integration.economic_handoff", command),
        )
        for group, command in (("fred_csv", "stage-fred"), ("estat", "stage"))
    ),
]


@pytest.mark.parametrize(
    ("workflow_name", "job_name", "step_name", "group", "writer"), WRITER_STEPS
)
@pytest.mark.parametrize(
    ("condition", "read_exit", "expected"),
    [
        ("true", 0, "strict"),
        ("false", 0, "ordinary"),
        ("true", 41, "blocked"),
        ("", 0, "unknown"),
        ("TRUE", 0, "unknown"),
        ("0", 0, "unknown"),
    ],
)
def test_conditional_v2_enforced_in_existing_credentialed_write_steps(
    tmp_path, workflow_name, job_name, step_name, group, writer, condition, read_exit, expected
):
    job = _workflow(workflow_name)["jobs"][job_name]
    step = next(item for item in job["steps"] if item.get("name") == step_name)
    assert {name for name in step["env"] if name.startswith("R2_")} == {
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    }
    assert "REQUIRES_V2" in step["env"]
    marker = tmp_path / "invocations"
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$INVOCATIONS"\n'
        'case "$*" in *"require-v2"*) exit "$V2_EXIT" ;; esac\n'
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", step["run"]],
        env={
            "PATH": str(tmp_path),
            "INVOCATIONS": str(marker),
            "V2_EXIT": str(read_exit),
            "REQUIRES_V2": condition,
            "GROUP": group,
            "SOURCE_ID": "fixture",
            "STAGE_ID": "fixture-2026-11-23-1-1",
            "CREATED_AT": "2026-11-23T00:00:00Z",
            "IDENTITY": "1-1",
            "RUNNER_TEMP": str(tmp_path),
        },
        check=False,
    )
    calls = (
        [line.split()[3:5] for line in marker.read_text().splitlines()] if marker.exists() else []
    )
    probe = ["baibai_jobs.publication.lease", "require-v2"]
    if expected == "unknown":
        assert result.returncode == 2
        assert not calls
    elif expected == "blocked":
        assert result.returncode == read_exit
        assert calls == ([list(writer)] if job_name == "daily" else [probe])
    else:
        assert result.returncode == 0
        if job_name != "daily" and expected == "strict":
            assert calls == [probe, list(writer)]
        else:
            assert calls == [list(writer)]
    if calls and job_name == "daily":
        assert ("--require-v2" in marker.read_text().split()) is (condition == "true")
