from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch.yml"


def _daily_batch_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    batch = next(step for step in workflow["jobs"]["daily"]["steps"] if step.get("id") == "batch")
    return {str(name): str(value) for name, value in batch["env"].items()}


class CloudDailyBatchWorkflowTests(unittest.TestCase):
    def test_daily_batch_step_env_covers_every_required_jpx_source(self) -> None:
        # scheduled run が当日 cache 不足で bootstrap-cache に入ると、JPX 規制 provider は
        # active rules の universe.required_jpx_flags に含まれる全 source を要求する。
        # batch step env がその実ゲートを ScreeningConfig 経由で満たせることを機械的に
        # 検査する (外部 HTTP へは接続せず、source 名の網羅だけを確認する。URL 値の到達性は
        # マージ後の実接続再検証が担う)。
        env = _daily_batch_env()
        config = ScreeningConfig.from_env(env)
        rules = load_screening_rules(ROOT / DEFAULT_RULES_PATH)

        required_sources = set(rules.universe.required_jpx_flags)
        self.assertTrue(required_sources)
        missing = required_sources - set(config.jpx_regulation_urls)
        self.assertEqual(missing, set())


# --- notification wiring contract -----------------------------------------
#
# These pin the workflow *wiring* that makes the Discord notification correct:
# stable step ids, the single ``always()`` notification point, the webhook secret
# scoped to that step alone, batch outputs finalized before a fatal exit, and the
# step ordering the decision table relies on. The decision semantics themselves
# live in notify_discord and are unit-tested there.


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["daily"]["steps"]


@pytest.fixture(scope="module")
def steps_by_id(steps: list[dict]) -> dict[str, dict]:
    return {step["id"]: step for step in steps if "id" in step}


def test_known_steps_have_stable_ids(steps_by_id: dict[str, dict]) -> None:
    for step_id in (
        "smoke",
        "validate-input",
        "setup",
        "sync",
        "pull",
        "batch",
        "upload-machine",
        "upload-serving",
        "deferred-report",
        "cancellation",
        "notify",
        "upload-run-summary",
    ):
        assert step_id in steps_by_id, f"missing stable step id: {step_id}"


