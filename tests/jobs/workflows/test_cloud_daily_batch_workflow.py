"""共通統合はモデルとlake releaseを固定して公開し、Web生成に成功条件を委ねない。"""

import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

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
        ("source-integration", "batch"),
        ("batch", "source-seal"),
        ("source-seal", "source-inputs"),
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


def test_article_semantic_refresh_is_opt_in_inside_normal_publication_lease(workflow, steps, by_id):
    declaration = workflow["on"]["workflow_dispatch"]["inputs"]["article_semantic_recovery"]
    assert declaration["required"] == "false"
    assert declaration["type"] == "string"
    assert declaration.get("default", "") == ""
    ids = [step.get("id") for step in steps]
    assert (
        ids.index("lease-acquire")
        < ids.index("hydrate")
        < ids.index("source-integration")
        < ids.index("article-semantic-recovery")
        < ids.index("batch")
    )
    assert ids.index("batch") < ids.index("article-semantic-readback") < ids.index("lease-release")
    refresh = by_id["article-semantic-recovery"]
    assert refresh["if"] == "${{ inputs.article_semantic_recovery != '' }}"
    assert "continue-on-error" not in refresh
    assert "${{ inputs." not in refresh["run"]
    assert "--article-semantic-recovery" in by_id["validate-input"]["run"]
    for name in ("article-semantic-recovery", "article-semantic-readback"):
        assert by_id[name]["env"]["R2_STOP_ON_REJECTION"] == "1"
        assert '--lease-handle "$RUNNER_TEMP/publication-lease-handle.json"' in by_id[name]["run"]
    assert (
        "steps.batch.outputs.models_ready == 'true' && steps.batch.outcome == 'success'"
        in by_id["article-semantic-readback"]["if"]
    )
    assert (
        "steps.article-semantic-recovery.outcome == 'success'"
        in by_id["article-semantic-readback"]["if"]
    )
    assert (
        "extract_observations" not in by_id["batch"]["run"]
    )  # No automatic all-history migration.


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
    assert '"Notify Discord #batch-runs' in WORKFLOW_PATH.read_text()


@pytest.mark.parametrize(
    "step", ["edinet-metrics-acceptance", "article-semantic-recovery", "article-semantic-readback"]
)
def test_explicit_acceptance_outcomes_reach_the_existing_notifier(by_id, step):
    from baibai_jobs.observability.discord import STEP_ORDER

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


def test_dispatch_does_not_offer_runs_pruning(workflow, by_id):
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "asof",
        "edinet_metrics_acceptance",
        "article_semantic_recovery",
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
