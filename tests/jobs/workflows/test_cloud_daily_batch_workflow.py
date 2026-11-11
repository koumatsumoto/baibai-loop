"""共通統合はモデルとlake releaseを固定して公開し、Web生成に成功条件を委ねない。"""

import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.workflow_schedule import declared_schedule

from baibai_jobs.publication.lease import DAILY_LEASE_TTL

ROOT = Path(__file__).resolve().parents[3]
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
    assert declared_schedule(WORKFLOW_PATH.read_text()) == [{"cron": "43 13 * * *"}]
    job = workflow["jobs"]["daily"]
    assert int(job["timeout-minutes"]) == 210
    assert timedelta(minutes=int(job["timeout-minutes"])) == DAILY_LEASE_TTL - timedelta(minutes=10)
    assert "needs" not in job
    for secret in ("JQUANTS_API_KEY", "EDINET_API_KEY", "ESTAT_APP_ID"):
        assert secret not in str(job)
    for source in ("MASTER", "CALENDAR", "PRICE", "FINANCIAL"):
        assert f"BAIBAI_{source}_NATIVE" not in str(workflow)


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
    assert '--integration-report "$RUNNER_TEMP/integration/applied.json"' in by_id["batch"]["run"]
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
            "PRESERVE_EXISTING_GENERATIONS": "",
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


@pytest.mark.parametrize("value", ["", "false", "true"])
def test_dispatch_retention_reaches_model_cli_only_when_explicit(tmp_path, workflow, by_id, value):
    declaration = workflow["on"]["workflow_dispatch"]["inputs"]["preserve_existing_generations"]
    assert declaration["type"] == "boolean"
    assert declaration["default"] == "false"
    validate = by_id["validate-input"]
    assert validate["env"]["PRESERVE_EXISTING_GENERATIONS"] == (
        "${{ inputs.preserve_existing_generations }}"
    )
    assert by_id["batch"]["env"]["PRESERVE_EXISTING_GENERATIONS"] == (
        "${{ steps.validate-input.outputs.preserve_existing_generations }}"
    )
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "MANUAL_ASOF": "",
        "PRESERVE_EXISTING_GENERATIONS": value,
        "PYTHONPATH": str(ROOT / "jobs/src"),
        "GITHUB_OUTPUT": str(output),
    }
    validated = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", validate["run"]],
        env=env,
        capture_output=True,
        check=False,
    )
    assert validated.returncode == 0, validated.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values == {"asof": "", "preserve_existing_generations": value}
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/arguments"\n')
    uv.chmod(0o755)
    batch = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", by_id["batch"]["run"]],
        env={
            **env,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "INTEGRATION_ASOF": "2026-10-25",
            "PRESERVE_EXISTING_GENERATIONS": values["preserve_existing_generations"],
        },
        capture_output=True,
        check=False,
    )
    assert batch.returncode == 0, batch.stderr
    args = (tmp_path / "arguments").read_text().splitlines()
    assert args[:3] == ["run", "baibai-jobs", "model-integration"]
    assert args.count("--preserve-existing-generations") == (1 if value == "true" else 0)


def test_invalid_retention_never_emits_workflow_outputs(tmp_path, steps, by_id):
    validate = by_id["validate-input"]
    assert all("secrets." not in str(step) for step in steps[: steps.index(validate) + 1])
    output = tmp_path / "outputs"
    value = "true\nasof=2099-01-01"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", validate["run"]],
        env={
            **os.environ,
            "MANUAL_ASOF": "",
            "PRESERVE_EXISTING_GENERATIONS": value,
            "PYTHONPATH": str(ROOT / "jobs/src"),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert value.encode() not in result.stderr
    assert not output.exists()


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

    from baibai_jobs.schedule import integration_target_date

    original = datetime(2026, 10, 23, 13, 43, tzinfo=UTC)
    assert integration_target_date(original, scheduled=True) == date(2026, 10, 23)
    morning = datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
    assert integration_target_date(morning, scheduled=False) == date(2026, 10, 24)
    assert integration_target_date(morning, scheduled=True) == date(2026, 10, 23)


def test_economic_l1_finishes_before_any_market_or_l2_dependency(steps, by_id):
    ids = [step.get("id") for step in steps]
    assert ids.index("lease-acquire") < ids.index("economic-l1")
    for later in ("market-plan", "pull", "hydrate", "source-integration", "batch"):
        assert ids.index("economic-l1") < ids.index(later)
    assert "economic_sources" in by_id["economic-l1"]["run"]
    assert "--lease-handle" in by_id["economic-l1"]["run"]
    assert "continue-on-error" not in by_id["economic-l1"]
    assert "pull-indicators" not in by_id["source-seal"]["run"]
    assert "--economic-report-path" in by_id["notify"]["run"]
    assert "--economic-l1-outcome" in by_id["notify"]["run"]
