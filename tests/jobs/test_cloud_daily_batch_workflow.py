"""共通統合はモデルとlake releaseを固定して公開し、Web生成に成功条件を委ねない。"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["daily"]["steps"]


@pytest.fixture(scope="module")
def by_id(steps):
    return {step["id"]: step for step in steps if "id" in step}


def test_common_integration_runs_including_weekends_without_acquisition(workflow):
    assert workflow["on"]["schedule"] == [{"cron": "43 13 * * *"}]
    job = workflow["jobs"]["daily"]
    assert int(job["timeout-minutes"]) < 120
    assert "needs" not in job
    for secret in ("JQUANTS_API_KEY", "EDINET_API_KEY", "ESTAT_APP_ID"):
        assert secret not in str(job)


def test_core_order_and_source_receipts_survive_without_web(steps, by_id):
    ids = [step.get("id") for step in steps]
    for first, second in (
        ("smoke", "validate-input"),
        ("validate-input", "setup"),
        ("hydrate", "source-integration"),
        ("source-integration", "batch"),
        ("batch", "upload-stores"),
        ("upload-stores", "source-seal"),
        ("source-seal", "source-inputs"),
    ):
        assert ids.index(first) < ids.index(second)
    assert "uv run baibai-jobs model-integration" in by_id["batch"]["run"]
    assert "--lease-handle" in by_id["batch"]["run"]
    assert "publish-lake" not in by_id  # no second export/release after model generation
    assert "publish-serving" not in by_id
    assert "upload-serving" not in str(by_id["upload-stores"])
    assert by_id["upload-stores"]["if"] == "steps.batch.outputs.models_ready == 'true'"
    assert "steps.batch.outcome == 'success'" in by_id["source-seal"]["if"]


def test_notifications_and_credentials_remain_scoped(steps, by_id):
    notify = by_id["notify"]
    assert notify["if"] == "${{ always() }}"
    assert notify["continue-on-error"] == "true"
    assert "--publish-serving-outcome" not in notify["run"]
    assert '--publish-lake-outcome "${{ steps.batch.outcome }}"' in notify["run"]
    assert "${{ inputs." not in notify["run"]
    for step in steps:
        if step.get("id") != "notify":
            assert "DISCORD_WEBHOOK_URL" not in str(step)
        assert "cancelled()" not in step.get("run", "")
        if step.get("id") in ("setup", "sync", "smoke", "validate-input"):
            assert "R2_SECRET_ACCESS_KEY" not in str(step)
    assert '"Notify Discord #batch-runs' in WORKFLOW_PATH.read_text()


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_batch_outputs_are_finalized_before_failure_without_a_web_artifact(
    tmp_path, by_id, exit_code
):
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    uv.chmod(0o755)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", by_id["batch"]["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "INTEGRATION_ASOF": "2026-10-25",
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values == {
        "exit_code": str(exit_code),
        "asof": "2026-10-25",
        "models_ready": "true" if exit_code == 0 else "false",
    }


@pytest.mark.parametrize("exit_code", [0, 2, 127])
def test_machine_push_preserves_its_outcome_and_never_uploads_views(tmp_path, by_id, exit_code):
    script = tmp_path / "jobs/scripts/r2_transfer.sh"
    script.parent.mkdir(parents=True)
    script.write_text(f'#!/bin/sh\n[ "$1" = "push-machine" ] || exit 99\nexit {exit_code}\n')
    script.chmod(0o755)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", by_id["upload-stores"]["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code
    assert output.read_text().strip() == f"machine={'success' if exit_code == 0 else 'failure'}"


def test_integration_date_uses_original_run_and_manual_clock():
    from datetime import UTC, date, datetime

    from baibai_jobs.jobs.schedule import integration_target_date

    original = datetime(2026, 10, 23, 13, 43, tzinfo=UTC)
    assert integration_target_date(original, scheduled=True) == date(2026, 10, 23)
    morning = datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
    assert integration_target_date(morning, scheduled=False) == date(2026, 10, 24)
    assert integration_target_date(morning, scheduled=True) == date(2026, 10, 23)
