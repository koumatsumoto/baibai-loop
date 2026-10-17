import asyncio
import datetime as datetime_module
import json
import os
import shutil
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
        "37 6,8,10,12,14 * * 1-5",
        "37 0,4,8 * * 0,6",
    ]
    assert set(data["on"]["workflow_dispatch"]["inputs"]) == {
        "interval",
        "observation_master_asof",
        "observations_json",
        "observations_sha256",
    }
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
        "lease-acquire",
        "pull",
        "hydrate",
        "preflight",
        "cooldown-check",
        "master",
        "tradingview",
        "publish-lake",
        "cooldown-save",
        "lease-release",
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


def test_partial_publication_precedes_failed_cooldown_and_preserves_simulated_pull(tmp_path):
    from tests.engine.test_tradingview_collector import DAY, NOW, _sdk, payload, store, stored

    from baibai_engine.external_assessment.tradingview.collector import collect
    from baibai_engine.external_assessment.tradingview.observations import COLUMNS, FetchError

    local = store(tmp_path / "local.sqlite", 51)
    published = tmp_path / "published.sqlite"
    requests = []

    async def fetch(symbols, columns):
        assert columns == COLUMNS
        requests.append(symbols)
        if len(requests) == 2:
            raise FetchError("fixture provider failure after one valid chunk")
        return _sdk(payload(symbols))

    async def sleep(_interval):
        pass

    report = asyncio.run(collect(local, DAY, fetch, clock=lambda: NOW, sleep=sleep))
    assert report["status"] == "partial"
    assert report["rows_added"] == 50
    expected = stored(local)
    transfer = tmp_path / "batch/scripts/r2_transfer.sh"
    transfer.parent.mkdir(parents=True)
    transfer.write_text(
        '#!/usr/bin/env bash\nprintf "publish-lake\\n" >> "$CALLS"\ncp "$LOCAL" "$PUBLISHED"\nprintf "{}\\n"\n'
    )
    transfer.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\nif [[ "$*" == *tradingview_cooldown* ]]; then\n  printf "cooldown-save\\n" >> "$CALLS"\n  echo "TradingView cooldown failed; type=CooldownError" >&2\n  exit 1\nfi\nprintf "lease-release\\n" >> "$CALLS"\n'
    )
    uv.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path),
        "LOCAL": str(local),
        "PUBLISHED": str(published),
        "CALLS": str(calls),
    }
    outcomes = {}
    failed = False
    # Execute the workflow scripts and order; publication, cooldown and lease are stubbed.
    # A condition without a status function has GitHub's implicit success() gate.
    for step in workflow()["jobs"]["snapshot"]["steps"]:
        if step.get("id") not in {"publish-lake", "cooldown-save", "lease-release"}:
            continue
        if failed and "always()" not in step["if"]:
            outcomes[step["id"]] = "skipped"
            continue
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True
        )
        outcomes[step["id"]] = "failure" if result.returncode else "success"
        failed = failed or result.returncode != 0
        if step["id"] == "cooldown-save":
            assert "CooldownError" in result.stderr
    assert outcomes == {
        "publish-lake": "success",
        "cooldown-save": "failure",
        "lease-release": "success",
    }
    assert calls.read_text().splitlines() == ["publish-lake", "cooldown-save", "lease-release"]
    # A simulated next pull retains every originally committed column/time.
    shutil.copyfile(published, local)
    assert stored(local) == expected


