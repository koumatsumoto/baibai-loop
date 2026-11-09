import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from mcp.types import CallToolResult
from tests.helpers.tradingview_market import capture

from baibai_engine.storage import filesystem
from baibai_jobs.acquisition import market_context
from baibai_jobs.acquisition.tradingview import market_daily


class Session:
    def __init__(self, *, fail_at=None, hang_at=None):
        self.calls = []
        self.fail_at = fail_at
        self.hang_at = hang_at

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("synthetic network error")
        if len(self.calls) == self.hang_at:
            await asyncio.Event().wait()
        if name == market_daily.BATCH_TOOL:
            payload = {
                "success": True,
                "data": {
                    s: {"close": 1, "update_mode": "delayed_streaming_900"} for s in args["symbols"]
                },
                "missing": [],
                "count": len(args["symbols"]),
                "missing_count": 0,
            }
            return CallToolResult(content=[], structuredContent=payload)
        sample = capture(bars=True).result.structured_content
        return CallToolResult(content=[], structuredContent={**sample, "symbol": args["symbol"]})


def test_daily_receipts_preserve_requested_symbols_and_time(tmp_path, monkeypatch):
    async def no_sleep(seconds):
        assert seconds == 15

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    written = {}

    def retain_bytes(path, content):
        written[path] = content.encode("utf-8")
        filesystem.write_text_atomic(path, content)

    monkeypatch.setattr(market_daily, "write_text_atomic", retain_bytes)
    session = Session()
    directory = tmp_path / "captures"
    assert asyncio.run(market_daily.collect(session, directory)) == 3
    paths = sorted(directory.glob("*.json"))
    assert {path: path.read_bytes() for path in paths} == written
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    report = market_context.prepare(paths, tmp_path / "packet", expected_captures=3)
    assert report["sources"][0]["status"] == "ok"
    assert len(report["market_receipts"]) == 3
    value = json.loads(paths[0].read_text())
    assert value["request_parameters"]["symbols"] == ["OSE:NK2251!", "SP:SPX", "NASDAQ:NDX"]
    assert value["requested_at_utc"] <= value["received_at_utc"]
    assert (
        value["result"]["structuredContent"]["data"]["OSE:NK2251!"]["update_mode"]
        == "delayed_streaming_900"
    )


@pytest.mark.parametrize("hang", [False, True])
def test_interrupted_collection_preserves_partial_without_claiming_complete(
    tmp_path, monkeypatch, hang
):
    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    directory = tmp_path / "captures"
    session = Session(hang_at=2) if hang else Session(fail_at=2)
    run = market_daily.collect(session, directory)
    if hang:
        with pytest.raises(TimeoutError):
            asyncio.run(asyncio.wait_for(run, timeout=0.05))
    else:
        with pytest.raises(RuntimeError, match="synthetic"):
            asyncio.run(run)
    report = market_context.prepare(
        sorted(directory.glob("*.json")), tmp_path / "packet", expected_captures=3
    )
    assert report["sources"][0]["status"] == "failed"
    assert len(report["market_receipts"]) == 1


def test_interrupted_capture_write_does_not_expose_an_incomplete_final_json(tmp_path, monkeypatch):
    async def no_sleep(_seconds):
        pass

    directory = tmp_path / "captures"
    real_fsync = filesystem.os.fsync

    def interrupted_fsync(descriptor):
        # The next completed response is still private until its atomic replace.
        if (directory / "0.json").exists() and not (directory / "1.json").exists():
            paths = list(directory.iterdir())
            if len(paths) == 2:
                assert sorted(path.name for path in directory.glob("*.json")) == ["0.json"]
                pending = next(path for path in paths if path.name != "0.json")
                assert pending.suffix != ".json"
                assert pending.stat().st_mode & 0o777 == 0o600
                raise KeyboardInterrupt("synthetic interruption before final capture replace")
        real_fsync(descriptor)

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(filesystem.os, "fsync", interrupted_fsync)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        asyncio.run(market_daily.collect(Session(), directory))
    assert sorted(path.name for path in directory.glob("*.json")) == ["0.json"]
    report = market_context.prepare(
        sorted(directory.glob("*.json")), tmp_path / "packet", expected_captures=3
    )
    assert report["sources"][0]["status"] == "failed"
    assert len(report["market_receipts"]) == 1


