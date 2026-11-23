"""The temporary BOJ proof is a read-only mode of the incumbent workflow."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/cloud-article-sources.yml"
STAGE = "boj_news-20261123T0513-123456789-1"
DIGEST = "a" * 64


@pytest.fixture
def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def _step(workflow, name):
    return next(
        step for step in workflow["jobs"]["verify-boj-pending"]["steps"] if step.get("id") == name
    )


def test_verify_dispatch_is_exclusive_and_uses_the_existing_public_queue(workflow):
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["operation"]["options"] == ["acquire", "verify_boj_pending"]
    assert inputs["operation"]["default"] == "acquire"
    acquire, verify = (workflow["jobs"][name] for name in ("acquire", "verify-boj-pending"))
    assert " ".join(acquire["if"].split()) == (
        "${{ github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' "
        "&& (inputs.operation || 'acquire') == 'acquire' && !inputs.boj_stage_id "
        "&& !inputs.boj_manifest_sha256) }}"
    )
    assert " ".join(verify["if"].split()) == (
        "${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' "
        "&& github.run_attempt == '1' && inputs.operation == 'verify_boj_pending' }}"
    )
    assert verify["concurrency"] == {
        **acquire["concurrency"],
        "group": "source-host-article-public",
    }
    assert verify["permissions"] == {"contents": "read"}
    assert int(verify["timeout-minutes"]) == 10
    steps = verify["steps"]
    assert steps[0]["id"] == "verify-input"
    assert steps[1]["uses"] == acquire["steps"][0]["uses"]
    assert steps[1]["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
    assert steps[2]["id"] == "verify-checkout"
    assert steps[-1]["id"] == "verify-pending"
    assert all("secrets." not in str(step) for step in steps[:-1])
    assert steps[-1]["env"] == {
        key: acquire["steps"][-1]["env"][key]
        for key in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    }
    assert "upload-artifact" not in str(verify)
    assert all("continue-on-error" not in step for step in steps)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, None),
        ("GITHUB_REPOSITORY", "fork/baibai-loop"),
        ("GITHUB_EVENT_NAME", "schedule"),
        ("GITHUB_REF", "refs/heads/review"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("BOJ_STAGE_ID", ""),
        ("BOJ_STAGE_ID", "tsr_data-20261123T0513-123456789-1"),
        ("BOJ_STAGE_ID", "boj_news-;echo synthetic-invalid"),
        ("BOJ_STAGE_ID", "boj_news-" + "x" * 112),
        ("BOJ_MANIFEST_SHA256", ""),
        ("BOJ_MANIFEST_SHA256", "A" * 64),
        ("BOJ_MANIFEST_SHA256", DIGEST + "\n"),
    ],
)
def test_verify_target_is_validated_before_credentials(workflow, field, value):
    env = {
        "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "BOJ_STAGE_ID": STAGE,
        "BOJ_MANIFEST_SHA256": DIGEST,
    }
    if field is not None:
        env[field] = value
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _step(workflow, "verify-input")["run"]],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == (0 if field is None else 2)
    assert result.stdout == ""
    assert result.stderr == (
        "" if field is None else "Exact current-main BOJ stage and digest required\n"
    )


def test_verify_step_only_invokes_the_read_only_owner_after_exact_checkout(workflow, tmp_path):
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\nprintf "%s\\n" "$REPO_HEAD"\n')
    git.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o700)
    path = f"{tmp_path}:{os.environ['PATH']}"
    check = ["bash", "-euo", "pipefail", "-c", _step(workflow, "verify-checkout")["run"]]
    for head in ("e" * 40, "f" * 40):
        result = subprocess.run(
            check, env={"PATH": path, "REPO_HEAD": head, "GITHUB_SHA": "e" * 40}, check=False
        )
        assert result.returncode == int(head != "e" * 40)
    command = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _step(workflow, "verify-pending")["run"]],
        env={"PATH": path, "BOJ_STAGE_ID": STAGE, "BOJ_MANIFEST_SHA256": DIGEST},
        capture_output=True,
        check=True,
        text=True,
    )
    assert command.stdout.splitlines() == [
        "run",
        "python",
        "-m",
        "baibai_jobs.acquisition.articles",
        "verify-boj-pending",
        "--stage-id",
        STAGE,
        "--manifest-sha256",
        DIGEST,
    ]