def test_gates_secret_scopes_and_durability():
    data = workflow()
    steps = {step["id"]: step for step in data["jobs"]["snapshot"]["steps"] if "id" in step}
    for name in (
        "setup",
        "setup-uv",
        "sync",
        "duckdb-httpfs",
        "lease-acquire",
        "pull",
        "hydrate",
        "preflight",
    ):
        assert steps[name]["if"] == "steps.target.outputs.eligible == 'true'"
    assert steps["master"]["if"] == (
        "steps.cooldown-check.outputs.allowed == 'true' && steps.preflight.outputs.status == 'needs_fetch' && steps.preflight.outputs.needs_master == 'true'"
    )
    assert (
        steps["tradingview"]["if"]
        == "steps.cooldown-check.outputs.allowed == 'true' && steps.preflight.outputs.status == 'needs_fetch' && (steps.preflight.outputs.needs_master == 'false' || steps.master.outcome == 'success')"
    )
    assert "steps.tradingview.outputs.status == 'partial'" in steps["publish-lake"]["if"]
    assert "rows_added" in steps["publish-lake"]["if"]
    assert steps["cooldown-check"]["if"] == "steps.preflight.outputs.status == 'needs_fetch'"
    assert "always()" in steps["cooldown-save"]["if"]
    assert "steps.lease-acquire.outputs.acquired == 'true'" in steps["cooldown-save"]["if"]
    assert "steps.tradingview.outcome != 'skipped'" in steps["cooldown-save"]["if"]
    assert "always()" in steps["lease-release"]["if"]
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
    for name in (
        "lease-acquire",
        "pull",
        "hydrate",
        "cooldown-check",
        "cooldown-save",
        "publish-lake",
        "lease-release",
    ):
        assert "R2_ACCESS_KEY_ID" in str(steps[name].get("env"))
    for name, step in steps.items():
        if name != "notify":
            assert "DISCORD_WEBHOOK_URL" not in str(step)
        if name != "master":
            assert "JQUANTS_API_KEY" not in str(step)
        if name != "tradingview":
            assert "TRADINGVIEW_OAUTH_STATE" not in str(step)
        if name not in (
            "lease-acquire",
            "pull",
            "hydrate",
            "cooldown-check",
            "cooldown-save",
            "publish-lake",
            "lease-release",
        ):
            assert "R2_ACCESS_KEY_ID" not in str(step)


@pytest.mark.parametrize(
    ("basis", "event", "interval", "valid", "eligible"),
    [
        ("", "workflow_dispatch", "15", True, "true"),
        ("", "schedule", "", True, "true"),
        ("2026-10-09", "workflow_dispatch", "15", True, "true"),
        ("2026-10-12", "workflow_dispatch", "15", False, None),
        ("2026-10-09", "schedule", "15", False, None),
        ("2026-10-09", "workflow_dispatch", "14", False, None),
    ],
)
def test_actual_target_script_keeps_normal_guard_and_validates_manual_observation_before_lease(
    tmp_path, monkeypatch, basis, event, interval, valid, eligible
):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "target")
    code = step["run"].split("<<'PYTARGET'\n", 1)[1].rsplit("\nPYTARGET", 1)[0]

    class Clock(datetime_module.datetime):
        @classmethod
        def now(cls, tz):
            return cls(2026, 10, 11, 1, tzinfo=datetime_module.UTC).astimezone(tz)

    monkeypatch.setattr(datetime_module, "datetime", Clock)
    output = tmp_path / "output"
    for key, value in {
        "GITHUB_OUTPUT": str(output),
        "EVENT_NAME": event,
        "EVENT_SCHEDULE": "37 6,8,10,12,14 * * 1-5",
        "OBSERVATION_MASTER_ASOF": basis,
        "INTERVAL": interval,
    }.items():
        monkeypatch.setenv(key, value)
    if not valid:
        with pytest.raises(ValueError, match="observation"):
            exec(code, {})
        assert not output.exists()
        return
    exec(code, {})
    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert fields["target_date"] == "2026-10-11"
    assert fields["master_asof"] == basis
    assert fields["eligible"] == eligible
    assert fields["slot"] == (
        "manual-observation"
        if basis
        else "manual"
        if event == "workflow_dispatch"
        else "post-close-2h"
    )


