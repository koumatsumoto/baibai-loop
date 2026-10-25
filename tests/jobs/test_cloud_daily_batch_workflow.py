from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

from baibai_engine.market.jpx_sources import JPX_REGULATION_URLS
from baibai_engine.machine_analysis.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cloud-daily-batch.yml"


def test_common_integration_runs_at_2243_jst_including_weekends() -> None:
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "43 13 * * *"}]


def test_common_integration_has_no_provider_acquisition_credentials_or_cutoff():
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["daily"]
    assert int(job["timeout-minutes"]) < 120
    assert "needs" not in job
    text = str(job)
    for key in ("JQUANTS_API_KEY", "EDINET_API_KEY", "BAIBAI_ACQUISITION_DEADLINE", "ESTAT_APP_ID"):
        assert key not in text


def _daily_batch_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    batch = next(step for step in workflow["jobs"]["daily"]["steps"] if step.get("id") == "batch")
    return {str(name): str(value) for name, value in batch["env"].items()}


def test_fixed_jpx_sources_cover_daily_batch_rules_without_step_env() -> None:
    env = _daily_batch_env()
    rules = load_screening_rules(ROOT / DEFAULT_RULES_PATH)

    required_sources = set(rules.universe.required_jpx_flags)
    assert required_sources
    assert required_sources <= set(JPX_REGULATION_URLS)
    assert not any(name.startswith("JPX_") for name in env)


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
    # Pin the smoke import; this textual check alone does not prove stdlib-only use.
    assert "import baibai_jobs.observability.discord" in smoke_run


def test_dispatch_input_is_validated_after_smoke_and_before_setup(
    steps: list[dict], steps_by_id: dict[str, dict]
) -> None:
    ids = [step.get("id") for step in steps]
    assert ids.index("smoke") < ids.index("validate-input") < ids.index("setup")
    validation = steps_by_id["validate-input"]
    assert validation["env"] == {
        "MANUAL_ASOF": "${{ inputs.asof }}",
        "PYTHONPATH": "jobs/src",
    }
    assert '--asof "$MANUAL_ASOF"' in validation["run"]
    assert 'echo "asof=$MANUAL_ASOF" >> "$GITHUB_OUTPUT"' in validation["run"]


def test_batch_step_invokes_stable_batch_entrypoint(steps_by_id: dict[str, dict]) -> None:
    batch_run = steps_by_id["batch"]["run"]
    assert "uv run baibai-jobs daily" in batch_run


def test_scheduled_trigger_uses_cron_target_mode(steps_by_id: dict[str, dict]) -> None:
    batch_run = steps_by_id["batch"]["run"]

    assert 'if [[ "$GITHUB_EVENT_NAME" == "schedule" ]]' in batch_run
    assert "args+=(--scheduled)" in batch_run
    assert 'args+=(--asof "$MANUAL_ASOF")' not in batch_run


def test_daily_uses_saved_economic_inputs_and_has_no_browser_setup(steps_by_id):
    assert "--saved-inputs" in steps_by_id["batch"]["run"]
    assert "ESTAT_APP_ID" not in steps_by_id["batch"]["env"]
    assert "browser-smoke" not in steps_by_id


def test_batch_step_writes_the_notice_and_finalizes_outputs_before_fatal_exit(
    steps_by_id: dict[str, dict],
) -> None:
    batch_run = steps_by_id["batch"]["run"]
    assert "--notice-output" in batch_run
    # Outputs are echoed before the fatal exit so the notification step sees them.
    assert batch_run.index('echo "exit_code=$code"') < batch_run.index('exit "$code"')
    assert 'echo "export_ready=true"' in batch_run
    assert 'echo "export_ready=false"' in batch_run


def test_upload_steps_require_a_complete_local_export(steps_by_id: dict[str, dict]) -> None:
    assert steps_by_id["upload-stores"]["if"] == "steps.batch.outputs.export_ready == 'true'"


