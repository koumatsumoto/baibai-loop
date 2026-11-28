"""共通統合はモデルとlake releaseを固定して公開し、Web生成に成功条件を委ねない。"""

import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from baibai_jobs.observability.watchdog import RESULT_STEP_PREFIX
from baibai_jobs.publication.lease import DAILY_LEASE_TTL

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch-current.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["daily"]["steps"]


@pytest.fixture(scope="module")
def by_id(steps):
    return {step["id"]: step for step in steps if "id" in step}


def test_common_integration_runs_without_acquisition(workflow):
    job = workflow["jobs"]["daily"]
    assert int(job["timeout-minutes"]) == 210
    assert timedelta(minutes=int(job["timeout-minutes"])) == DAILY_LEASE_TTL - timedelta(minutes=10)
    assert job["needs"] == "retirement"
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
        ("source-integration", "listing-master"),
        ("listing-master", "batch"),
        ("batch", "source-seal"),
        ("source-seal", "source-check"),
    ):
        assert ids.index(first) < ids.index(second)
    assert "uv run baibai-jobs model-integration" in by_id["batch"]["run"]
    assert "--lease-handle" in by_id["batch"]["run"]
    assert '--integration-report "$RUNNER_TEMP/integration/applied.json"' in by_id["batch"]["run"]
    assert not {"publish-lake", "upload-stores", "publish-serving"} & by_id.keys()
    assert by_id["source-seal"]["if"] == (
        "${{ always() && steps.batch.outputs.models_ready == 'true' "
        "&& steps.batch.outcome == 'success' }}"
    )
    assert '--daily-readback-notice "$RUNNER_TEMP/batch-notice.json"' in by_id["source-seal"]["run"]
    check = by_id["source-check"]
    assert "steps.source-seal.outcome == 'success'" in check["if"]
    assert "continue-on-error" not in check
    assert 'integration.sources check --directory "$RUNNER_TEMP/integration"' in check["run"]


def test_optional_local_probe_is_independent_of_daily_publication(workflow, steps, by_id):
    option = workflow["on"]["workflow_dispatch"]["inputs"]["jp_input_probe_ticker"]
    assert option["required"] == "false"
    probe = by_id["jp-input-probe"]
    assert steps.index(probe) > steps.index(by_id["notify"])
    assert probe["if"] == (
        "${{ !cancelled() && github.event_name == 'workflow_dispatch' "
        "&& inputs.jp_input_probe_ticker != '' && steps.batch.outcome == 'success' }}"
    )
    assert probe["continue-on-error"] == "true"
    assert "env" not in probe  # Neither private dispatch values in logs nor writer credentials.


def test_optional_edinet_acceptance_is_before_any_model_publication(workflow, steps, by_id):
    declaration = workflow["on"]["workflow_dispatch"]["inputs"]["edinet_metrics_acceptance"]
    assert declaration["type"] == "string"
    assert declaration["required"] == "false"
    gate = by_id["edinet-metrics-acceptance"]
    assert gate["if"] == "${{ inputs.edinet_metrics_acceptance != '' }}"
    ids = [step.get("id") for step in steps]
    assert (
        ids.index("source-integration")
        < ids.index("edinet-metrics-acceptance")
        < ids.index("batch")
    )
    assert "continue-on-error" not in gate
    assert gate["env"]["R2_STOP_ON_REJECTION"] == "1"
    assert gate["run"].index("integration.sources check") < gate["run"].index(
        "integration.edinet_metric_acceptance"
    )
    assert "${{ inputs." not in gate["run"]
    assert '"$RUNNER_TEMP/integration/applied.json"' in gate["run"]
    assert "if" not in by_id["batch"]  # GitHub's normal success condition, never always().


def test_current_daily_has_no_article_replay_entrypoint(workflow, by_id):
    assert "article_semantic_recovery" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "article-semantic-recovery" not in by_id
    assert "article-semantic-readback" not in by_id
    assert "article_semantic_recovery" not in str(workflow)
    assert "--article-semantic-" not in str(workflow)
    assert "source-inputs" not in by_id  # Only retired article planning used it.
    assert "integration.sources inputs" not in str(workflow)


@pytest.mark.parametrize(
    ("source_exit", "acceptance_exit", "calls"), [(41, 0, 1), (0, 42, 2), (0, 0, 2)]
)
def test_acceptance_phase_does_not_continue_after_a_failed_check(
    tmp_path, by_id, source_exit, acceptance_exit, calls
):
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RUNNER_TEMP/calls"\n'
        f'case "$*" in *integration.sources*) exit {source_exit};; *) exit {acceptance_exit};; esac\n'
    )
    uv.chmod(0o755)
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", by_id["edinet-metrics-acceptance"]["run"]],
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "EDINET_METRICS_ACCEPTANCE": '{"quoted":"$(exit 99)"}',
        },
        capture_output=True,
        check=False,
    )
    assert completed.returncode == (source_exit or acceptance_exit)
    assert len((tmp_path / "calls").read_text().splitlines()) == calls
    assert (tmp_path / "edinet-metrics-acceptance.json").read_text() == '{"quoted":"$(exit 99)"}'