def test_acquisition_shell_passes_explicit_master_without_changing_observation_date(tmp_path):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "tradingview")
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then printf "0 0\\n"; exit 0; fi\nprintf \'%s\\n\' "$@" > "$RUNNER_TEMP/arguments"\nprintf \'%s\\n\' \'{"status":"already_saved"}\'\n'
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "TARGET_DATE": "2026-10-11",
            "MASTER_ASOF": "2026-10-09",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    args = (tmp_path / "arguments").read_text().splitlines()
    assert args[args.index("--asof") + 1] == "2026-10-11"
    assert args[args.index("--master-asof") + 1] == "2026-10-09"
    assert "--batch-size" not in args
    assert args[args.index("--interval") + 1] == "15"


@pytest.mark.parametrize("status", ["saved", "partial", "already_saved", "skipped_historical_asof"])
def test_acquisition_shell_parses_only_known_success_status(tmp_path, status):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then echo "0 0"; exit 0; fi\nprintf \'%s\\n\' \''
        + json.dumps(
            {
                "status": status,
                "rows_added": 2,
                "rows": 2,
                "expected_universe": 3 if status == "partial" else 2,
                "remaining": 1 if status == "partial" else 0,
                "unresolved": 0,
                "stop_reason": "provider_rate_limit",
            }
        )
        + "'\n"
    )
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
    assert f"status={status}" in output.read_text().splitlines()
    if status in {"saved", "partial"}:
        assert "rows_added=2" in output.read_text()


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        '{"status":"unknown"}',
        '{"status":[]}',
        '{"status":"partial","rows_added":0,"rows":50,"expected_universe":51,"remaining":1,"unresolved":0}',
        '{"status":"partial","rows_added":1,"rows":50,"expected_universe":49,"remaining":-1,"unresolved":0}',
    ],
)
def test_acquisition_rejects_unrecognized_success_output(tmp_path, body):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then echo "0 0"; exit 0; fi\nprintf \'%s\\n\' \''
        + body
        + "'\n"
    )
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
        f'#!/bin/sh\nif [ "$3" = "-m" ]; then echo "0 0"; exit 0; fi\n'
        f"echo 'TradingView acquisition failed; category=provider_rate_limit' >&2\nexit {exit_code}\n"
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


def test_receipt_delta_on_failed_acquisition_qualifies_only_l1_publication(tmp_path):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then '
        'if [ -f "$RUNNER_TEMP/count-seen" ]; then echo "1 0"; '
        'else touch "$RUNNER_TEMP/count-seen"; echo "0 0"; fi; exit 0; fi\n'
        "echo 'TradingView acquisition failed; category=provider_response' >&2\nexit 1\n"
    )
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
    assert "failure_category=provider_response" in output.read_text()
    assert "publish_failed_receipts=true" in output.read_text()
    gate = steps["publish-lake"]["if"]
    assert "always()" in gate
    assert "!cancelled()" in gate
    for guard in ("lease-acquire", "pull.outcome", "hydrate.outcome", "tradingview.outcome"):
        assert guard in gate
    assert "publish_failed_receipts == 'true'" in gate
    assert "always()" in steps["cooldown-save"]["if"]
    assert "always()" in steps["lease-release"]["if"]


@pytest.mark.parametrize(
    ("category", "after", "count_fails"),
    [
        ("internal", "1 0", False),
        ("storage", "1 0", False),
        ("unknown", "1 0", False),
        ("provider_response", "0 0", False),
        ("provider_response", "1 1", False),
        ("provider_response", "1 0", True),
    ],
)
def test_failed_acquisition_without_publishable_raw_only_delta(
    tmp_path, category, after, count_fails
):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "tradingview")
    uv = tmp_path / "uv"
    second = "exit 1" if count_fails else f"echo '{after}'"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then '
        f'if [ -f "$RUNNER_TEMP/count-seen" ]; then {second}; '
        'else touch "$RUNNER_TEMP/count-seen"; echo "0 0"; fi; exit 0; fi\n'
        f"echo 'TradingView acquisition failed; category={category}' >&2\nexit 1\n"
    )
    uv.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
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
    assert "publish_failed_receipts=false" in output.read_text()