def test_the_durable_record_waits_for_the_store_and_the_views(
    steps_by_id: dict[str, dict],
) -> None:
    """Retained `history/` records past runs; `meta.json` is the freshness claim.

    Publishing either before the store holding this run is persisted would leave a
    permanent record of a run the store does not contain.
    """
    assert steps_by_id["publish-serving"]["if"] == "steps.upload-stores.outcome == 'success'"


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
def test_failed_upload_reports_completed_and_skipped_stages(
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
    # This executes the upload shell, not GitHub's expression engine. The declared
    # tail gate only permits a successful upload step, so these failures hold it back.
    assert steps_by_id["publish-serving"]["if"] == "steps.upload-stores.outcome == 'success'"
    assert "publish-serving-tail" in steps_by_id["publish-serving"]["run"]
    assert "publish-serving-tail" not in steps_by_id["upload-stores"]["run"]


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
    assert (
        steps_by_id["notify"]["name"]
        == "Notify Discord #batch-runs for ${{ steps.batch.outputs.asof || 'unknown' }}"
    )


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
    # The generic drift gate enforces step-scoped credentials, not which step
    # owns each credential. This exact mapping detects unintended expansion
    # of the workflow's credential-bearing surface.
    assert holders == {
        "lease-acquire": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "pull": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "hydrate": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "source-plan": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "source-integration": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "source-seal": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "source-inputs": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "publish-lake": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "upload-stores": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "publish-serving": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
        "lease-release": {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"},
    }


def test_notify_step_receives_the_notice_and_every_tracked_step_outcome(
    steps_by_id: dict[str, dict],
) -> None:
    notify_run = steps_by_id["notify"]["run"]
    assert "python3 -m baibai_jobs.observability.discord" in notify_run
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


def test_common_integration_has_no_tradingview_acquisition_step_or_notice() -> None:
    workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["on"]["schedule"] == [{"cron": "43 13 * * *"}]
    steps = workflow["jobs"]["daily"]["steps"]
    assert "tradingview" not in {step.get("id") for step in steps}
    notify = next(step for step in steps if step.get("id") == "notify")
    assert "--tradingview-outcome" not in notify["run"]


def test_indicator_only_publication_keeps_the_existing_l1_release(
    steps_by_id: dict[str, dict],
) -> None:
    assert steps_by_id["publish-lake"]["if"] == (
        "steps.batch.outputs.export_ready == 'true' && steps.batch.outputs.market_updated == 'true'"
    )
    assert "market_updated=" in steps_by_id["batch"]["run"]
    # The one existing complete store/view publication path also handles holidays.
    assert steps_by_id["upload-stores"]["if"] == "steps.batch.outputs.export_ready == 'true'"


@pytest.mark.parametrize(
    ("notice_case", "expected_publish", "expected_warning"),
    [
        ("missing", True, True),
        ("unreadable", True, True),
        ("invalid_json", True, True),
        ("invalid_field", True, True),
        ("unknown_change", True, True),
        ("false", True, False),
        ("true", True, False),
        ("invalid_asof", True, False),
    ],
)
def test_common_replay_forces_lake_publication_even_if_daily_observer_is_unchanged(
    tmp_path: Path,
    steps_by_id: dict[str, dict],
    notice_case: str,
    expected_publish: bool,
    expected_warning: bool,
) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    notice = runner_temp / "batch-notice.json"
    private_marker = "private-notice-content-must-not-be-printed"
    if notice_case == "unreadable":
        notice.mkdir()  # read_text raises OSError even when tests run as root.
    elif notice_case == "invalid_json":
        notice.write_text(private_marker)
    elif notice_case == "invalid_field":
        notice.write_text('{"market_updated":"false","private":"' + private_marker + '"}')
    elif notice_case == "unknown_change":
        notice.write_text(json.dumps({"market_updated": None}))
    elif notice_case in {"false", "true"}:
        notice.write_text(
            json.dumps({"market_updated": notice_case == "true", "asof": "2026-10-14"})
        )
    elif notice_case == "invalid_asof":
        notice.write_text(json.dumps({"market_updated": False, "asof": "2026-13-40"}))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nmkdir -p "$RUNNER_TEMP/serving/views"\n'
        'printf "{}\\n" > "$RUNNER_TEMP/serving/views/meta.json"\n'
    )
    uv.chmod(0o755)
    github_output = tmp_path / "outputs"
    recorded = tmp_path / "lake-commands"
    transfer = tmp_path / "jobs/scripts/r2_transfer.sh"
    transfer.parent.mkdir(parents=True)
    transfer.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$RECORDED"\nprintf "{}\\n"\n')
    transfer.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(github_output),
        "GITHUB_EVENT_NAME": "schedule",
        "MANUAL_ASOF": "",
        "INTEGRATION_ASOF": "2026-10-14",
        "RECORDED": str(recorded),
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", steps_by_id["batch"]["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert private_marker not in result.stdout + result.stderr
    assert result.stderr == (
        "::warning::Daily notice unavailable or invalid; continuing L1 publication.\n"
        if expected_warning
        else ""
    )
    outputs = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
    assert outputs["exit_code"] == "0"
    assert outputs["export_ready"] == "true"
    assert outputs["market_updated"] == str(expected_publish).lower()
    assert outputs["asof"] == ("2026-10-14" if notice_case in {"false", "true"} else "unknown")
    if outputs["export_ready"] == "true" and outputs["market_updated"] == "true":
        publish = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", steps_by_id["publish-lake"]["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert publish.returncode == 0
    assert recorded.exists() is expected_publish
    if expected_publish:
        assert recorded.read_text().splitlines() == ["publish-lake"]


@pytest.mark.parametrize(("event", "scheduled"), [("schedule", True), ("workflow_dispatch", False)])
def test_workflow_constructed_target_and_gate_arguments_are_accepted(
    tmp_path, steps_by_id, event, scheduled
):
    from baibai_jobs.jobs.daily import build_parser

    prefix = steps_by_id["batch"]["run"].split("set +e", 1)[0]
    prefix = prefix.replace("${{ steps.source-plan.outputs.asof }}", "2026-10-24")
    output = tmp_path / "argv"
    script = (
        prefix
        + """
python3 -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))' "$ARGS_PATH" "${args[@]}"
"""
    )
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event,
            "MANUAL_ASOF": "2026-10-24",
            "INTEGRATION_ASOF": "2026-10-24",
            "RUNNER_TEMP": str(tmp_path),
            "ARGS_PATH": str(output),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(output.read_text())
    assert argv.count("--asof") == 1
    parsed = build_parser().parse_args(argv)
    assert parsed.asof == "2026-10-24"
    assert parsed.scheduled is scheduled
    assert parsed.saved_inputs is True


def test_integration_date_uses_original_run_and_manual_clock():
    from datetime import UTC, date, datetime

    from baibai_jobs.jobs.schedule import integration_target_date

    original = datetime(2026, 10, 23, 13, 43, tzinfo=UTC)
    assert integration_target_date(original, scheduled=True) == date(2026, 10, 23)
    morning = datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
    assert integration_target_date(morning, scheduled=False) == date(2026, 10, 24)
    assert integration_target_date(morning, scheduled=True) == date(2026, 10, 23)
