import json

import pytest

from baibai_batch.observability import tradingview_discord as notify


def invoke(monkeypatch, *args):
    calls = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")

    def transport(url, body, timeout):
        calls.append((url, json.loads(body)))
        return 200

    assert notify.main(["--asof", "2026-09-28", *args], transport=transport) == 0
    return calls


def test_saved_is_ok_and_contains_run_url(monkeypatch):
    calls = invoke(
        monkeypatch,
        "--eligible",
        "true",
        "--snapshot-status",
        "saved",
        "--publish-lake-outcome",
        "success",
    )
    message = calls[0][1]["content"]
    assert message.startswith("[OK] as-of 2026-09-28 — TradingView snapshot saved")
    assert "https://github.com/example/repo/actions/runs/123" in message
    assert "Review Set" not in message


@pytest.mark.parametrize(
    ("github_actions", "expected"), [("true", "GitHub Actions"), (None, "Local")]
)
def test_executor_line_uses_runner_context(monkeypatch, github_actions, expected):
    if github_actions is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    message = invoke(
        monkeypatch,
        "--eligible",
        "true",
        "--snapshot-status",
        "saved",
        "--publish-lake-outcome",
        "success",
    )[0][1]["content"]
    assert message.splitlines()[1] == f"executor: {expected}"
    assert message.count("executor:") == 1


@pytest.mark.parametrize("step", notify.FAILED_STEPS)
def test_non_acquisition_failures_are_failed(monkeypatch, step):
    calls = invoke(monkeypatch, "--eligible", "true", f"--{step}-outcome", "failure")
    assert f"[FAILED] as-of 2026-09-28 — failed step: {step}" in calls[0][1]["content"]


def test_acquisition_failure_is_degraded(monkeypatch):
    calls = invoke(monkeypatch, "--eligible", "true", "--tradingview-outcome", "failure")
    assert "[DEGRADED]" in calls[0][1]["content"]


def test_cancelled_is_cancelled(monkeypatch):
    calls = invoke(monkeypatch, "--cancelled", "true")
    assert calls[0][1]["content"].startswith("[CANCELLED]")


@pytest.mark.parametrize(
    "args",
    [
        ("--target-outcome", "success"),
        ("--eligible", "true", "--preflight-status", "already_saved"),
        ("--eligible", "true", "--preflight-status", "non_trading_day"),
        ("--eligible", "true", "--snapshot-status", "skipped_historical_asof"),
    ],
)
def test_noop_has_no_transport(monkeypatch, args):
    assert invoke(monkeypatch, *args) == []


@pytest.mark.parametrize(
    "snapshot_status", ["already_saved", "non_trading_day", "skipped_historical_asof"]
)
def test_successful_acquisition_noop_status_does_not_notify(monkeypatch, snapshot_status):
    assert (
        invoke(
            monkeypatch,
            "--eligible",
            "true",
            "--preflight-status",
            "needs_fetch",
            "--tradingview-outcome",
            "success",
            "--snapshot-status",
            snapshot_status,
        )
        == []
    )


def test_delivery_failure_keeps_exit_zero(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/id/token")
    assert (
        notify.main(
            [
                "--eligible",
                "true",
                "--snapshot-status",
                "saved",
                "--publish-lake-outcome",
                "success",
            ],
            transport=lambda *_: 500,
        )
        == 0
    )