@pytest.mark.parametrize(
    ("case", "expected_exit", "expected_receipts", "expected_status"),
    [("timeout", 124, 1, "failed"), ("no_receipt", 124, 0, None), ("normal", 0, 3, "ok")],
)
def test_workflow_capture_timeout_preserves_completed_receipts_in_existing_handoff(
    tmp_path, monkeypatch, case, expected_exit, expected_receipts, expected_status
):
    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(market_daily.asyncio, "sleep", no_sleep)
    fixtures = tmp_path / "fixtures"
    asyncio.run(market_daily.collect(Session(), fixtures))
    root = Path(__file__).resolve().parents[4]
    workflow = yaml.load(
        (root / ".github/workflows/cloud-tradingview-market-daily.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["capture"]["steps"]
    capture_step = next(step for step in steps if step.get("id") == "capture")
    prepare_step = next(step for step in steps if step.get("id") == "prepare")
    stage_step = next(step for step in steps if "market-evidence stage" in step.get("run", ""))
    cooldown_step = next(step for step in steps if "cooldown record" in step.get("run", ""))
    for step in (prepare_step, stage_step, cooldown_step):
        assert "always()" in step["if"]
        assert "!cancelled()" in step["if"]
    assert "steps.capture.outcome != 'skipped'" in prepare_step["if"]
    assert "steps.prepare.outputs.ready == 'true'" in stage_step["if"]

    system_timeout = shutil.which("timeout")
    assert system_timeout is not None
    timeout = tmp_path / "timeout"
    timeout.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$RUNNER_TEMP/timeout-arguments"\n'
        "shift 3\n"
        f'exec {shlex.quote(system_timeout)} --signal=TERM --kill-after=30s 1s "$@"\n'
    )
    timeout.chmod(0o755)
    owner_cli = tmp_path / "owner_cli.py"
    owner_cli.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from tests.helpers.r2_store import MemoryR2Store\n"
        "from baibai_jobs.acquisition import market_context\n"
        "from baibai_jobs.acquisition.tradingview import cooldown\n"
        "remote = MemoryR2Store()\n"
        "market_context.Boto3R2Store = cooldown.Boto3R2Store = lambda **_: remote\n"
        "if sys.argv[1:4] == ['run', 'baibai-jobs', 'market-evidence']:\n"
        "    status = market_context.main(sys.argv[4:])\n"
        "    if sys.argv[4] == 'stage':\n"
        "        key = next(key for key in remote.values if key.endswith('/handoff.json'))\n"
        "        Path(os.environ['RUNNER_TEMP'], 'staged.json').write_text(\n"
        "            json.dumps({'keys': remote.put_keys, 'manifest': json.loads(remote.get_bytes(key))}))\n"
        "else:\n"
        "    assert sys.argv[1:5] == ['run', 'python', '-m', 'baibai_jobs.acquisition.tradingview.cooldown']\n"
        "    status = cooldown.main(sys.argv[5:])\n"
        "    Path(os.environ['RUNNER_TEMP'], 'cooldown-recorded').touch()\n"
        "raise SystemExit(status)\n"
    )
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "$4" = baibai_jobs.acquisition.tradingview.market_daily ]; then\n'
        '  mkdir "$5"\n'
        '  if [ "$CAPTURE_CASE" != no_receipt ]; then cp "$RUNNER_TEMP/fixtures/0.json" "$5/"; fi\n'
        '  if [ "$CAPTURE_CASE" != normal ]; then exec sleep 60; fi\n'
        '  cp "$RUNNER_TEMP/fixtures/1.json" "$RUNNER_TEMP/fixtures/2.json" "$5/"\n'
        "  exit 0\nfi\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(owner_cli))} "$@"\n'
    )
    uv.chmod(0o755)
    output = tmp_path / "output"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PYTHONPATH": os.pathsep.join(
            [str(root), str(root / "engine/src"), str(root / "jobs/src")]
        ),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "STAGE_ID": "tradingview_market-timeout-test",
        "CAPTURE_CASE": case,
    }

    def shell(step):
        return subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    assert shell(capture_step).returncode == expected_exit
    assert (tmp_path / "timeout-arguments").read_text().splitlines()[:3] == [
        "--signal=TERM",
        "--kill-after=30s",
        "5m",
    ]
    # A failed capture step, not a cancelled job: existing always() steps can still run.
    prepared = shell(prepare_step)
    assert prepared.returncode == 0, prepared.stderr
    if expected_receipts:
        assert output.read_text() == "ready=true\n"
        staged = shell(stage_step)
        assert staged.returncode == 0, staged.stderr
        saved = json.loads((tmp_path / "staged.json").read_text())
        report = saved["manifest"]["result"]
        assert len(report["market_receipts"]) == expected_receipts
        assert report["sources"][0]["status"] == expected_status
        assert len(saved["keys"]) == 2  # One existing receipt pack and its immutable handoff.
    else:
        assert not output.exists()
        assert not (tmp_path / "market-handoff").exists()
        assert not (tmp_path / "staged.json").exists()
    recorded = shell(cooldown_step)
    assert recorded.returncode == 0, recorded.stderr
    assert (tmp_path / "cooldown-recorded").exists()
