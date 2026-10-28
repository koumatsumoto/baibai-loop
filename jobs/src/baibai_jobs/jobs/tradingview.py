"""Run a local TradingView snapshot under the existing publication lease."""

from __future__ import annotations

import argparse
import json
import os
import re
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

from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_jobs.observability.tradingview import failure_line as tradingview_failure_line
from baibai_jobs.acquisition.tradingview.collector import preflight_snapshot as tradingview_preflight
from baibai_jobs.observability.tradingview import safe_progress as tradingview_safe_progress
from baibai_jobs.jobs.schedule import resolve_tradingview_target
from baibai_jobs.jobs.tradingview_receipts import (
    KNOWN_FAILURES,
    can_publish_failure_receipts,
    count_layer_rows,
)
from baibai_jobs.storage import publication_lease as lease

ROOT = Path(__file__).resolve().parents[4]
FETCH_TIMEOUT = 30 * 60
RUN_TIMEOUT = 60 * 60
RECOVERY_PUBLISH_TIMEOUT = 10 * 60


class StageFailure(Exception):
    """A child already reported its safe failure details."""

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category if category in KNOWN_FAILURES else None


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


def command(
    argv: list[str],
    *,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stderr_path: Path | None = None,
) -> str:
    # fixed commands with parsed date/path arguments
    stderr_file = stderr_path.open("w", encoding="utf-8") if stderr_path else None
    try:
        process = subprocess.Popen(  # nosec B603
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
            env=env,
        )
    except BaseException:
        if stderr_file:
            stderr_file.close()
        raise
    try:
        output, _ = process.communicate(timeout=timeout)
    finally:
        try:
            _stop_child(process)
        finally:
            if stderr_file:
                stderr_file.close()
    if process.returncode:
        category = None
        if stderr_path:
            with suppress(OSError, UnicodeError):
                matches = re.findall(
                    r"TradingView acquisition failed; category=([a-z_]+)",
                    stderr_path.read_text(encoding="utf-8"),
                )
                category = matches[-1] if matches and matches[-1] in KNOWN_FAILURES else None
        raise StageFailure("TradingView stage failed", category=category)
    return output


def _report(output: str, allowed: set[str]) -> dict[str, object]:
    try:
        value = json.loads(output.strip().splitlines()[-1])
        if not isinstance(value, dict) or value.get("status") not in allowed:
            raise ValueError()
        if value["status"] in {"saved", "partial"}:

            def count(key: str) -> int:
                number = value.get(key)
                if type(number) is not int or number < 0:
                    raise ValueError()
                return number

            added, rows = count("rows_added"), count("rows")
            expected, remaining = count("expected_universe"), count("remaining")
            if (
                added <= 0
                or added > rows
                or rows + remaining != expected
                or (value["status"] == "saved") != (remaining == 0)
            ):
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


def preflight(day: date) -> dict[str, object]:
    now = datetime.now(UTC)
    today, _, _, _ = resolve_tradingview_target(now, event_name="workflow_dispatch", schedule="")
    if day < today:
        return {"status": "skipped_historical_asof", "snapshot_date": day.isoformat()}
    return tradingview_preflight(ROOT / MARKET_DB_PATH, day, now=now)


def run_local(state_file: Path, *, interval: float = 15.0) -> int:
    if not 15 <= interval <= 3600:
        raise StageFailure("Observation interval must be between 15 and 3600 seconds")
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
            command([str(ROOT / "jobs/scripts/r2_transfer.sh"), "pull-market"])
            command([str(ROOT / "jobs/scripts/r2_transfer.sh"), "hydrate-market"])
            result = preflight(day)
            if result.get("status") == "needs_fetch":
                if result.get("needs_master"):
                    command(
                        [
                            sys.executable,
                            "-m",
                            "baibai_engine.cli.app",
                            "data-collection",
                            "market",
                            "backfill-master",
                            "--asof",
                            day.isoformat(),
                        ]
                    )
                progress = handle_dir / "progress.json"
                stderr_path = handle_dir / "refresh-stderr.log"
                before_receipts, before_rows = count_layer_rows(ROOT / MARKET_DB_PATH, day)
                try:
                    output = command(
                        [
                            sys.executable,
                            "-m",
                            "baibai_engine.cli.app",
                            "tradingview",
                            "refresh",
                            "--asof",
                            day.isoformat(),
                            "--state-file",
                            str(state_file),
                            "--interval",
                            str(interval),
                            "--progress-output",
                            str(progress),
                        ],
                        timeout=FETCH_TIMEOUT,
                        stderr_path=stderr_path,
                    )
                except (StageFailure, subprocess.TimeoutExpired, RunTimeout) as exc:
                    category = exc.category if isinstance(exc, StageFailure) else "timeout"
                    try:
                        after_receipts, after_rows = count_layer_rows(ROOT / MARKET_DB_PATH, day)
                    except Exception:
                        after_receipts, after_rows = -1, -1
                    if can_publish_failure_receipts(
                        category, before_receipts, after_receipts, before_rows, after_rows
                    ):
                        try:
                            print(
                                command(
                                    [str(ROOT / "jobs/scripts/r2_transfer.sh"), "publish-lake"],
                                    env={**os.environ, "PUBLICATION_LEASE_HANDLE": str(handle)},
                                    timeout=RECOVERY_PUBLISH_TIMEOUT,
                                ),
                                end="",
                            )
                        except Exception:
                            print(
                                "TradingView lake publication failed; category=storage",
                                file=sys.stderr,
                            )
                    raise
                finally:
                    stderr_path.unlink(missing_ok=True)
                result = _report(
                    output,
                    {
                        "saved",
                        "partial",
                        "already_saved",
                        "skipped_historical_asof",
                    },
                )
                if result["status"] in {"saved", "partial"}:
                    print(
                        command(
                            [str(ROOT / "jobs/scripts/r2_transfer.sh"), "publish-lake"],
                            env={**os.environ, "PUBLICATION_LEASE_HANDLE": str(handle)},
                        ),
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
    parser = argparse.ArgumentParser(prog="baibai-jobs tradingview")
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="independent local OAuth file outside this dedicated runtime checkout",
    )
    parser.add_argument(
        "--interval", type=float, default=15.0, help="request interval, 15..3600 seconds"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        if Path.cwd().resolve() != ROOT:
            raise StageFailure("Run from the dedicated repository root")
        return run_local(
            args.state_file.expanduser().resolve(),
            interval=args.interval,
        )
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
        if exc.category:
            print(f"TradingView acquisition failed; category={exc.category}", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(tradingview_failure_line(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
