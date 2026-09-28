"""Run a local TradingView snapshot under the existing publication lease."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess  # nosec B404 - fixed operational commands, never credential arguments
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from types import FrameType

from dotenv import load_dotenv

from baibai_batch.jobs.schedule import resolve_tradingview_target
from baibai_batch.storage import publication_lease as lease
from baibai_engine.batch_api import (
    MARKET_DB_PATH,
    tradingview_failure_line,
    tradingview_preflight,
    tradingview_safe_progress,
    tradingview_validate_time,
)

ROOT = Path(__file__).resolve().parents[4]
FETCH_TIMEOUT = 30 * 60
RUN_TIMEOUT = 60 * 60


class StageFailure(Exception):
    """A child already reported its safe failure details."""


class RunTimeout(BaseException):
    pass


class RunInterrupted(BaseException):
    pass


@contextmanager
def interruption_guard(seconds: float) -> Iterator[None]:
    def stop(signum: int, _frame: FrameType | None) -> None:
        if signum == signal.SIGALRM:
            raise RunTimeout()
        raise RunInterrupted()

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    previous = {value: signal.getsignal(value) for value in signals}
    for value in signals:
        signal.signal(value, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for value, handler in previous.items():
            signal.signal(value, handler)


def _stop_child(process: subprocess.Popen[str]) -> None:
    # A shell/uv parent can exit before its children. Kill the whole isolated group.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def command(argv: list[str], *, timeout: float | None = None) -> str:
    process = subprocess.Popen(  # nosec B603 - fixed commands with parsed date/path arguments
        argv, cwd=ROOT, stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode:
            raise StageFailure("TradingView stage failed")
        return output
    finally:
        _stop_child(process)


def _report(output: str, allowed: set[str]) -> dict[str, object]:
    try:
        value = json.loads(output.strip().splitlines()[-1])
        if not isinstance(value, dict) or value.get("status") not in allowed:
            raise ValueError()
        return value
    except (ValueError, IndexError, TypeError):
        raise StageFailure("Invalid TradingView stage result") from None


def report_failed_progress(path: Path) -> None:
    # Best effort only: diagnostics must not obscure the failure or prevent release.
    with suppress(OSError, ValueError, UnicodeError):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            summary = json.dumps(
                tradingview_safe_progress(payload), sort_keys=True, allow_nan=False
            )
            print("TradingView progress: " + summary, file=sys.stderr)


def preflight(day: date) -> dict[str, object] | None:
    now = datetime.now(UTC)
    today, _, _, _ = resolve_tradingview_target(now, event_name="workflow_dispatch", schedule="")
    if day < today:
        return {"status": "skipped_historical_asof", "snapshot_date": day.isoformat()}
    tradingview_validate_time(day, now)
    return tradingview_preflight(ROOT / MARKET_DB_PATH, day)


def run_local(state_file: Path) -> int:
    day, _, eligible, reason = resolve_tradingview_target(
        datetime.now(UTC), event_name="workflow_dispatch", schedule=""
    )
    if not eligible:
        print(json.dumps({"status": reason, "snapshot_date": day.isoformat()}))
        return 0
    if state_file.is_relative_to(ROOT):
        raise StageFailure("Keep the independent OAuth file outside the repository")
    if not state_file.is_file():
        raise StageFailure("Authorize an independent local OAuth file first")
    # Existing transfer commands intentionally use canonical paths within this checkout.
    if command(["git", "status", "--porcelain"]).strip():
        raise StageFailure("Use a clean dedicated runtime checkout")
    handle_dir = Path(tempfile.mkdtemp(prefix="baibai-tv-lease-"))
    handle = handle_dir / "handle.json"
    result: dict[str, object] = {}
    try:
        with interruption_guard(RUN_TIMEOUT):
            status = lease.main(["acquire", "--purpose", "tradingview", "--handle", str(handle)])
            if status:
                return status
            command([str(ROOT / "batch/scripts/r2_transfer.sh"), "pull-market"])
            command([str(ROOT / "batch/scripts/r2_transfer.sh"), "hydrate-market"])
            result = preflight(day) or {}
            if not result:
                command(
                    [
                        sys.executable,
                        "-m",
                        "baibai_engine.cli",
                        "screening",
                        "backfill-master",
                        "--asof",
                        day.isoformat(),
                    ]
                )
                progress = handle_dir / "progress.json"
                output = command(
                    [
                        sys.executable,
                        "-m",
                        "baibai_engine.cli",
                        "tradingview",
                        "refresh",
                        "--asof",
                        day.isoformat(),
                        "--state-file",
                        str(state_file),
                        "--progress-output",
                        str(progress),
                    ],
                    timeout=FETCH_TIMEOUT,
                )
                result = _report(
                    output, {"saved", "already_saved", "non_trading_day", "skipped_historical_asof"}
                )
                if result["status"] == "saved":
                    print(
                        command([str(ROOT / "batch/scripts/r2_transfer.sh"), "publish-lake"]),
                        end="",
                    )
    except BaseException:
        report_failed_progress(handle_dir / "progress.json")
        raise
    finally:
        # All child groups have stopped before reaching here. A failed release retains its handle.
        if handle.exists():
            print(f"TradingView lease handle: {handle}", file=sys.stderr)
            if lease.main(["release", "--handle", str(handle)]):
                print("TradingView lease release failed; retain the handle", file=sys.stderr)
                if sys.exception() is None:
                    raise lease.LeaseError("TradingView lease release failed")
        if not handle.exists():
            (handle_dir / "progress.json").unlink(missing_ok=True)
            handle_dir.rmdir()
    print(json.dumps(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-batch tradingview")
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="independent local OAuth file outside this dedicated runtime checkout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        if Path.cwd().resolve() != ROOT:
            raise StageFailure("Run from the dedicated repository root")
        return run_local(args.state_file.expanduser().resolve())
    except lease.LeaseError:
        print("TradingView execution failed; category=lease", file=sys.stderr)
        return 1
    except (RunTimeout, subprocess.TimeoutExpired):
        print("TradingView acquisition failed; category=timeout", file=sys.stderr)
        return 1
    except (RunInterrupted, KeyboardInterrupt):
        print("TradingView execution failed; category=cancelled", file=sys.stderr)
        return 1
    except StageFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(tradingview_failure_line(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
