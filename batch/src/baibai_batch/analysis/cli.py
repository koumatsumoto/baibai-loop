"""Triage one canonical Review Set without reproducing its machine work."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess  # nosec B404
import time
import traceback
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from baibai_batch.analysis.io import (
    canonical_json,
    ensure_private_dir,
    read_json,
    redact,
    redact_argv,
    resolve_executable,
    write_json_atomic,
    write_log,
)
from baibai_batch.analysis.models import (
    MacroProjection,
    ModelInput,
    ModelOutput,
    ModelUsage,
    TriageCandidate,
)
from baibai_batch.analysis.policy import TRIAGE_POLICY
from baibai_engine.batch_api import (
    APPLICATION_DB_PATH,
    MACRO_CONTEXT_STALE_DAYS,
    RUNS_DB_PATH,
    DailyAnalysisContext,
    ReviewSetEntrySnapshot,
    load_daily_analysis_context,
    publish_daily_research_triage,
)

_JST = ZoneInfo("Asia/Tokyo")
_MAX_MODEL_INPUT_BYTES = 1_000_000
_MAX_MODEL_OUTPUT_BYTES = 256_000
_MODEL_TIMEOUT_SECONDS = 900
_TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "mcp_tool_call", "web_search", "file_read", "file_write"}
)


def default_state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    return base / "baibai-loop"


class PipelineLock(AbstractContextManager["PipelineLock"]):
    """Use the kernel as the only authority for one local analysis execution."""

    def __init__(self, state_root: Path) -> None:
        lock_dir = ensure_private_dir(state_root / "locks", root=state_root)
        self.path = lock_dir / "daily-analysis.lock"
        self._handle: TextIO = self.path.open("a+", encoding="utf-8")
        self.path.chmod(0o600)

    def acquire(self) -> bool:
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback_value: TracebackType | None,
    ) -> None:
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


class RunLog:
    """Keep one private, redacted, bounded detail log outside normal stdout."""

    def __init__(self, path: Path, *, state_root: Path) -> None:
        self.path = path
        self._state_root = state_root
        self._parts: list[str] = []
        self.append("analysis run started")

    def append(self, text: str) -> None:
        self._parts.append(redact(text).rstrip())
        write_log(self.path, "\n\n".join(self._parts) + "\n", root=self._state_root)


class ModelAdapterError(RuntimeError):
    pass


type ModelRunner = Callable[[ModelInput, Path, Path, RunLog], tuple[ModelOutput, ModelUsage, int]]


def _create_run(state_root: Path) -> Path:
    stamp = datetime.now(_JST).strftime("%Y%m%dT%H%M%S%z")
    return ensure_private_dir(
        state_root / "analysis" / f"{stamp}-{uuid4().hex[:8]}", root=state_root
    )


def _macro_projection(context: DailyAnalysisContext) -> MacroProjection:
    document = context.macro_context
    if document is None:
        return MacroProjection(status="missing")
    age_days = (context.review_set.as_of - document.as_of).days
    return MacroProjection(
        status="stale" if age_days > MACRO_CONTEXT_STALE_DAYS else "current",
        as_of=document.as_of.isoformat(),
        age_days=age_days,
        summary=document.summary,
        synthesis=(
            None if document.synthesis is None else document.synthesis.model_dump(mode="json")
        ),
        connection=document.connection.model_dump(mode="json"),
    )


def _model_input(context: DailyAnalysisContext) -> ModelInput:
    candidates = tuple(
        TriageCandidate(
            ticker=entry.ticker,
            snapshot=ReviewSetEntrySnapshot(
                name=entry.name,
                sector_33=entry.sector_33,
                nominations=entry.nominations,
                analysis=entry.analysis,
            ),
        )
        for entry in context.review_set.entries
    )
    return ModelInput(
        schema_version=1,
        task="research-triage",
        instruction=(
            "Use only this JSON. Do not call tools or read files. Compare every candidate and "
            "return exactly one strict decision for each ticker."
        ),
        policy=TRIAGE_POLICY,
        as_of=context.review_set.as_of.isoformat(),
        macro_context=_macro_projection(context),
        candidates=candidates,
    )


def _adapter_environment() -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run_model(
    model_input: ModelInput,
    run_dir: Path,
    state_root: Path,
    log: RunLog,
) -> tuple[ModelOutput, ModelUsage, int]:
    payload = canonical_json(model_input.model_dump(mode="json")) + b"\n"
    schema = canonical_json(ModelOutput.model_json_schema()) + b"\n"
    adapter_input_bytes = _model_input_bytes(model_input)
    if adapter_input_bytes > _MAX_MODEL_INPUT_BYTES:
        raise ModelAdapterError(f"model input exceeds the {_MAX_MODEL_INPUT_BYTES} byte hard limit")
    result_path = run_dir / "result.json"
    schema_path = run_dir / ".output-schema.json"
    schema_path.write_bytes(schema)
    schema_path.chmod(0o600)
    argv = [
        resolve_executable("codex"),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
        "--json",
        "-",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(  # nosec B603
            argv,
            cwd=run_dir,
            env=_adapter_environment(),
            input=payload.decode("utf-8"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_MODEL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelAdapterError("model adapter timed out") from error
    finally:
        schema_path.unlink(missing_ok=True)
    duration = time.monotonic() - started
    log.append(
        "model adapter\n"
        f"argv={' '.join(str(value) for value in redact_argv(argv[:-1]))}\n"
        f"exit={completed.returncode} duration={duration:.6f}s\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    requests, input_tokens, output_tokens, tool_calls = _codex_usage(completed.stdout)
    usage = ModelUsage(
        model_requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_seconds=duration,
        tool_calls=tool_calls,
    )
    if completed.returncode != 0:
        raise ModelAdapterError(f"model adapter exited {completed.returncode}")
    if tool_calls:
        raise ModelAdapterError("model used a tool even though the task forbids tool use")
    if not result_path.is_file() or result_path.is_symlink():
        raise ModelAdapterError("model adapter did not produce result.json")
    if result_path.stat().st_size > _MAX_MODEL_OUTPUT_BYTES:
        raise ModelAdapterError(
            f"model output exceeds the {_MAX_MODEL_OUTPUT_BYTES} byte hard limit"
        )
    try:
        output = ModelOutput.model_validate(read_json(result_path, root=state_root))
    except (OSError, ValueError, ValidationError) as error:
        raise ModelAdapterError(f"invalid model result: {error}") from error
    write_json_atomic(result_path, output.model_dump(mode="json"), root=state_root)
    return output, usage, adapter_input_bytes


def _model_input_bytes(model_input: ModelInput) -> int:
    payload = canonical_json(model_input.model_dump(mode="json")) + b"\n"
    schema = canonical_json(ModelOutput.model_json_schema()) + b"\n"
    return len(payload) + len(schema)


def _codex_usage(stdout: str) -> tuple[int | None, int | None, int | None, int]:
    requests = 0
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    tool_calls = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            requests += 1
            usage = event.get("usage")
            if isinstance(usage, dict):
                current_input = usage.get("input_tokens")
                current_output = usage.get("output_tokens")
                if isinstance(current_input, int) and isinstance(current_output, int):
                    input_tokens += current_input
                    output_tokens += current_output
                    saw_usage = True
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in _TOOL_ITEM_TYPES:
            tool_calls += 1
    return (
        requests if requests else None,
        input_tokens if saw_usage else None,
        output_tokens if saw_usage else None,
        tool_calls,
    )


def _base_summary(asof: date, run_dir: Path, log: RunLog) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "started",
        "as_of": asof.isoformat(),
        "operator_commands": 1,
        "machine_commands": 0,
        "model_process_launches": 0,
        "model_requests": 0,
        "model_input_bytes": 0,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "ai_duration_seconds": 0.0,
        "ai_file_reads": 0,
        "ai_tool_calls": 0,
        "candidate_count": 0,
        "research_count": 0,
        "skip_count": 0,
        "human_action": None,
        "run_dir": str(run_dir),
        "log_path": str(log.path),
    }


def _existing_triage_summary(
    context: DailyAnalysisContext,
    summary: dict[str, object],
) -> None:
    triage = context.existing_triage
    if triage is None:
        raise AssertionError("existing Triage summary requires a Triage")
    research_count = len(triage.admissible_research_tickers())
    summary.update(
        status="awaiting_human" if research_count else "already_published",
        candidate_count=len(triage.entries),
        research_count=research_count,
        skip_count=len(triage.entries) - research_count,
        human_action="Research Setを選択" if research_count else None,
    )


def _execute(
    *,
    asof: date,
    root: Path,
    state_root: Path,
    run_dir: Path,
    log: RunLog,
    summary: dict[str, object],
    model_runner: ModelRunner,
) -> int:
    app_db_path = root / APPLICATION_DB_PATH
    runs_db_path = root / RUNS_DB_PATH
    context = load_daily_analysis_context(
        asof,
        app_db_path=app_db_path,
        runs_db_path=runs_db_path,
    )
    if context is None:
        summary["status"] = "no_review_set"
        return 0
    summary["candidate_count"] = len(context.review_set.entries)
    if not context.review_set.entries:
        summary["status"] = "empty_review_set"
        return 0
    if context.existing_triage is not None:
        _existing_triage_summary(context, summary)
        return 0

    model_input = _model_input(context)
    write_json_atomic(run_dir / "input.json", model_input.model_dump(mode="json"), root=state_root)
    preflight_bytes = _model_input_bytes(model_input)
    summary["model_input_bytes"] = preflight_bytes
    if preflight_bytes > _MAX_MODEL_INPUT_BYTES:
        raise ValueError(f"model input exceeds the {_MAX_MODEL_INPUT_BYTES} byte hard limit")
    summary["model_process_launches"] = 1
    summary["model_requests"] = None
    summary["ai_file_reads"] = None
    summary["ai_tool_calls"] = None
    output, usage, input_bytes = model_runner(model_input, run_dir, state_root, log)
    write_json_atomic(run_dir / "result.json", output.model_dump(mode="json"), root=state_root)
    expected_tickers = {candidate.ticker for candidate in model_input.candidates}
    actual_tickers = {decision.ticker for decision in output.decisions}
    if len(output.decisions) != len(model_input.candidates) or actual_tickers != expected_tickers:
        raise ValueError("AI decisions must contain every Review Set ticker exactly once")
    summary.update(
        model_requests=usage.model_requests,
        model_input_bytes=input_bytes,
        actual_input_tokens=usage.input_tokens,
        actual_output_tokens=usage.output_tokens,
        ai_duration_seconds=round(usage.duration_seconds, 6),
        ai_file_reads=usage.tool_calls,
        ai_tool_calls=usage.tool_calls,
    )
    triage = publish_daily_research_triage(
        context.review_set.review_set_id,
        output.decisions,
        macro_context_id=(
            None if context.macro_context is None else context.macro_context.context_id
        ),
        app_db_path=app_db_path,
        runs_db_path=runs_db_path,
        published_at=datetime.now(_JST),
    )
    research_count = len(triage.admissible_research_tickers())
    summary.update(
        status="published_awaiting_human" if research_count else "published_all_skip",
        research_count=research_count,
        skip_count=len(triage.entries) - research_count,
        human_action="Research Setを選択" if research_count else None,
    )
    return 0


def _emit(summary: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        return
    for key in (
        "status",
        "as_of",
        "model_process_launches",
        "model_requests",
        "model_input_bytes",
        "actual_input_tokens",
        "actual_output_tokens",
        "research_count",
        "skip_count",
        "human_action",
        "log_path",
    ):
        print(f"{key}={summary.get(key)}")


def _run(args: argparse.Namespace, *, model_runner: ModelRunner = _run_model) -> int:
    asof = args.asof or datetime.now(_JST).date()
    state_root = ensure_private_dir(args.state_dir.absolute())
    lock = PipelineLock(state_root)
    with lock:
        if not lock.acquire():
            _emit(
                {
                    "status": "already_running",
                    "as_of": asof.isoformat(),
                    "model_process_launches": 0,
                },
                args.format,
            )
            return 0
        run_dir = _create_run(state_root)
        log = RunLog(run_dir / "run.log", state_root=state_root)
        summary = _base_summary(asof, run_dir, log)
        try:
            exit_code = _execute(
                asof=asof,
                root=args.repo_root,
                state_root=state_root,
                run_dir=run_dir,
                log=log,
                summary=summary,
                model_runner=model_runner,
            )
        except (
            ModelAdapterError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            ValueError,
        ) as error:
            summary["status"] = "failed"
            summary["failure"] = redact(str(error))[-500:]
            log.append(traceback.format_exc())
            exit_code = 1
        write_json_atomic(run_dir / "summary.json", summary, root=state_root)
        _emit(summary, args.format)
        return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-batch analysis")
    commands = parser.add_subparsers(dest="analysis_command", required=True)
    run = commands.add_parser("run", help="triage the canonical Review Set for one as-of date")
    run.add_argument(
        "--asof",
        type=date.fromisoformat,
        help="JST date for a manual rerun; the default is today's JST date",
    )
    run.add_argument("--state-dir", type=Path, default=default_state_dir())
    run.add_argument("--repo-root", type=Path, default=Path.cwd())
    run.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    previous_umask = os.umask(0o077)
    try:
        return _run(args)
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PipelineLock", "build_parser", "default_state_dir", "main"]
