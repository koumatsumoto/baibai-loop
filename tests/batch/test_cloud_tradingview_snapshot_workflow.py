import os
import subprocess
from pathlib import Path

import pytest
import yaml

PATH = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-tradingview-snapshot.yml"


def workflow():
    return yaml.load(PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_metadata_order_and_exclusive_writer():
    data = workflow()
    assert [item["cron"] for item in data["on"]["schedule"]] == [
        "57 6 * * 1-5",
        "7 9 * * 1-5",
        "17 11 * * 1-5",
    ]
    assert data["on"]["workflow_dispatch"] == ""
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "cloud-publish",
        "queue": "max",
        "cancel-in-progress": "false",
    }
    job = data["jobs"]["snapshot"]
    assert job["timeout-minutes"] == "60"
    assert "env" not in job
    steps = job["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7",
    ]
    assert [step["id"] for step in steps if "id" in step] == [
        "smoke",
        "target",
        "setup",
        "setup-uv",
        "sync",
        "duckdb-httpfs",
        "pull",
        "hydrate",
        "preflight",
        "master",
        "tradingview",
        "publish-lake",
        "cancellation",
        "summary",
        "notify",
    ]
    assert "PYTHONPATH" in steps[1]["env"]
    assert "python3" in steps[1]["run"]
    assert "python3" in steps[2]["run"]
    target = steps[2]
    assert "github.event_name" in target["env"]["EVENT_NAME"]
    assert "github.event.schedule" in target["env"]["EVENT_SCHEDULE"]
    assert "${{" not in target["run"]
    for field in (
        "target_date",
        "slot",
        "eligible",
        "reason",
        "actual_start_utc",
        "actual_start_jst",
    ):
        assert f'"{field}"' in target["run"]


def test_gates_secret_scopes_and_durability():
    data = workflow()
    steps = {step["id"]: step for step in data["jobs"]["snapshot"]["steps"] if "id" in step}
    for name in ("setup", "setup-uv", "sync", "duckdb-httpfs", "pull", "hydrate", "preflight"):
        assert steps[name]["if"] == "steps.target.outputs.eligible == 'true'"
    assert steps["master"]["if"] == "steps.preflight.outputs.status == 'needs_fetch'"
    assert (
        steps["tradingview"]["if"]
        == "steps.preflight.outputs.status == 'needs_fetch' && steps.master.outcome == 'success'"
    )
    assert steps["publish-lake"]["if"] == "steps.tradingview.outputs.status == 'saved'"
    for name in ("summary", "notify"):
        assert steps[name]["if"] == "${{ always() }}"
        assert steps[name]["continue-on-error"] == "true"
    assert "continue-on-error" not in steps["tradingview"]
    source = PATH.read_text(encoding="utf-8")
    assert "push-market" not in source
    assert "push-machine" not in source
    for forbidden in (
        "screening run",
        "review-set",
        "macro reading",
        "playwright",
        "upload-serving-views",
    ):
        assert forbidden not in source.lower()
    assert "timeout --kill-after=30s 30m" in steps["tradingview"]["run"]
    assert '2> >(tee "$stderr" >&2)' in steps["tradingview"]["run"]
    assert '--asof "$TARGET_DATE"' in steps["tradingview"]["run"]
    assert '--asof "$TARGET_DATE"' in steps["master"]["run"]
    assert "DISCORD_WEBHOOK_URL" in str(steps["notify"].get("env"))
    assert "JQUANTS_API_KEY" in str(steps["master"].get("env"))
    assert "TRADINGVIEW_OAUTH_STATE" in str(steps["tradingview"].get("env"))
    for name in ("pull", "hydrate", "publish-lake"):
        assert "R2_ACCESS_KEY_ID" in str(steps[name].get("env"))
    for name, step in steps.items():
        if name != "notify":
            assert "DISCORD_WEBHOOK_URL" not in str(step)
        if name != "master":
            assert "JQUANTS_API_KEY" not in str(step)
        if name != "tradingview":
            assert "TRADINGVIEW_OAUTH_STATE" not in str(step)
        if name not in ("pull", "hydrate", "publish-lake"):
            assert "R2_ACCESS_KEY_ID" not in str(step)


@pytest.mark.parametrize(
    "status", ["saved", "already_saved", "non_trading_day", "skipped_historical_asof"]
)
def test_acquisition_shell_parses_only_known_success_status(tmp_path, status):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text(f"#!/bin/sh\nprintf '%s\\n' '{{\"status\":\"{status}\"}}'\n")
    uv.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", steps["tradingview"]["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "TARGET_DATE": "2026-09-28",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert output.read_text().strip() == f"status={status}"


@pytest.mark.parametrize("body", ["not-json", '{"status":"unknown"}', '{"status":[]}'])
def test_acquisition_rejects_unrecognized_success_output(tmp_path, body):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\\n' '" + body + "'\n")
    uv.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", steps["tradingview"]["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "TARGET_DATE": "2026-09-28",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "failure_category=internal" in output.read_text()


@pytest.mark.parametrize(
    ("exit_code", "category"), [(1, "provider_rate_limit"), (124, "timeout"), (137, "timeout")]
)
def test_acquisition_failure_keeps_red_exit_and_safe_category(tmp_path, exit_code, category):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!/bin/sh\necho 'TradingView acquisition failed; category=provider_rate_limit' >&2\nexit {exit_code}\n"
    )
    uv.chmod(0o755)
    (tmp_path / "tradingview-progress.json").write_text("private-token")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", steps["tradingview"]["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "TARGET_DATE": "2026-09-28",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == exit_code
    assert f"failure_category={category}" in output.read_text()
    assert "TradingView progress: unavailable" in result.stdout
    assert "private-token" not in result.stdout + result.stderr