def test_notifications_and_credentials_remain_scoped(steps, by_id):
    notify = by_id["notify"]
    assert notify["if"] == "${{ always() }}"
    assert notify["continue-on-error"] == "true"
    assert "--publish-serving-outcome" not in notify["run"]
    assert '--machine-publication-outcome "${{ steps.batch.outcome }}"' in notify["run"]
    assert '--hydrate-outcome "${{ steps.hydrate.outcome }}"' in notify["run"]
    for obsolete in ("--publish-lake-outcome", "--upload-stores-outcome"):
        assert obsolete not in notify["run"]
    assert "${{ inputs." not in notify["run"]
    for step in steps:
        if step.get("id") != "notify":
            assert "DISCORD_WEBHOOK_URL" not in str(step)
        assert "cancelled()" not in step.get("run", "")
        if step.get("id") in ("setup", "sync", "smoke", "validate-input"):
            assert "R2_SECRET_ACCESS_KEY" not in str(step)
    assert notify["name"].startswith(RESULT_STEP_PREFIX)


def test_explicit_edinet_acceptance_outcome_reaches_the_existing_notifier(by_id):
    from baibai_jobs.observability.discord import STEP_ORDER

    step = "edinet-metrics-acceptance"
    assert step in STEP_ORDER
    assert step in by_id
    assert f'--{step}-outcome "${{{{ steps.{step}.outcome }}}}"' in by_id["notify"]["run"]


@pytest.mark.parametrize("exit_code", [0, 1, 2, 127])
def test_failed_machine_publication_never_reports_ready_or_starts_another_process(
    tmp_path, by_id, exit_code
):
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\n" "$*" >> "$RUNNER_TEMP/calls"\nexit {exit_code}\n')
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
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("run baibai-jobs model-integration ")
    assert "--daily-origin" in calls[0]
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values == {
        "exit_code": str(exit_code),
        "asof": "2026-10-25",
        "models_ready": "true" if exit_code == 0 else "false",
    }


@pytest.mark.parametrize("pin_exit", [0, 41])
def test_daily_l2_and_web_share_one_required_listing_pin(tmp_path, by_id, pin_exit):
    assert "if" not in by_id["listing-master"]
    assert "continue-on-error" not in by_id["listing-master"]
    assert "if" not in by_id["batch"]  # A failed pin never reaches either consumer.
    runner = tmp_path / "runner temp"
    runner.mkdir()
    path = runner / "current-listing-master.json"
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
runner = Path(os.environ["RUNNER_TEMP"])
pin = runner / "current-listing-master.json"
if args[1:4] == ["python", "-m", "baibai_jobs.integration.listing_master_current"]:
    phase = "pin"
    assert Path(args[args.index("--output") + 1]) == pin
else:
    assert Path(args[args.index("--current-listing-master") + 1]) == pin
    assert pin.read_bytes() == b"same listing input"
    phase = "l2" if args[1:3] == ["baibai-jobs", "model-integration"] else "web"
    if phase == "web":
        assert args[1:4] == ["python", "-m", "baibai_web.materialize"]
with (runner / "calls").open("a") as log:
    print(phase, file=log)
if phase == "pin":
    if exit_code := int(os.environ["PIN_EXIT"]):
        raise SystemExit(exit_code)
    with pin.open("xb") as output:
        output.write(b"same listing input")
