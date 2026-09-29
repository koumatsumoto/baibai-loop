"""Local publication follows the same cloud snapshot and exclusion boundaries."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from baibai_batch.jobs import tradingview as runner


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    state = tmp_path / "oauth.json"
    state.write_text("test-state")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(runner, "load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        runner,
        "resolve_tradingview_target",
        lambda *_a, **_kw: (date(2026, 9, 28), "manual", True, ""),
    )
    calls = []
    settings = {"acquire": 0, "release": 0, "preflight": None, "refresh": "saved", "fail": None}

    def lease_main(args):
        step = args[0]
        calls.append(step)
        handle = Path(args[args.index("--handle") + 1])
        settings["handle"] = handle
        if step == "acquire" and not settings[step]:
            handle.write_text("held")
        if step == "release" and not settings[step]:
            handle.unlink()
        return settings[step]

    def command(args, **kwargs):
        if args[0] == "git":
            return ""
        if "refresh" in args:
            name = "refresh"
            assert args[args.index("--state-file") + 1] == str(state)
            assert args[args.index("--asof") + 1] == "2026-09-28"
            assert kwargs["timeout"] == 1800
        elif "backfill-master" in args:
            name = "master"
            assert args[args.index("--asof") + 1] == "2026-09-28"
        else:
            name = args[-1]
        calls.append(name)
        if name == settings["fail"]:
            raise runner.StageFailure("stage failed")
        if name == "refresh":
            return json.dumps({"status": settings["refresh"]})
        return ""

    def preflight(_day):
        calls.append("preflight")
        if settings["fail"] == "preflight":
            raise ValueError("private-store-value")
        return settings["preflight"]

    monkeypatch.setattr(runner.lease, "main", lease_main)
    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(runner, "preflight", preflight)
    return state, calls, settings


def test_local_sequence_and_cloud_write_scope(runtime):
    state, calls, settings = runtime
    assert runner.main(["--state-file", str(state)]) == 0
    assert calls == [
        "acquire",
        "pull-market",
        "hydrate-market",
        "preflight",
        "master",
        "refresh",
        "publish-lake",
        "release",
    ]
    assert not settings["handle"].exists()


@pytest.mark.parametrize("code", [1, 3])
def test_acquire_failure_does_not_read_or_release(runtime, code):
    state, calls, settings = runtime
    settings["acquire"] = code
    assert runner.main(["--state-file", str(state)]) == code
    assert calls == ["acquire"]


@pytest.mark.parametrize("status", ["already_saved", "non_trading_day", "skipped_historical_asof"])
def test_preflight_noop_avoids_master_oauth_and_publish(runtime, status):
    state, calls, settings = runtime
    settings["preflight"] = {"status": status}
    assert runner.main(["--state-file", str(state)]) == 0
    assert calls == ["acquire", "pull-market", "hydrate-market", "preflight", "release"]


@pytest.mark.parametrize(
    "stage", ["pull-market", "hydrate-market", "preflight", "master", "refresh", "publish-lake"]
)
def test_stage_failure_stops_pipeline_and_releases(runtime, stage):
    state, calls, settings = runtime
    settings["fail"] = stage
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-1] == "release"
    if stage != "publish-lake":
        assert "publish-lake" not in calls


def test_release_failure_keeps_handle_and_nonzero(runtime):
    state, calls, settings = runtime
    settings["release"] = 1
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-1] == "release"
    assert settings["handle"].exists()
    settings["handle"].unlink()
    settings["handle"].parent.rmdir()


def test_crossed_date_after_master_does_not_publish(runtime):
    state, calls, settings = runtime
    settings["refresh"] = "skipped_historical_asof"
    assert runner.main(["--state-file", str(state)]) == 0
    assert "publish-lake" not in calls
    assert calls[-1] == "release"


def test_bad_success_payload_does_not_publish(runtime):
    state, calls, settings = runtime
    settings["refresh"] = "unexpected"
    assert runner.main(["--state-file", str(state)]) == 1
    assert "publish-lake" not in calls
    assert calls[-1] == "release"


def test_before_close_does_not_read_credentials_or_acquire(runtime, monkeypatch):
    state, calls, _ = runtime
    state.unlink()
    monkeypatch.setattr(
        runner,
        "resolve_tradingview_target",
        lambda *_a, **_kw: (date(2026, 9, 28), "manual", False, "before_close"),
    )
    assert runner.main(["--state-file", str(state)]) == 0
    assert calls == []


@pytest.mark.parametrize(
    "error", [runner.RunInterrupted, runner.RunTimeout, subprocess.TimeoutExpired]
)
def test_interrupt_or_timeout_releases_after_child_cleanup(runtime, monkeypatch, error):
    state, calls, _ = runtime
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("child-stopped")
            if error is subprocess.TimeoutExpired:
                raise error("refresh", 1800)
            raise error()
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-2:] == ["child-stopped", "release"]
    assert "publish-lake" not in calls


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGALRM])
def test_real_signal_kills_child_before_control_returns(tmp_path, monkeypatch, signum):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    pid_file = tmp_path / "pid"
    script = (
        "import os, signal, time; from pathlib import Path; Path('pid').write_text(str(os.getpid())); os.kill(os.getppid(), "
        + str(int(signum))
        + "); time.sleep(60)"
    )
    error = runner.RunTimeout if signum == signal.SIGALRM else runner.RunInterrupted
    with pytest.raises(error), runner.interruption_guard(5):
        runner.command([sys.executable, "-c", script])
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_real_command_timeout_kills_descendants(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    script = "import subprocess, sys, time; from pathlib import Path; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); Path('pid').write_text(str(p.pid)); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.command([sys.executable, "-c", script], timeout=0.5)
    pid = int((tmp_path / "pid").read_text())
    proc = Path(f"/proc/{pid}/stat")
    # Group signals are asynchronous; waitpid only reaps our direct child.
    deadline = time.monotonic() + 2
    while True:
        try:
            state = proc.read_text().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        assert time.monotonic() < deadline, f"descendant still running: {state}"
        time.sleep(0.01)


def test_collection_and_release_failures_are_both_reported(runtime, capsys):
    state, _, settings = runtime
    settings["fail"] = "refresh"
    settings["release"] = 1
    assert runner.main(["--state-file", str(state)]) == 1
    stderr = capsys.readouterr().err
    assert "stage failed" in stderr
    assert "lease release failed" in stderr
    settings["handle"].unlink()
    settings["handle"].parent.rmdir()


def test_dirty_checkout_does_not_acquire(runtime, monkeypatch):
    state, calls, _ = runtime
    monkeypatch.setattr(runner, "command", lambda *_a, **_kw: " M file.py")
    assert runner.main(["--state-file", str(state)]) == 1
    assert not calls


def test_repository_oauth_file_is_rejected_before_acquire(runtime):
    _, calls, _ = runtime
    state = runner.ROOT / "oauth.json"
    state.write_text("private")
    assert runner.main(["--state-file", str(state)]) == 1
    assert not calls


def test_timeout_reports_safe_progress_before_release_and_removes_file(
    runtime, monkeypatch, capsys
):
    state, calls, settings = runtime
    original = runner.command
    progress_path = None

    def command(args, **kwargs):
        nonlocal progress_path
        if "refresh" in args:
            progress_path = Path(args[args.index("--progress-output") + 1])
            progress_path.write_text(
                json.dumps(
                    {
                        "snapshot_date": "2026-09-28",
                        "phase": "fetch",
                        "expected_universe": 3709,
                        "chunks_completed": 10,
                        "chunk_index": 11,
                        "provider_elapsed_seconds": 123.5,
                        "oauth_rotations": 1,
                        "response_bytes": 2975,
                        "token": "private-token",
                        "headers": {"authorization": "private-token"},
                        "elapsed_seconds": {"unexpected": "private-token"},
                    }
                )
            )
            raise subprocess.TimeoutExpired("refresh", 1800)
        return original(args, **kwargs)

    original_release = runner.lease.main

    def lease_main(args):
        if args[0] == "release":
            stderr = capsys.readouterr().err
            lines = [
                line for line in stderr.splitlines() if line.startswith("TradingView progress: ")
            ]
            assert len(lines) == 1
            summary = json.loads(lines[0].removeprefix("TradingView progress: "))
            assert summary["chunks_completed"] == 10
            assert summary["chunk_index"] == 11
            assert summary["oauth_rotations"] == 1
            assert summary["expected_universe"] == 3709
            assert summary["provider_elapsed_seconds"] == 123.5
            assert summary["response_bytes"] == 2975
            assert "elapsed_seconds" not in summary
            assert "private-token" not in stderr
            assert "token" not in summary
            assert "headers" not in summary
        return original_release(args)

    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(runner.lease, "main", lease_main)
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-1] == "release"
    assert progress_path is not None
    assert not progress_path.exists()
    assert not settings["handle"].parent.exists()
    assert "category=timeout" in capsys.readouterr().err