def test_failed_initial_count_does_not_start_acquisition(tmp_path):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "tradingview")
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$3" = "-m" ]; then exit 1; fi\ntouch "$RUNNER_TEMP/refresh-started"\n'
    )
    uv.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
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
    assert not (tmp_path / "refresh-started").exists()
    assert not output.exists()


def test_summary_preserves_partial_failure_and_null_counts(tmp_path):
    steps = {step["id"]: step for step in workflow()["jobs"]["snapshot"]["steps"] if "id" in step}
    (tmp_path / "tradingview-progress.json").write_text(
        json.dumps({"requests_attempted": 2, "symbols_requested": 100, "chunks_completed": 1})
    )
    (tmp_path / "tradingview-result.json").write_text(
        json.dumps(
            {
                "status": "partial",
                "normal_nulls": {"analyst_rating": 3},
                "last_failure": "TradingView acquisition failed; category=provider_rate_limit; mcp_http_status=429; retry_not_before_utc=2026-10-11T18:00:00+00:00",
            }
        )
    )
    summary = tmp_path / "summary"
    subprocess.run(
        ["bash", "-e", "-c", steps["summary"]["run"]],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "TRADINGVIEW": "partial",
            "MASTER_ASOF": "2026-10-09",
            "ACQUISITION_OUTCOME": "success",
            "PREFLIGHT_ROWS": "50",
        },
        check=True,
        capture_output=True,
    )
    text = summary.read_text()
    assert "| requests_attempted | 2 |" in text
    assert "| mcp_http_status | 429 |" in text
    assert "| retry_not_before_utc | 2026-10-11T18:00:00+00:00 |" in text
    assert "analyst_rating" in text
    assert "| preflight_saved_rows | 50 |" in text
    assert "| master_asof | 2026-10-09 |" in text


@pytest.mark.parametrize(
    ("outcome", "count"),
    [("skipped", "0"), ("failure", "unavailable"), ("cancelled", "unavailable")],
)
def test_summary_does_not_call_missing_failure_progress_zero_requests(tmp_path, outcome, count):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "summary")
    summary = tmp_path / "summary"
    subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "ACQUISITION_OUTCOME": outcome,
            "TRADINGVIEW": "",
        },
        check=True,
        capture_output=True,
    )
    assert f"| requests_attempted | {count} |" in summary.read_text()


@pytest.mark.parametrize(
    ("status", "expected", "remaining"), [("already_saved", 3, 0), ("needs_fetch", 5, 2)]
)
def test_skipped_acquisition_summary_keeps_stored_quality(tmp_path, status, expected, remaining):
    step = next(s for s in workflow()["jobs"]["snapshot"]["steps"] if s.get("id") == "summary")
    (tmp_path / "tradingview-preflight.json").write_text(
        json.dumps(
            {
                "status": status,
                "is_business_day": False,
                "rows": 3,
                "expected_universe": expected,
                "remaining": remaining,
                "unresolved": 1,
                "normal_nulls": {"price_target_average": 2},
                "first_fetched_at_utc": "2026-10-11T00:00:00+00:00",
                "last_fetched_at_utc": "2026-10-11T00:01:00+00:00",
            }
        )
    )
    summary = tmp_path / "summary"
    subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
            "ACQUISITION_OUTCOME": "skipped",
            "PREFLIGHT": status,
            "COOLDOWN_ALLOWED": "false" if status == "needs_fetch" else "",
            "MASTER_ASOF": "2026-10-09",
        },
        check=True,
        capture_output=True,
    )
    text = summary.read_text()
    for name, value in {
        "rows": 3,
        "expected_universe": expected,
        "remaining": remaining,
        "unresolved": 1,
        "requests_attempted": 0,
    }.items():
        assert f"| {name} | {value} |" in text
    assert "price_target_average" in text
    assert "| last_fetched_at_utc | 2026-10-11T00:01:00+00:00 |" in text