"""
    )
    uv.chmod(0o755)
    scripts = tmp_path / "jobs/scripts"
    scripts.mkdir(parents=True)
    transfer = scripts / "r2_transfer.sh"
    transfer.write_text('#!/bin/sh\nprintf "%s\n" "$1" >> "$RUNNER_TEMP/calls"\n')
    transfer.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "INTEGRATION_ASOF": "2026-11-27",
        "PIN_EXIT": str(pin_exit),
    }
    for phase in ("listing-master", "batch", "portfolio-views"):
        completed = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", by_id[phase]["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            break
    assert completed.returncode == pin_exit
    assert (runner / "calls").read_text().splitlines() == (
        ["pin"] if pin_exit else ["pin", "l2", "pull-app", "web", "upload-portfolio-views"]
    )
    assert path.exists() == (pin_exit == 0)


def test_dispatch_does_not_offer_runs_pruning(workflow, by_id):
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "asof",
        "jp_input_probe_ticker",
        "edinet_metrics_acceptance",
        "us_market",
    }
    for step in ("validate-input", "batch"):
        assert "--preserve-existing-generations" not in by_id[step]["run"]


def test_invalid_date_never_emits_workflow_outputs(tmp_path, steps, by_id):
    validate = by_id["validate-input"]
    assert all("secrets." not in str(step) for step in steps[: steps.index(validate) + 1])
    output = tmp_path / "outputs"
    value = "2026-11-24\nasof=2099-01-01"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", validate["run"]],
        env={
            **os.environ,
            "MANUAL_ASOF": value,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "PYTHONPATH": str(ROOT / "jobs/src"),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert value.encode() not in result.stderr
    assert not output.exists()


def test_integration_date_uses_original_run_and_manual_clock():
    from datetime import UTC, date, datetime

    from baibai_jobs.schedule import integration_target_date

    original = datetime(2026, 10, 23, 13, 43, tzinfo=UTC)
    assert integration_target_date(original, scheduled=True) == date(2026, 10, 23)
    morning = datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
    assert integration_target_date(morning, scheduled=False) == date(2026, 10, 24)
    assert integration_target_date(morning, scheduled=True) == date(2026, 10, 23)


def test_economic_l1_follows_machine_admission_but_is_independent_of_hydration(steps, by_id):
    ids = [step.get("id") for step in steps]
    assert ids.index("lease-acquire") < ids.index("pull") < ids.index("economic-l1")
    assert by_id["pull"]["run"] == "jobs/scripts/r2_transfer.sh pull-machine --daily-preflight"
    assert "publication-lease-handle.json" in by_id["pull"]["env"]["PUBLICATION_LEASE_HANDLE"]
    assert "if" not in by_id["pull"]
    assert "continue-on-error" not in by_id["pull"]
    assert "if" not in by_id["economic-l1"]  # Normal success only, not an always() publisher.
    for later in ("market-plan", "hydrate", "source-integration", "batch"):
        assert ids.index("economic-l1") < ids.index(later)
    hydrate = by_id["hydrate"]
    assert ids.index("market-plan") < ids.index("hydrate") < ids.index("source-integration")
    assert hydrate["run"] == "jobs/scripts/r2_transfer.sh hydrate-market-for-daily"
    assert (
        hydrate["env"]["PUBLICATION_LEASE_HANDLE"]
        == (by_id["pull"]["env"]["PUBLICATION_LEASE_HANDLE"])
    )
    assert "if" not in hydrate
    assert "continue-on-error" not in hydrate
    assert "economic_sources" in by_id["economic-l1"]["run"]
    assert "--lease-handle" in by_id["economic-l1"]["run"]
    assert "continue-on-error" not in by_id["economic-l1"]
    assert "pull-indicators" not in by_id["source-seal"]["run"]
    assert "--economic-report-path" in by_id["notify"]["run"]
    assert "--economic-l1-outcome" in by_id["notify"]["run"]


def test_us_job_is_independent_and_manually_opt_in(workflow):
    option = workflow["on"]["workflow_dispatch"]["inputs"]["us_market"]
    assert (option["type"], option["default"], option["required"]) == ("boolean", "false", "false")
    job = workflow["jobs"]["us-market"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "github.event_name == 'schedule'" in job["if"]
    assert "github.event_name == 'workflow_dispatch' && inputs.us_market == true" in job["if"]
    assert "needs" not in job
    assert "continue-on-error" not in job
    assert "APCA_" not in str(workflow["jobs"]["daily"])
    assert "US_MARKET" not in str(workflow["jobs"]["daily"])
    assert "secrets." not in str(job["env"])
    assert not any("continue-on-error" in step for step in job["steps"])
    outside = [step for step in job["steps"] if step.get("id") != "us-market-inputs"]
    assert all(
        "APCA_" not in str(step) and "R2_SECRET_ACCESS_KEY" not in str(step) for step in outside
    )


@pytest.mark.parametrize("exit_code", [0, 1])
def test_us_job_uses_own_clock_without_jp_asof_and_preserves_exit(tmp_path, workflow, exit_code):
    steps = {step["id"]: step for step in workflow["jobs"]["us-market"]["steps"] if "id" in step}
    assert steps["clock"]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert ".created_at" in steps["clock"]["run"]
    step = steps["us-market-inputs"]
    assert step["env"]["RUN_CREATED_AT"] == "${{ steps.clock.outputs.created_at }}"
    assert "inputs." not in str(step)
    assert "ASOF" not in str(step)
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/calls"\nexit {exit_code}\n')
    uv.chmod(0o755)
    origin = "2026-11-28T13:43:00Z"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "RUN_CREATED_AT": origin,
            "GITHUB_EVENT_NAME": "schedule",
            "INTEGRATION_ASOF": "2026-11-27",
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code
    assert (tmp_path / "calls").read_text().splitlines() == [
        "run",
        "--offline",
        "--no-sync",
        "python",
        "-m",
        "baibai_jobs.integration.us_market_daily",
        "--run-created-at",
        origin,
        "--event",
        "schedule",
    ]