def test_smoke_check_runs_before_setup_python_on_system_python(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("smoke") < ids.index("setup")
    smoke_run = steps_by_id["smoke"]["run"]
    assert "py_compile tools/cloud/batch_summary.py tools/cloud/notify_discord.py" in smoke_run
    # The import (not just py_compile) guarantees the stdlib-only contract.
    assert "import tools.cloud.batch_summary" in smoke_run
    assert "import tools.cloud.notify_discord" in smoke_run
    assert "started_at=" in smoke_run


def test_dispatch_input_is_validated_after_smoke_and_before_setup(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("smoke") < ids.index("validate-input") < ids.index("setup")
    validation = steps_by_id["validate-input"]
    assert validation["env"] == {"MANUAL_ASOF": "${{ inputs.asof }}"}
    assert '--asof "$MANUAL_ASOF"' in validation["run"]
    assert 'echo "asof=$MANUAL_ASOF" >> "$GITHUB_OUTPUT"' in validation["run"]


def test_batch_step_invokes_daily_batch_as_a_module(steps_by_id: dict[str, dict]) -> None:
    # Script-path invocation puts tools/cloud (not the repo root) on sys.path, so
    # `from tools.cloud...` inside daily_batch raises ModuleNotFoundError before
    # the batch starts; only `-m` puts the working directory on sys.path.
    batch_run = steps_by_id["batch"]["run"]
    assert "python -m tools.cloud.daily_batch" in batch_run
    assert "tools/cloud/daily_batch.py" not in batch_run


def test_batch_step_writes_summary_and_finalizes_outputs_before_fatal_exit(
    steps_by_id: dict[str, dict],
) -> None:
    batch_run = steps_by_id["batch"]["run"]
    assert "--summary-output" in batch_run
    # Outputs are echoed before the fatal exit so the notification step sees them.
    assert batch_run.index('echo "exit_code=$code"') < batch_run.index('exit "$code"')
    assert 'echo "local_export=true"' in batch_run
    assert 'echo "local_export=false"' in batch_run


def test_upload_steps_run_only_when_published(steps_by_id: dict[str, dict]) -> None:
    assert steps_by_id["upload-machine"]["if"] == "steps.batch.outputs.published == 'true'"
    assert steps_by_id["upload-serving"]["if"] == "steps.batch.outputs.published == 'true'"


def test_uploads_precede_deferred_report_which_fires_on_exit_3(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("upload-machine") < ids.index("deferred-report")
    assert ids.index("upload-serving") < ids.index("deferred-report")
    assert steps_by_id["deferred-report"]["if"] == "steps.batch.outputs.exit_code == '3'"


def test_notify_is_the_single_notification_point_running_on_every_terminal_state(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    # Notification stays the last step that decides the run outcome; only the
    # best-effort summary upload may follow it.
    assert ids[-2:] == ["notify", "upload-run-summary"]
    # `always()`, never `!cancelled()`: GitHub reports a `timeout-minutes` expiry
    # as a cancellation, so `!cancelled()` silently skips the notification for a
    # hung batch — the failure this workflow most needs to report.
    assert steps_by_id["notify"]["if"] == "${{ always() }}"
    # The cancellation state still reaches the notifier, via the step output.
    assert "steps.cancellation.outputs.cancelled" in steps_by_id["notify"]["run"]
    assert steps_by_id["cancellation"]["if"] == "${{ cancelled() }}"


def test_no_run_block_calls_a_status_check_function(steps: list[dict]) -> None:
    # success() / failure() / cancelled() / always() are only evaluated in an `if`
    # conditional. Interpolating one into `run:` makes the whole workflow file
    # invalid, which GitHub reports as a failed run with no step output at all —
    # a shape that YAML parsing alone cannot catch.
    for step in steps:
        run = step.get("run", "")
        for function in ("success()", "failure()", "cancelled()", "always()"):
            assert function not in run, f"{step.get('name')}: {function} in run:"


def test_notify_step_name_keeps_the_channel_out_of_a_yaml_comment(
    steps_by_id: dict[str, dict],
) -> None:
    # An unquoted ` #` starts a YAML comment, which would truncate the name to
    # "Notify Discord" in the Actions UI.
    assert steps_by_id["notify"]["name"] == "Notify Discord #batch-runs"


def test_run_summary_upload_publishes_the_file_notify_wrote_without_changing_the_outcome(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("notify") < ids.index("upload-run-summary")
    upload = steps_by_id["upload-run-summary"]
    # Same reason as notify: a timed-out run must still publish its record.
    assert upload["if"] == "${{ always() && steps.validate-input.outcome == 'success' }}"
    # Best-effort: publishing the record must not turn a delivered notification
    # or a successful publish into a failed run.
    assert upload["continue-on-error"] is True
    assert "upload-run-summary" in upload["run"]

    # The two steps must name the same file, or the upload silently publishes nothing.
    notify_run = steps_by_id["notify"]["run"]
    summary_path = notify_run.split("--output")[1].split()[0].strip('"')
    assert summary_path in upload["run"]


def test_webhook_secret_is_scoped_to_the_notify_step_alone(
    workflow: dict, steps: list[dict]
) -> None:
    job_env = workflow["jobs"]["daily"].get("env", {})
    assert "DISCORD_WEBHOOK_URL" not in job_env

    holders = [step.get("id") for step in steps if "DISCORD_WEBHOOK_URL" in step.get("env", {})]
    assert holders == ["notify"]


def test_data_credentials_are_absent_from_job_and_setup_steps(
    workflow: dict, steps: list[dict]
) -> None:
    restricted = {
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "JQUANTS_API_KEY",
        "ESTAT_APP_ID",
        "EDINET_API_KEY",
    }
    assert "env" not in workflow["jobs"]["daily"]
    holders = {
        step.get("id", step.get("name")): restricted.intersection(step.get("env", {}))
        for step in steps
        if restricted.intersection(step.get("env", {}))
    }
    assert holders == {
        "pull": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "preserve-market-v13": {
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
        },
        "batch": {"JQUANTS_API_KEY", "ESTAT_APP_ID", "EDINET_API_KEY"},
        "upload-machine": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "upload-serving": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "upload-run-summary": {
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
        },
    }


def test_notify_step_receives_summary_and_step_outcomes(steps_by_id: dict[str, dict]) -> None:
    notify_run = steps_by_id["notify"]["run"]
    assert "python3 -m tools.cloud.notify_discord" in notify_run
    for flag in (
        "--summary-path",
        "--batch-exit-code",
        "--local-export",
        "--smoke-outcome",
        "--setup-outcome",
        "--sync-outcome",
        "--pull-outcome",
        "--upload-machine-outcome",
        "--upload-serving-outcome",
        "--run-started-at",
        "--output",
    ):
        assert flag in notify_run, f"notify step missing {flag}"
    assert "steps.smoke.outcome" in notify_run
    assert "steps.setup.outcome" in notify_run
    assert "steps.upload-machine.outcome" in notify_run
    assert "steps.batch.outputs.exit_code" in notify_run
    assert "steps.batch.outputs.local_export" in notify_run


def test_notify_step_does_not_interpolate_dispatch_input_into_the_run_block(
    steps_by_id: dict[str, dict],
) -> None:
    # Script-injection guard: workflow_dispatch input must reach the notify step
    # via the MANUAL_ASOF env var, never as a ${{ inputs.* }} expression in run:.
    notify_run = steps_by_id["notify"]["run"]
    assert "inputs.asof" not in notify_run
    assert "${{ inputs." not in notify_run
    assert '--asof "$MANUAL_ASOF"' in notify_run


if __name__ == "__main__":
    unittest.main()
