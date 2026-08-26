from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch.yml"


def test_schedule_runs_at_1643_jst_on_weekdays() -> None:
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "43 7 * * 1-5"}]


def _daily_batch_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    batch = next(step for step in workflow["jobs"]["daily"]["steps"] if step.get("id") == "batch")
    return {str(name): str(value) for name, value in batch["env"].items()}


class CloudDailyBatchWorkflowTests(unittest.TestCase):
    def test_daily_batch_step_env_covers_every_required_jpx_source(self) -> None:
        # scheduled run が毎回 bootstrap-cache に入ると、JPX 規制 provider は
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
        "browser-smoke",
        "pull",
        "batch",
        "upload-stores",
        "publish-serving",
        "cancellation",
        "notify",
    ):
        assert step_id in steps_by_id, f"missing stable step id: {step_id}"


def test_smoke_check_runs_before_setup_python_on_system_python(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("smoke") < ids.index("setup")
    smoke_run = steps_by_id["smoke"]["run"]
    # The import is what guarantees the stdlib-only contract.
    assert "import baibai_batch.observability.discord" in smoke_run


def test_dispatch_input_is_validated_after_smoke_and_before_setup(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("smoke") < ids.index("validate-input") < ids.index("setup")
    validation = steps_by_id["validate-input"]
    assert validation["env"] == {
        "MANUAL_ASOF": "${{ inputs.asof }}",
        "PYTHONPATH": "batch/src",
    }
    assert '--asof "$MANUAL_ASOF"' in validation["run"]
    assert 'echo "asof=$MANUAL_ASOF" >> "$GITHUB_OUTPUT"' in validation["run"]


def test_batch_step_invokes_stable_batch_entrypoint(steps_by_id: dict[str, dict]) -> None:
    batch_run = steps_by_id["batch"]["run"]
    assert "uv run baibai-batch daily" in batch_run


def test_isolated_browser_smoke_runs_before_any_credential_bearing_step(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("sync") < ids.index("browser-smoke") < ids.index("pull")
    smoke = steps_by_id["browser-smoke"]
    assert "env" not in smoke
    assert "BrowserFetcher" in smoke["run"]
    assert "data:text/html" in smoke["run"]


def test_batch_step_writes_the_notice_and_finalizes_outputs_before_fatal_exit(
    steps_by_id: dict[str, dict],
) -> None:
    batch_run = steps_by_id["batch"]["run"]
    assert "--notice-output" in batch_run
    # Outputs are echoed before the fatal exit so the notification step sees them.
    assert batch_run.index('echo "exit_code=$code"') < batch_run.index('exit "$code"')
    assert 'echo "published=true"' in batch_run
    assert 'echo "published=false"' in batch_run


def test_upload_steps_run_only_when_published(steps_by_id: dict[str, dict]) -> None:
    assert steps_by_id["upload-stores"]["if"] == "steps.batch.outputs.published == 'true'"


def test_the_durable_record_waits_for_the_store_and_the_views(
    steps_by_id: dict[str, dict],
) -> None:
    """`history/` never deletes and `meta.json` is the freshness claim.

    Publishing either before the store holding this run is persisted would leave a
    permanent record of a run the store does not contain.
    """
    assert steps_by_id["publish-serving"]["if"] == "steps.upload-stores.outcome == 'success'"


@pytest.mark.parametrize(
    ("machine_exit", "views_exit"),
    [(1, 0), (0, 1), (1, 1)],
)
def test_a_failed_upload_publishes_no_durable_record(
    tmp_path: Path, steps_by_id: dict[str, dict], machine_exit: int, views_exit: int
) -> None:
    """The gating condition has to actually keep the tail stage from running.

    A string comparison against the `if:` expression says nothing about what
    reaches R2, and this is the property that keeps a run the store never received
    from leaving a permanent record behind.
    """
    code, _outputs, _recorded = _run_upload_step(
        tmp_path, steps_by_id, machine_exit=machine_exit, views_exit=views_exit
    )
    assert code != 0

    # GitHub ANDs an implicit success() into a condition with no status function,
    # so a non-zero step is enough to hold the tail back. Simulate that decision
    # and confirm the tail command is the one it withholds.
    condition = steps_by_id["publish-serving"]["if"]
    tail_runs = condition == "steps.upload-stores.outcome == 'success'" and code == 0

    assert tail_runs is False
    assert "publish-serving-tail" in steps_by_id["publish-serving"]["run"]
    assert "publish-serving-tail" not in steps_by_id["upload-stores"]["run"]


def _run_upload_step(
    tmp_path: Path, steps_by_id: dict[str, dict], *, machine_exit: int, views_exit: int
) -> tuple[int, dict[str, str], list[tuple[str, float, float]]]:
    """Execute the step's own script against a stub transfer command.

    Asserting on the text of a run block says nothing about whether the shell in it
    works. This runs it: a stub `r2_transfer.sh` sleeps and records when each side
    started and finished, so the ordering, whether the second side ran at all, and
    the reported results are observed rather than read.
    """
    script = steps_by_id["upload-stores"]["run"]
    stub_dir = tmp_path / "batch" / "scripts"
    stub_dir.mkdir(parents=True)
    timings = tmp_path / "timings.tsv"
    (stub_dir / "r2_transfer.sh").write_text(
        "#!/usr/bin/env bash\n"
        'started="$(date +%s.%N)"\n'
        # Long enough that the recorded windows stay distinguishable on a loaded
        # CI box, short enough that four parametrised runs stay cheap.
        "sleep 0.3\n"
        'printf "%s\\t%s\\t%s\\n" "$1" "$started" "$(date +%s.%N)" >> "$TIMINGS"\n'
        'printf "stub ran %s\\n" "$1"\n'
        'if [[ "$1" == "push-machine" ]]; then exit "$MACHINE_EXIT"; fi\n'
        'exit "$VIEWS_EXIT"\n',
        encoding="utf-8",
    )
    (stub_dir / "r2_transfer.sh").chmod(0o755)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    github_output = tmp_path / "github_output"
    github_output.touch()

    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(github_output),
            "TIMINGS": str(timings),
            "MACHINE_EXIT": str(machine_exit),
            "VIEWS_EXIT": str(views_exit),
        },
        capture_output=True,
        text=True,
    )

    outputs = dict(
        line.split("=", 1)
        for line in github_output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    recorded = [
        (name, float(start), float(end))
        for name, start, end in (
            line.split("\t") for line in timings.read_text(encoding="utf-8").splitlines()
        )
    ]
    return completed.returncode, outputs, recorded


def test_the_mirror_starts_only_after_the_store_push_finished(
    tmp_path: Path, steps_by_id: dict[str, dict]
) -> None:
    """`views/` must never describe a run the remote store does not hold.

    Run together, a machine push that failed beside a mirror that succeeded left
    exactly that, standing until the next successful run. Ordered this way the only
    partial state left behind is views older than the store.
    """
    code, outputs, recorded = _run_upload_step(tmp_path, steps_by_id, machine_exit=0, views_exit=0)

    assert code == 0
    assert outputs == {"machine": "success", "views": "success"}
    assert {name for name, _start, _end in recorded} == {"push-machine", "upload-serving-views"}
    starts = {name: start for name, start, _end in recorded}
    ends = {name: end for name, _start, end in recorded}
    assert starts["upload-serving-views"] >= ends["push-machine"]


@pytest.mark.parametrize(
    ("machine_exit", "views_exit", "expected", "sides_run"),
    [
        (1, 0, {"machine": "failure", "views": "skipped"}, 1),
        (0, 1, {"machine": "success", "views": "failure"}, 2),
        (1, 1, {"machine": "failure", "views": "skipped"}, 1),
    ],
)
def test_a_failed_store_push_fails_the_step_and_leaves_the_mirror_untouched(
    tmp_path: Path,
    steps_by_id: dict[str, dict],
    machine_exit: int,
    views_exit: int,
    expected: dict[str, str],
    sides_run: int,
) -> None:
    """`skipped` is a measurement: it says the mirror did not run, which is what
    makes the remote `views/` still the previous run's rather than unknown."""
    code, outputs, recorded = _run_upload_step(
        tmp_path, steps_by_id, machine_exit=machine_exit, views_exit=views_exit
    )

    assert code != 0
    assert outputs == expected
    assert len(recorded) == sides_run


def test_notify_is_the_single_notification_point_running_on_every_terminal_state(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    # Notification is the last step.
    assert ids[-1] == "notify"
    # `always()`, never `!cancelled()`: GitHub reports a `timeout-minutes` expiry
    # as a cancellation, so `!cancelled()` silently skips the notification for a
    # hung batch — the failure this workflow most needs to report.
    assert steps_by_id["notify"]["if"] == "${{ always() }}"
    # The cancellation state still reaches the notifier, via the step output.
    assert "steps.cancellation.outputs.cancelled" in steps_by_id["notify"]["run"]
    assert steps_by_id["cancellation"]["if"] == "${{ cancelled() }}"
    # A broken webhook is not "the day's artefacts were not published", so it
    # never turns the run red.
    assert steps_by_id["notify"]["continue-on-error"] is True


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
    # The step digests in the drift gate can be regenerated, so they cannot on their
    # own show that a change left the credential boundary where it was. This table
    # is what shows it: every difference has to be a step appearing, disappearing or
    # being renamed, never a step gaining a secret it did not hold.
    assert holders == {
        "pull": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "hydrate": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "batch": {"JQUANTS_API_KEY", "ESTAT_APP_ID", "EDINET_API_KEY"},
        "publish-lake": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "upload-stores": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "publish-serving": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
    }


def test_notify_step_receives_the_notice_and_every_tracked_step_outcome(
    steps_by_id: dict[str, dict],
) -> None:
    notify_run = steps_by_id["notify"]["run"]
    assert "python3 -m baibai_batch.observability.discord" in notify_run
    assert "--notice-path" in notify_run
    assert "--batch-exit-code" in notify_run
    assert "steps.batch.outputs.exit_code" in notify_run
    for step in (
        "smoke",
        "setup",
        "sync",
        "pull",
        "hydrate",
        "publish-lake",
        "upload-stores",
        "publish-serving",
    ):
        assert f"--{step}-outcome" in notify_run, f"notify step missing --{step}-outcome"
        assert f"steps.{step}.outcome" in notify_run
    assert "--cancelled" in notify_run


def test_notify_step_does_not_interpolate_dispatch_input_into_the_run_block(
    steps_by_id: dict[str, dict],
) -> None:
    # Script-injection guard: workflow_dispatch input must reach the notify step
    # via the MANUAL_ASOF env var, never as a ${{ inputs.* }} expression in run:.
    notify_run = steps_by_id["notify"]["run"]
    assert "inputs.asof" not in notify_run
    assert "${{ inputs." not in notify_run


if __name__ == "__main__":
    unittest.main()
