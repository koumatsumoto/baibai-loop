"""Local publication follows the same cloud snapshot and exclusion boundaries."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from baibai_jobs.jobs import tradingview as runner
from baibai_jobs.jobs import tradingview_receipts as receipt_jobs
from baibai_jobs.jobs.tradingview_receipts import can_publish_failure_receipts, count_layer_rows
from baibai_engine.job_api import MarketSchemaError
from baibai_engine.data_collection.tradingview.receipts import retain_tool_result
from baibai_engine.storage.observations.schema import open_connection

REAL_COMMAND = runner.command


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
    settings = {
        "acquire": 0,
        "release": 0,
        "preflight": {"status": "needs_fetch", "needs_master": True},
        "refresh": "saved",
        "fail": None,
    }

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
        if name == "publish-lake":
            assert kwargs["env"]["PUBLICATION_LEASE_HANDLE"] == str(settings["handle"])
        calls.append(name)
        if name == settings["fail"]:
            raise runner.StageFailure("stage failed")
        if name == "refresh":
            return json.dumps(
                {
                    "status": settings["refresh"],
                    "rows_added": 2,
                    "rows": 2,
                    "expected_universe": 3 if settings["refresh"] == "partial" else 2,
                    "remaining": 1 if settings["refresh"] == "partial" else 0,
                }
            )
        return ""

    def preflight(_day):
        calls.append("preflight")
        if settings["fail"] == "preflight":
            raise ValueError("private-store-value")
        return settings["preflight"]

    monkeypatch.setattr(runner.lease, "main", lease_main)
    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: (0, 0))
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


def test_local_partial_with_existing_master_publishes_without_master_fetch(runtime):
    state, calls, settings = runtime
    settings["preflight"] = {"status": "needs_fetch", "needs_master": False, "rows": 2}
    settings["refresh"] = "partial"
    assert runner.main(["--state-file", str(state), "--interval", "15"]) == 0
    assert "master" not in calls
    assert calls[-3:] == ["refresh", "publish-lake", "release"]


def test_local_invalid_pacing_stops_before_cloud_operations(runtime):
    state, calls, _ = runtime
    assert runner.main(["--state-file", str(state), "--interval", "3"]) == 1
    assert calls == []


@pytest.mark.parametrize("code", [1, 3])
def test_acquire_failure_does_not_read_or_release(runtime, code):
    state, calls, settings = runtime
    settings["acquire"] = code
    assert runner.main(["--state-file", str(state)]) == code
    assert calls == ["acquire"]


@pytest.mark.parametrize("status", ["already_saved", "skipped_historical_asof"])
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


@pytest.mark.parametrize(("added", "expected", "remaining"), [(0, 51, 1), (1, 49, -1)])
def test_invalid_partial_counts_do_not_publish(runtime, monkeypatch, added, expected, remaining):
    state, calls, _ = runtime
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh")
            return json.dumps(
                {
                    "status": "partial",
                    "rows_added": added,
                    "rows": 50,
                    "expected_universe": expected,
                    "remaining": remaining,
                }
            )
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert "publish-lake" not in calls


def test_before_observation_hours_does_not_read_credentials_or_acquire(runtime, monkeypatch):
    state, calls, _ = runtime
    state.unlink()
    monkeypatch.setattr(
        runner,
        "resolve_tradingview_target",
        lambda *_a, **_kw: (date(2026, 9, 28), "manual", False, "before_observation"),
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


def test_refresh_stderr_uses_final_safe_category_without_echo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    stderr_path = tmp_path / "stderr.log"
    script = (
        "import sys; "
        "print('TradingView acquisition failed; category=provider_response', file=sys.stderr); "
        "print('private-token', file=sys.stderr); "
        "print('TradingView acquisition failed; category=storage', file=sys.stderr); "
        "sys.exit(1)"
    )
    with pytest.raises(runner.StageFailure) as error:
        runner.command([sys.executable, "-c", script], stderr_path=stderr_path)
    assert error.value.category == "storage"
    assert "private-token" not in capsys.readouterr().err


def test_count_uses_validated_read_transaction(tmp_path, monkeypatch):
    market = tmp_path / "market.sqlite"
    open_connection(market).close()
    original_validate = receipt_jobs.validate_market_schema

    def validate(conn):
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        original_validate(conn)

    monkeypatch.setattr(receipt_jobs, "validate_market_schema", validate)
    assert count_layer_rows(market, date(2026, 9, 28)) == (0, 0)
    malformed = tmp_path / "malformed.sqlite"
    sqlite3.connect(malformed).close()
    with pytest.raises(MarketSchemaError):
        count_layer_rows(malformed, date(2026, 9, 28))


def test_actual_refresh_failure_prints_only_safe_category(runtime, monkeypatch, capsys):
    state, calls, _settings = runtime
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            script = (
                "import sys; "
                "print('private-token', file=sys.stderr); "
                "print('TradingView acquisition failed; category=provider_response', file=sys.stderr); "
                "sys.exit(1)"
            )
            return REAL_COMMAND([sys.executable, "-c", script], stderr_path=kwargs["stderr_path"])
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    stderr = capsys.readouterr().err
    assert "TradingView acquisition failed; category=provider_response" in stderr
    assert "private-token" not in stderr
    assert "publish-lake" not in calls


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


def test_batch_size_is_not_a_configurable_operational_option(runtime):
    state, calls, _ = runtime
    with pytest.raises(SystemExit) as error:
        runner.main(["--state-file", str(state), "--batch-size", "50"])
    assert error.value.code == 2
    assert calls == []


@pytest.mark.parametrize(
    ("category", "receipt"),
    [
        (
            "provider_rate_limit",
            CallToolResult(isError=True, content=[], structuredContent={"status": 429}),
        ),
        (
            "provider_response",
            CallToolResult(isError=False, content=[], structuredContent={"unexpected": 1}),
        ),
        (
            "provider_transport",
            CallToolResult(isError=True, content=[], structuredContent={"status": 503}),
        ),
        ("auth", CallToolResult(isError=True, content=[], structuredContent={"status": 401})),
        (
            "secret_persistence",
            CallToolResult(isError=False, content=[], structuredContent={"unexpected": 1}),
        ),
        (
            "time_guard",
            CallToolResult(isError=False, content=[], structuredContent={"unexpected": 1}),
        ),
        ("timeout", CallToolResult(isError=False, content=[], structuredContent={"unexpected": 1})),
    ],
)
def test_failed_acquisition_publishes_retained_l1_only(
    runtime, monkeypatch, tmp_path, category, receipt
):
    state, calls, _settings = runtime
    market = runner.ROOT / runner.MARKET_DB_PATH
    market.parent.mkdir(parents=True, exist_ok=True)
    with open_connection(market):
        pass
    monkeypatch.setattr(
        runner,
        "count_layer_rows",
        lambda _path, day: count_layer_rows(market, day),
    )
    original = runner.command
    published = tmp_path / "published.sqlite"

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            with sqlite3.connect(market) as conn:
                retain_tool_result(
                    conn,
                    snapshot_date=date(2026, 9, 28),
                    master_asof=date(2026, 9, 28),
                    tool_name="test_tool",
                    received_at_utc=datetime(2026, 9, 28, tzinfo=UTC),
                    requested_symbols=["1000"],
                    requested_columns=["close"],
                    result=receipt,
                )
            raise runner.StageFailure("original acquisition failure", category=category)
        if args[-1] == "publish-lake":
            calls.append("publish-lake")
            shutil.copyfile(market, published)
            return ""
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-3:] == ["refresh-child-stopped", "publish-lake", "release"]
    assert can_publish_failure_receipts(category, 0, 1, 0, 0)
    with sqlite3.connect(published) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone() == (
            0,
        )


@pytest.mark.parametrize("category", ["internal", "storage", "unknown", None])
def test_nonprovider_failure_does_not_expand_publication(runtime, monkeypatch, category):
    state, calls, _settings = runtime
    counts = iter([(0, 0), (1, 0)])
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: next(counts))
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            raise runner.StageFailure("failed", category=category)
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert "publish-lake" not in calls
    assert calls[-1] == "release"


def test_no_receipt_delta_does_not_publish_transport_failure(runtime, monkeypatch):
    state, calls, _settings = runtime
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            raise runner.StageFailure("failed", category="provider_transport")
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert "publish-lake" not in calls


def test_failed_acquisition_with_l2_delta_does_not_expand_publication(runtime, monkeypatch):
    state, calls, _settings = runtime
    counts = iter([(0, 0), (1, 1)])
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: next(counts))
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            raise runner.StageFailure("failed", category="provider_response")
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert "publish-lake" not in calls
    assert calls[-1] == "release"


def test_publication_failure_keeps_original_acquisition_failure(runtime, monkeypatch, capsys):
    state, calls, _settings = runtime
    counts = iter([(0, 0), (1, 0)])
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: next(counts))
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            raise runner.StageFailure("original acquisition failure", category="provider_response")
        if args[-1] == "publish-lake":
            calls.append("publish-lake")
            raise runner.StageFailure("private publication detail")
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(runner.StageFailure, match="original acquisition failure"):
        runner.run_local(state)
    assert calls[-3:] == ["refresh-child-stopped", "publish-lake", "release"]
    stderr = capsys.readouterr().err
    assert "category=storage" in stderr
    assert "private publication detail" not in stderr


def test_actual_timeout_bounds_recovery_and_preserves_failure(runtime, monkeypatch, capsys):
    state, calls, _settings = runtime
    counts = iter([(0, 0), (1, 0)])
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: next(counts))
    monkeypatch.setattr(runner, "RECOVERY_PUBLISH_TIMEOUT", 0.03)
    original = runner.command
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            return REAL_COMMAND(sleeper, timeout=0.07)
        if args[-1] == "publish-lake":
            calls.append("publish-lake")
            assert kwargs["timeout"] == 0.03
            return REAL_COMMAND(sleeper, timeout=kwargs["timeout"])
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        runner.run_local(state)
    assert error.value.timeout == 0.07
    assert calls[-3:] == ["refresh-child-stopped", "publish-lake", "release"]
    assert "category=storage" in capsys.readouterr().err


def test_actual_overall_alarm_bounds_recovery_publication(runtime, monkeypatch, capsys):
    state, calls, _settings = runtime
    counts = iter([(0, 0), (1, 0)])
    monkeypatch.setattr(runner, "count_layer_rows", lambda *_a: next(counts))
    monkeypatch.setattr(runner, "RECOVERY_PUBLISH_TIMEOUT", 0.03)
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            script = "import os,signal,time; os.kill(os.getppid(), signal.SIGALRM); time.sleep(60)"
            return REAL_COMMAND([sys.executable, "-c", script], timeout=5)
        if args[-1] == "publish-lake":
            calls.append("publish-lake")
            assert kwargs["timeout"] == 0.03
            return REAL_COMMAND(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=kwargs["timeout"],
            )
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(runner.RunTimeout):
        runner.run_local(state)
    assert calls[-3:] == ["refresh-child-stopped", "publish-lake", "release"]
    assert "category=storage" in capsys.readouterr().err


def test_actual_cancellation_stops_without_recovery_publication(runtime, monkeypatch, capsys):
    state, calls, _settings = runtime
    original = runner.command

    def command(args, **kwargs):
        if "refresh" in args:
            calls.append("refresh-child-stopped")
            script = "import os,signal,time; os.kill(os.getppid(), signal.SIGTERM); time.sleep(60)"
            return REAL_COMMAND([sys.executable, "-c", script], timeout=5)
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "command", command)
    assert runner.main(["--state-file", str(state)]) == 1
    assert calls[-2:] == ["refresh-child-stopped", "release"]
    assert "publish-lake" not in calls
    assert "category=cancelled" in capsys.readouterr().err
